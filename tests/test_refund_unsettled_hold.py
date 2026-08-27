"""Refunding a charge that is still in the settlement hold must NOT strand its
pending credit or over-debit withdrawable balance (adversarial money audit, HIGH).

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_refund_unsettled_hold.py

Bug: a non-instant merchant's succeeded charge posts net (amount-fee) into
MERCHANT_PENDING with settled_at=None. Refunding BEFORE the hold set status
REFUNDED and funded a payout from MERCHANT_AVAILABLE — but the pending credit was
never released (the sweep only settles SUCCEEDED charges), so ~net was frozen in
pending forever and available was over-debited by net. Ledger still summed to
zero, so reconciliation passed. Fix: refund_charge early-releases pending->available
(and sets settled_at) before funding the payout.

Proves after refund: (a) merchant_pending == 0 (nothing stranded),
(b) the journal sums to zero, (c) withdrawable dropped by EXACTLY the disbursement
fee — the intended net cost of a refund, not net+fee.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import (Merchant, Transaction, TxnStatus, Channel, AccountType, utcnow)
from app.services import ledger


def bal(mtype, merchant_id, is_test):
    """Withdrawable-style read: credit-normal accounts are stored negative, so the
    merchant-facing number is -cached_balance."""
    a = ledger.get_or_create_account(type=mtype, merchant_id=merchant_id,
                                     currency="UGX", is_test=is_test)
    db.session.refresh(a)
    return -int(a.cached_balance)


def journal_sum():
    from app.models import JournalEntry
    return sum(int(e.amount) for e in JournalEntry.query.all())


def main():
    app = create_app({"WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="K", email="k@x.com", public_key="pk", secret_key="sk_test_k",
                     handle="k", kyc_status="verified",
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        mode = True   # sandbox ledger

        AMOUNT, FEE = 100_000, 1_500
        NET = AMOUNT - FEE
        W0 = 200_000   # other available funds so the refund payout's balance check passes

        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                            merchant_id=None, currency="UGX", is_test=mode)
        avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                             merchant_id=m.id, currency="UGX", is_test=mode)
        pending = ledger.get_or_create_account(type=AccountType.MERCHANT_PENDING,
                                               merchant_id=m.id, currency="UGX", is_test=mode)
        # Fund available with W0, and place a succeeded-but-unsettled charge's NET in pending.
        ledger.post([(rail, +W0), (avail, -W0)], currency="UGX", memo="seed available")
        ledger.post([(rail, +NET), (pending, -NET)], currency="UGX", memo="charge into hold")
        db.session.commit()

        txn = Transaction(public_id="ch_hold", merchant_id=m.id, amount=AMOUNT,
                          fee_amount=FEE, currency="UGX", channel=Channel.MTN_MOMO,
                          status=TxnStatus.SUCCEEDED, is_test=mode,
                          customer_phone="256780000000", completed_at=utcnow(),
                          settled_at=None)   # STILL IN HOLD
        db.session.add(txn); db.session.commit()

        assert bal(AccountType.MERCHANT_PENDING, m.id, mode) == NET, "precondition: net in pending"
        assert bal(AccountType.MERCHANT_AVAILABLE, m.id, mode) == W0, "precondition: W0 available"
        assert journal_sum() == 0, "precondition: journal balanced"

        from app.services.refunds import refund_charge
        # A request context so refund_charge sets g.api_mode='test' and the refund
        # payout funds from the SAME (sandbox) ledger the charge lives on.
        with app.test_request_context():
            res = refund_charge(txn, m)
        assert res["ok"], f"refund failed: {res}"
        payout = res["payout"]
        payout_fee = int(payout.fee_amount or 0)

        pend_after = bal(AccountType.MERCHANT_PENDING, m.id, mode)
        avail_after = bal(AccountType.MERCHANT_AVAILABLE, m.id, mode)

        # (a) THE FIX: pending fully released — nothing stranded (was NET before).
        assert pend_after == 0, f"[a] pending stranded! expected 0, got {pend_after}"
        # (b) double-entry intact.
        assert journal_sum() == 0, "[b] journal no longer sums to zero"
        # (c) withdrawable = available minus what the refund earmarked into suspense.
        #     The merchant's net cost of the refund is only the disbursement fee:
        #     start W0, +NET released, +FEE returned, then the payout earmarks
        #     (AMOUNT + payout_fee) out of available -> W0 - payout_fee.
        expected = W0 - payout_fee
        assert avail_after == expected, \
            f"[c] over/under-debit: expected {expected} (W0 - payout_fee), got {avail_after}"
        print(f"[a] PASS — pending released to 0 (was {NET} stranded before the fix)")
        print(f"[b] PASS — journal sums to zero")
        print(f"[c] PASS — withdrawable = W0 - payout_fee = {expected} "
              f"(net cost of refund is only the {payout_fee} disbursement fee)")
        print("\nALL REFUND-UNSETTLED-HOLD ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
