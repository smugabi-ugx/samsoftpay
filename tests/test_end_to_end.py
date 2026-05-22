"""End-to-end test: charge a card, force-succeed the rail callback, check the ledger."""
import os
import sys
import time
import uuid

# Make the project importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force deterministic, fast rails BEFORE creating the app.
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"  # always succeed for the test
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db
from app.models import (
    Account,
    AccountType,
    Channel,
    Merchant,
    Transaction,
    TxnStatus,
)
from app.services import ledger
from app.services.orchestrator import create_charge


def main():
    app = create_app()
    with app.app_context():
        db.create_all()

        m = Merchant(
            name="Test Merchant",
            email="test@example.com",
            public_key="pk_test",
            secret_key="sk_test",
            kyc_status="verified",
        )
        db.session.add(m)
        db.session.commit()

        txn = create_charge(
            merchant=m,
            amount=10_000,
            currency="UGX",
            channel=Channel.MTN_MOMO,
            customer_phone="+256700111222",
            customer_email=None,
            merchant_reference="test-001",
        )
        assert txn.status == TxnStatus.AUTHORIZED, txn.status
        print(f"created {txn.public_id} status={txn.status.value} rail_ref={txn.rail_reference}")

        # The mock rail fires a callback ~1s later. Poll for completion.
        for _ in range(40):
            time.sleep(0.1)
            db.session.refresh(txn)
            if txn.status in (TxnStatus.SUCCEEDED, TxnStatus.FAILED):
                break
        assert txn.status == TxnStatus.SUCCEEDED, f"expected SUCCEEDED, got {txn.status}"
        print(f"completed: status={txn.status.value} fee={txn.fee_amount}")

        # Ledger invariants
        mismatches = ledger.assert_balances_match()
        assert not mismatches, f"balance mismatches: {mismatches}"
        print("ledger cached == recomputed: OK")

        # Sum of every journal entry across all accounts must equal 0.
        from app.models import JournalEntry
        from sqlalchemy import func
        total = db.session.query(func.coalesce(func.sum(JournalEntry.amount), 0)).scalar()
        assert int(total) == 0, f"journal does not sum to zero: {total}"
        print(f"journal global sum: {total}")

        # Merchant pending should be amount - fee, expressed as a credit (negative).
        merch_pending = Account.query.filter_by(
            merchant_id=m.id, type=AccountType.MERCHANT_PENDING
        ).one()
        expected = -(10_000 - txn.fee_amount)
        assert merch_pending.cached_balance == expected, (
            f"merchant_pending: got {merch_pending.cached_balance}, expected {expected}"
        )
        print(f"merchant_pending balance: {merch_pending.cached_balance} (= -{10_000 - txn.fee_amount})")

        # PSP revenue should equal the fee (also credit-side).
        revenue = Account.query.filter_by(type=AccountType.PSP_REVENUE).one()
        assert revenue.cached_balance == -txn.fee_amount
        print(f"psp_revenue balance: {revenue.cached_balance}")

        # Idempotency: re-querying via API would return the same response (tested
        # at the route level in a real suite — here we just demonstrate the model).
        print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
