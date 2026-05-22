"""End-to-end test for the full payment gateway flow:
   Collections (money IN) followed by Disbursements (money OUT).

Uses mock rails (fast, deterministic) — no real MoMo calls.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force mocks and fast settings BEFORE creating the app.
os.environ.pop("MOMO_USE_REAL", None)
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db
from app.models import (
    Account,
    AccountType,
    Channel,
    JournalEntry,
    Merchant,
    Payout,
    PayoutStatus,
    Transaction,
    TxnStatus,
)
from app.services import ledger
from app.services.orchestrator import create_charge
from app.services.payouts import create_payout


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(
            name="Combined Test", email="combined@example.com",
            public_key="pk", secret_key="sk", kyc_status="verified",
        )
        db.session.add(m); db.session.commit()

        # ---------- 1. COLLECTION: customer pays merchant ----------
        txn = create_charge(
            merchant=m, amount=100_000, currency="UGX", channel=Channel.MTN_MOMO,
            customer_phone="+256700111222", customer_email=None,
            merchant_reference="combined-test",
        )
        for _ in range(40):
            time.sleep(0.1)
            db.session.refresh(txn)
            if txn.status in (TxnStatus.SUCCEEDED, TxnStatus.FAILED):
                break
        assert txn.status == TxnStatus.SUCCEEDED, txn.status
        print(f"[1] Collection: {txn.public_id} succeeded, fee={txn.fee_amount}")

        # ---------- 2. SWEEP: move pending -> available ----------
        pending = Account.query.filter_by(
            merchant_id=m.id, type=AccountType.MERCHANT_PENDING
        ).one()
        available = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id, currency="UGX"
        )
        amt = -pending.cached_balance  # convert credit to positive
        ledger.post(
            [(pending, +amt), (available, -amt)],
            currency="UGX",
            memo="sweep to available",
        )
        db.session.commit()
        print(f"[2] Swept {amt} UGX from pending to available")

        # ---------- 3. DISBURSEMENT: pay out part of it ----------
        p = create_payout(
            merchant=m, amount=50_000, currency="UGX",
            recipient_phone="+256780000001", recipient_name="Recipient",
        )
        for _ in range(40):
            time.sleep(0.1)
            db.session.refresh(p)
            if p.status in (PayoutStatus.SUCCEEDED, PayoutStatus.FAILED):
                break
        assert p.status == PayoutStatus.SUCCEEDED, p.status
        print(f"[3] Disbursement: {p.public_id} succeeded")

        # ---------- 4. LEDGER INVARIANTS ----------
        from sqlalchemy import func
        total = db.session.query(func.coalesce(func.sum(JournalEntry.amount), 0)).scalar()
        assert int(total) == 0, f"journal not zero: {total}"
        mismatches = ledger.assert_balances_match()
        assert not mismatches, f"balance mismatches: {mismatches}"

        db.session.refresh(available)
        avail_now = -available.cached_balance
        # Started with 0, got 98500 (100k charge - 1.5k fee), paid out 50k -> 48500
        assert avail_now == 48_500, f"available: expected 48500, got {avail_now}"
        print(f"[4] Final available: {avail_now} UGX (ledger balanced & reconciled)")
        print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
