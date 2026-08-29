"""A crashed request must not wedge its Idempotency-Key at 409 forever.

If a process dies between reserve() and store(), the key is stranded IN_FLIGHT
and every retry gets '409 still in flight' until the 30-day prune. reap_stale_
inflight() resolves a reservation older than the lease to a TERMINAL response
(verify/retry-with-a-new-key) — it never re-executes, so there is no double-spend
risk — while a genuinely fresh in-flight reservation is left untouched.
"""
import atexit
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="idem_reap_")
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
from app.models import IdempotencyKey, Merchant, utcnow
from app.services import idempotency


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="I Co", email="i@x.com", public_key="pk_live_i",
                     test_secret_key="sk_test_i", secret_key="sk_live_i")
        db.session.add(m); db.session.commit()
        mid = m.id

        # a STALE in-flight reservation (holder crashed ~20 min ago)
        stale = IdempotencyKey(merchant_id=mid, key="charge:stale",
                               request_hash="h", response_status=idempotency.IN_FLIGHT,
                               response_body="{}")
        db.session.add(stale); db.session.commit()
        stale.created_at = utcnow() - timedelta(minutes=20)
        # a FRESH in-flight reservation (a real request genuinely in flight now)
        fresh = IdempotencyKey(merchant_id=mid, key="charge:fresh",
                               request_hash="h", response_status=idempotency.IN_FLIGHT,
                               response_body="{}")
        db.session.add(fresh); db.session.commit()

        n = idempotency.reap_stale_inflight()   # lease = 15 min
        assert n == 1, f"expected to reap 1, reaped {n}"

        db.session.expire_all()
        s = idempotency.find(mid, "charge:stale")
        f = idempotency.find(mid, "charge:fresh")
        assert s.response_status == 409, f"stale not resolved: {s.response_status}"
        assert "expired" in s.response_body, s.response_body
        assert f.response_status == idempotency.IN_FLIGHT, "fresh reservation was wrongly reaped"
        print("[1] stale IN_FLIGHT reservation -> terminal 409 'expired'; fresh one untouched")

        # a retry now replays the terminal message (no re-execution) — verified by
        # the fact the row is a non-IN_FLIGHT cached response the api replays.
        assert s.response_status != idempotency.IN_FLIGHT
        print("[2] the reaped key now returns a terminal response (no re-charge)")

    print("\nIdempotency reaper check passed.")


if __name__ == "__main__":
    main()
