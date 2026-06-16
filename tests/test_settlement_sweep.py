"""Settlement sweep: only transactions past THEIR OWN hold settle, exactly once.

Verifies the fix where the old sweep could release money from a young transaction
just because the merchant also had an older one.
"""
import os
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["MOMO_USE_REAL"] = "0"   # force the in-process mock rail

from app import create_app
from app.extensions import db
from app.models import (
    Account, AccountType, Channel, Merchant, Transaction, TxnStatus, utcnow,
)
from app.services import ledger
from app.services.orchestrator import create_charge
from app.services.settlement import sweep_to_available


def _make_succeeded_charge(m, amount):
    txn = create_charge(
        merchant=m, amount=amount, currency="UGX", channel=Channel.MTN_MOMO,
        customer_phone="+256700111222", customer_email=None, merchant_reference="x",
    )
    for _ in range(40):
        time.sleep(0.1)
        db.session.refresh(txn)
        if txn.status in (TxnStatus.SUCCEEDED, TxnStatus.FAILED):
            break
    assert txn.status == TxnStatus.SUCCEEDED, txn.status
    return txn


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="M", email="m@x.com", public_key="pk", secret_key="sk",
                     kyc_status="verified")
        db.session.add(m)
        db.session.commit()

        old = _make_succeeded_charge(m, 10_000)   # will be aged past hold
        new = _make_succeeded_charge(m, 5_000)    # stays within hold

        # Backdate only the "old" charge to 30h ago.
        old.completed_at = utcnow() - timedelta(hours=30)
        db.session.commit()

        pending = Account.query.filter_by(merchant_id=m.id, type=AccountType.MERCHANT_PENDING).one()
        avail_before = Account.query.filter_by(merchant_id=m.id, type=AccountType.MERCHANT_AVAILABLE).one_or_none()
        print(f"before sweep: pending={pending.cached_balance} available={avail_before.cached_balance if avail_before else 0}")

        moved = sweep_to_available(hold_hours=24)
        db.session.expire_all()

        # Only the old charge (net 10000-200=9800) should move.
        assert moved.get(m.id) == 9_800, f"expected 9800 moved, got {moved.get(m.id)}"

        db.session.refresh(old); db.session.refresh(new)
        assert old.settled_at is not None, "old charge should be settled"
        assert new.settled_at is None, "new charge must NOT be settled (within hold)"

        available = Account.query.filter_by(merchant_id=m.id, type=AccountType.MERCHANT_AVAILABLE).one()
        pending = Account.query.filter_by(merchant_id=m.id, type=AccountType.MERCHANT_PENDING).one()
        # available holds 9800 (credit => -9800); pending still holds the new charge's 4800.
        assert available.cached_balance == -9_800, available.cached_balance
        assert pending.cached_balance == -4_800, pending.cached_balance
        print(f"after sweep: pending={pending.cached_balance} available={available.cached_balance} moved={moved}")

        # Idempotency: running again moves nothing (old already settled, new still young).
        moved2 = sweep_to_available(hold_hours=24)
        assert not moved2, f"second sweep should move nothing, moved {moved2}"

        mismatches = ledger.assert_balances_match()
        assert not mismatches, f"ledger mismatch: {mismatches}"
        print("second sweep moved nothing; ledger consistent")
        print("\nALL SETTLEMENT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
