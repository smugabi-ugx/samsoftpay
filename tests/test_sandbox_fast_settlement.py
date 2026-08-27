"""Sandbox settles fast; live keeps its (longer) hold.

Run: MOMO_USE_REAL=0 .venv\Scripts\python.exe tests\test_sandbox_fast_settlement.py

Two SUCCEEDED charges completed 5 minutes ago — one sandbox (is_test=True), one
live (is_test=False). With the default holds (sandbox 1 min, live 30 min):
  • the SANDBOX charge settles (5 min > 1 min)
  • the LIVE charge does NOT (5 min < 30 min)
A shorter sandbox hold must never release live money early.
"""
import os
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from app import create_app
from app.extensions import db
from app.models import (Account, AccountType, Channel, Merchant, Transaction,
                        TxnStatus, utcnow)
from app.services import ledger
from app.services.settlement import sweep_to_available


def _seed_charge(m, *, is_test, amount, fee, minutes_ago):
    """A SUCCEEDED charge whose net is sitting in merchant_pending for its mode."""
    net = amount - fee
    txn = Transaction(
        public_id=f"ch_{uuid.uuid4().hex[:12]}", merchant_id=m.id, amount=amount,
        fee_amount=fee, currency="UGX", channel=Channel.MTN_MOMO,
        status=TxnStatus.SUCCEEDED, is_test=is_test,
        completed_at=utcnow() - timedelta(minutes=minutes_ago), settled_at=None,
    )
    db.session.add(txn)
    rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None,
                                        currency="UGX", is_test=is_test)
    pending = ledger.get_or_create_account(type=AccountType.MERCHANT_PENDING, merchant_id=m.id,
                                           currency="UGX", is_test=is_test)
    ledger.post([(rail, +net), (pending, -net)], currency="UGX", memo="seed pending")
    db.session.commit()
    return txn


def _avail(m, is_test):
    a = Account.query.filter_by(merchant_id=m.id, type=AccountType.MERCHANT_AVAILABLE,
                                is_test=is_test).one_or_none()
    return -int(a.cached_balance) if a else 0


def main():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="Int", email="i@x.com", public_key="pk", secret_key="sk",
                     handle="int", kyc_status="verified")
        db.session.add(m); db.session.commit()

        sbx = _seed_charge(m, is_test=True,  amount=50_000, fee=750, minutes_ago=5)
        live = _seed_charge(m, is_test=False, amount=40_000, fee=750, minutes_ago=5)

        moved = sweep_to_available()          # mode-aware defaults (sandbox 1, live 30)
        db.session.expire_all()
        db.session.refresh(sbx); db.session.refresh(live)

        assert sbx.settled_at is not None, "[FAIL] sandbox charge (5 min old) should settle at 1-min hold"
        assert live.settled_at is None, "[FAIL] live charge (5 min old) must NOT settle at 30-min hold"
        assert _avail(m, True) == 49_250, f"[FAIL] sandbox available should be 49250, got {_avail(m, True)}"
        assert _avail(m, False) == 0, f"[FAIL] live available must stay 0, got {_avail(m, False)}"
        print(f"PASS — sandbox settled fast (available {_avail(m, True)}); live still held (available {_avail(m, False)})")

        # A short sandbox hold NEVER releases live money early — belt check: even a
        # second sweep leaves live untouched until its own 30-min hold elapses.
        sweep_to_available(); db.session.expire_all(); db.session.refresh(live)
        assert live.settled_at is None and _avail(m, False) == 0, "[FAIL] live released early"
        print("PASS — live money never released early by the sandbox fast-path")
        print("\nALL SANDBOX-FAST-SETTLEMENT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
