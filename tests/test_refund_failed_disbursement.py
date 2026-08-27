"""A refund whose disbursement FAILS at the rail must re-open the charge — not
strand money (audit HIGH). Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_refund_failed_disbursement.py

Bug: refund_charge marks the charge REFUNDED, returns the platform charge fee to
the merchant, and creates a payout to pay the customer. When that payout later
FAILS, complete_payout only reversed the payout's own earmark — it never reversed
the charge-fee return (merchant over-credited, psp_revenue short) nor re-opened
the charge (a retry hit 'already_refunded'). Fix: on refund-payout failure,
reconcile_failed_refund_payout reverses the fee return and sets the charge back to
SUCCEEDED. Proves: available returns to its pre-refund figure, charge is SUCCEEDED
and retryable, and the journal sums to zero.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import (Merchant, Transaction, TxnStatus, Channel, Payout,
                        PayoutStatus, AccountType, utcnow)
from app.services import ledger


def avail(mid, mode=True):
    a = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid,
                                     currency="UGX", is_test=mode)
    db.session.refresh(a)
    return -int(a.cached_balance)


def journal_sum():
    from app.models import JournalEntry
    return sum(int(e.amount) for e in JournalEntry.query.all())


def main():
    app = create_app({"WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="K", email="k@x.com", public_key="pk", secret_key="sk_test_k",
                     handle="k", kyc_status="verified", password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        mode = True
        W0 = 200_000
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None,
                                            currency="UGX", is_test=mode)
        av = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id,
                                          currency="UGX", is_test=mode)
        ledger.post([(rail, +W0), (av, -W0)], currency="UGX", memo="seed")
        db.session.commit()

        # A SETTLED succeeded charge (settled_at set → no early-release path).
        txn = Transaction(public_id="ch_ref", merchant_id=m.id, amount=100_000, fee_amount=1_500,
                          currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                          is_test=mode, customer_phone="256780000000", completed_at=utcnow(),
                          settled_at=utcnow())
        db.session.add(txn); db.session.commit()
        assert avail(m.id) == W0 and journal_sum() == 0, "precondition"

        from app.services.refunds import refund_charge
        with app.test_request_context():
            res = refund_charge(txn, m)
        assert res["ok"], f"refund failed: {res}"
        payout = res["payout"]
        assert txn.refund_payout_id == payout.id, "charge should link to refund payout"

        # Now FAIL the refund's disbursement at the rail.
        from app.services.payouts import complete_payout
        complete_payout(payout.id, success=False, rail_reference="x", reason="recipient_not_found")

        db.session.refresh(txn); db.session.refresh(payout)
        # (a) charge re-opened and retryable
        assert txn.status == TxnStatus.SUCCEEDED, f"[a] charge must reopen SUCCEEDED, got {txn.status}"
        assert txn.refund_payout_id is None and txn.refunded_at is None, "[a] refund fields cleared"
        assert payout.status == PayoutStatus.FAILED, "[a] payout FAILED"
        # (b) no money stranded — available back to the pre-refund figure
        assert avail(m.id) == W0, f"[b] available should return to {W0}, got {avail(m.id)}"
        # (c) double-entry intact
        assert journal_sum() == 0, "[c] journal must sum to zero"
        print(f"[a] PASS — failed refund re-opened the charge (SUCCEEDED, retryable)")
        print(f"[b] PASS — available restored to {W0} (no over-credit, psp_revenue made whole)")
        print(f"[c] PASS — journal sums to zero")

        # (d) the merchant can actually retry the refund now
        with app.test_request_context():
            res2 = refund_charge(txn, m)
        assert res2["ok"], f"[d] retry refund should work now: {res2}"
        print("[d] PASS — merchant can retry the refund (no 'already_refunded' dead-end)")

        print("\nALL REFUND-FAILED-DISBURSEMENT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
