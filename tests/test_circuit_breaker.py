"""Circuit breaker on the MTN collections rail.

A slow rail must not hold the web threads. Proves: the breaker opens after N
consecutive network failures, blocks while open, half-opens after the cooldown,
and resets on success; and that a breaker-open initiate() surfaces as a CLEAN
FAILED charge (RailUnavailableError), never a phantom AUTHORIZED (which would be
parked for a call MTN never received).
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="breaker_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Channel, Merchant, TxnStatus
from app.services.circuit_breaker import CircuitBreaker, RailUnavailableError
from app.services.rails_mtn_disbursement import AmbiguousRailError


def unit():
    cb = CircuitBreaker("t", fail_threshold=3, reset_timeout=0.08)
    assert cb.allow() is True
    cb.record_failure(); cb.record_failure()
    assert cb.allow() is True, "should still be closed at 2/3 failures"
    cb.record_failure()                       # 3rd -> OPEN
    assert cb.allow() is False and cb.is_open, "should be OPEN at threshold"
    time.sleep(0.09)                          # cooldown elapses -> HALF-OPEN
    assert cb.allow() is True, "should allow one probe after cooldown"
    cb.record_success()                       # probe ok -> CLOSED
    assert cb.allow() is True and not cb.is_open
    # a failing probe re-opens for another cooldown
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open
    time.sleep(0.09)
    assert cb.allow() is True                 # half-open probe
    cb.record_failure()                       # probe fails -> re-open
    assert cb.allow() is False, "a failed probe must re-open the breaker"
    print("[unit] breaker: closed -> open(threshold) -> half-open(cooldown) -> reset/re-open")

    # RailUnavailableError must NOT be an AmbiguousRailError — that distinction is
    # what routes a never-sent charge to FAILED instead of phantom-AUTHORIZED.
    assert not issubclass(RailUnavailableError, AmbiguousRailError)
    print("[unit] RailUnavailableError is distinct from AmbiguousRailError (clean-fail routing)")


def integration():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="B Co", email="b@x.com", public_key="pk_live_b",
                     test_secret_key="sk_test_b", secret_key="sk_live_b",
                     kyc_status="verified", is_active=True)
        db.session.add(m); db.session.commit()

        from app.services import orchestrator

        class _OpenAdapter:
            def initiate(self, txn):
                raise RailUnavailableError("circuit open")

        orig = orchestrator.get_adapter
        orchestrator.get_adapter = lambda ch: _OpenAdapter()
        try:
            from flask import g
            g.api_mode = "test"
            txn = orchestrator.create_charge(
                merchant=m, amount=5000, currency="UGX", channel=Channel.MTN_MOMO,
                customer_phone="256700000000", customer_email=None, merchant_reference="r1")
        finally:
            orchestrator.get_adapter = orig

        assert txn.status == TxnStatus.FAILED, f"breaker-open charge must be FAILED, got {txn.status}"
        assert txn.status != TxnStatus.AUTHORIZED, "must NOT be phantom-AUTHORIZED"
        assert "rail_error" in (txn.failure_reason or ""), txn.failure_reason
        print(f"[integ] breaker-open initiate -> charge FAILED cleanly ({txn.failure_reason[:50]}), not AUTHORIZED")


def main():
    unit()
    integration()
    print("\nCircuit-breaker checks passed.")


if __name__ == "__main__":
    main()
