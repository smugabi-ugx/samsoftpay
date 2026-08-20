"""Fixes for three confirmed findings from the readiness sweep.

1. money-correctness-1 (CRITICAL): a fully-gift-card-covered charge was left
   SUCCEEDED with settled_at=None. The 24h settlement sweep selects exactly
   (SUCCEEDED, settled_at IS NULL, aged) and would post (pending +total,
   available -total) for it — minting a withdrawable balance no rail funded.
   Fix: set settled_at at creation so the sweep never touches it.

2. merchant-founder-1 (CRITICAL): every KYC route carried @verified_required,
   which redirects unverified users to kyc_home — itself @verified_required — so
   a new merchant looped forever (ERR_TOO_MANY_REDIRECTS) and could never get
   verified. Fix: KYC routes are @login_required only.

3. money-correctness-2 (HIGH): bulk payouts did find()->create_payout()->store()
   with no reserve() between, so concurrent identical batches both disbursed.
   Fix: reserve-before-execute per item; a duplicate reference does not re-disburse.
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="sweep_fixes_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


from datetime import timedelta

from app import create_app
from app.extensions import db
from app.models import (
    Account, AccountType, Merchant, Payout, PaymentLink, Transaction, TxnStatus, utcnow,
)
from app.services import ledger
from app.services.giftcards import create_gift_card
from app.services.settlement import sweep_to_available

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Sweep Co", email="sweep@x.com", public_key="pk_sw",
                     secret_key="sk_live_sw", test_public_key="pk_test_sw",
                     test_secret_key="sk_test_sw", kyc_status="verified",
                     email_verified=True, handle="sweep-co")
        from werkzeug.security import generate_password_hash
        m.password_hash = generate_password_hash("Password123")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        card_code = create_gift_card(merchant_id=mid, face_value=5000).code

    client = app.test_client()

    # ── FIX 1: gift-card fully-covered charge must not be minted by the sweep ──
    with app.app_context():
        import uuid
        link = PaymentLink(public_id=f"lnk_{uuid.uuid4().hex[:12]}", merchant_id=mid,
                           amount=5000, currency="UGX", is_test=False)
        db.session.add(link)
        db.session.commit()
        link_pub = link.public_id

    client.post(f"/pay/{link_pub}/apply-voucher", data={"code": card_code})
    client.post(f"/pay/{link_pub}/submit", data={"channel": "mtn_momo", "phone": "256783647260"})

    with app.app_context():
        txn = Transaction.query.filter_by(merchant_id=mid).order_by(Transaction.id.desc()).first()
        check("fully-covered charge is SUCCEEDED", txn and txn.status == TxnStatus.SUCCEEDED)
        check("fully-covered charge has settled_at set (sweep will skip it)",
              txn and txn.settled_at is not None)
        # Age it well past the hold and force a sweep — it must move NOTHING.
        txn.completed_at = utcnow() - timedelta(hours=48)
        db.session.commit()

    with app.app_context():
        moved = sweep_to_available(hold_hours=24)
    with app.app_context():
        avail = Account.query.filter_by(type=AccountType.MERCHANT_AVAILABLE,
                                        merchant_id=mid, is_test=False).first()
        minted = -(avail.cached_balance) if avail else 0
        check("settlement sweep minted NO withdrawable balance for the gift-card charge",
              minted == 0)
        check("sweep reported nothing moved for this merchant", mid not in (moved or {}))

    # ── FIX 2: a pending (unverified) merchant reaches the KYC portal, no loop ──
    with client.session_transaction() as sess:
        sess["_user_id"] = str(mid)
        sess["_fresh"] = True
    r = client.get("/kyc", follow_redirects=False)
    check("GET /kyc for an unverified merchant is 200 (no redirect loop)", r.status_code == 200)
    r2 = client.get("/kyc/step/1", follow_redirects=False)
    check("GET /kyc/step/1 for an unverified merchant is 200", r2.status_code == 200)

    # ── FIX 3: bulk payout reserve — a duplicate reference does not re-disburse ──
    with app.app_context():
        avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                             merchant_id=mid, currency="UGX", is_test=False)
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                            merchant_id=None, currency="UGX", is_test=False)
        ledger.post([(rail, +200000), (avail, -200000)], currency="UGX", memo="fund")
        db.session.commit()

    def bulk(items, idem):
        return client.post("/v1/payouts/bulk", headers={
            "Authorization": "Bearer sk_live_sw", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))},
            json={"payouts": items})

    item = {"amount": 10000, "phone": "256780000001", "name": "X", "reference": "dup-ref-1"}
    r = bulk([item], "batch-1")
    check("bulk payout accepted first time", r.status_code == 200 and r.json["accepted"] == 1)
    with app.app_context():
        after_first = Payout.query.filter_by(merchant_id=mid).count()

    # Re-submit the SAME reference (as a retry / duplicate batch).
    r2 = bulk([item], "batch-2")
    with app.app_context():
        after_second = Payout.query.filter_by(merchant_id=mid).count()
        check("duplicate reference did NOT create a second payout (no double-disburse)",
              after_second == after_first)
    check("duplicate bulk item returns a result (idempotent), 200",
          r2.status_code == 200)

    # ── Ledger cache: two postings to ONE account in one transaction ──
    # ledger.post() assigned the SQL increment expression per leg, so a second
    # posting to the same Account before a flush REPLACED the first pending
    # expression: the journal stayed right while cached_balance silently lost
    # an increment — and the payout overdraft check reads cached_balance.
    with app.app_context():
        from app.services import ledger as _lg
        acct = _lg.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid,
            currency="UGX", is_test=False)
        rail = _lg.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None,
            currency="UGX", is_test=False)
        _lg.post([(rail, +7000), (acct, -7000)], currency="UGX", memo="cache-a")
        _lg.post([(rail, +3000), (acct, -3000)], currency="UGX", memo="cache-b")
        db.session.commit()
        db.session.refresh(acct)
        check("two postings to one account in one transaction: cache == journal",
              int(acct.cached_balance) == _lg.recompute_balance(acct))
        # Same account twice within a SINGLE posting must aggregate, not clobber.
        susp = _lg.get_or_create_account(
            type=AccountType.SUSPENSE, merchant_id=mid,
            currency="UGX", is_test=False)
        db.session.refresh(susp)
        before = int(susp.cached_balance)   # may already hold a payout earmark
        _lg.post([(susp, +5000), (susp, -2000), (rail, -3000)],
                 currency="UGX", memo="cache-c")
        db.session.commit()
        db.session.refresh(susp)
        check("same account twice in one posting aggregates correctly",
              int(susp.cached_balance) == _lg.recompute_balance(susp) == before + 3000)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SWEEP-CRITICAL-FIX TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
