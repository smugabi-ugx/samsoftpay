"""End-to-end test for the full payment gateway flow:
   Collections (money IN) followed by Disbursements (money OUT).

Uses mock rails (fast, deterministic) — no real MoMo calls.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force mocks and fast settings BEFORE creating the app.
# SET this rather than popping it: .env sets MOMO_USE_REAL=1 and dotenv refills a
# popped variable, which sends this test at MTN's real sandbox and leaves every
# charge stuck in "authorized".
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"

# A FILE database, not :memory:. The mock rail completes on a timer thread, and
# in-memory SQLite is per-connection, so that thread would write into a different
# empty database and the completion would never be visible here.
import atexit
import tempfile

_FD, _P = tempfile.mkstemp(suffix=".db", prefix="collections_test_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_P)
    except OSError:
        pass

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
        # Re-read pending account to avoid stale-data race vs. the rail callback.
        db.session.expire_all()
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
        # Started with 0, collected 100k less the 1.5k collection fee -> 98,500.
        # Paid out 50k, which also costs the flat payout fee. Derive that fee
        # rather than hardcoding a total: this assertion previously read 48,500,
        # forgetting the payout fee, and never failed because the test died
        # earlier and its later assertions were never reached.
        from app.services.fees import calculate_payout_fee
        payout_fee = calculate_payout_fee(amount=50_000, currency="UGX")  # 1.5% of 50k = 750
        expected = 98_500 - 50_000 - payout_fee
        assert avail_now == expected, (
            f"available: expected {expected} (98,500 - 50,000 - {payout_fee} payout fee), "
            f"got {avail_now}"
        )
        print(f"[4] Final available: {avail_now} UGX (ledger balanced & reconciled)")
        print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
