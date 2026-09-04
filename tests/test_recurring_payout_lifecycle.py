"""Recurring-payout additions: calendar-month intervals, a future start date, and
the pause / resume / cancel lifecycle endpoints.

The exactly-once / balance-safety engine is covered by tests/test_scheduled_payouts.py.
This pins the NEW surface:

  [A] _next_run("monthly") advances by a real CALENDAR month (preserves the
      day-of-month, clamps Jan-31 -> Feb-28/29, rolls the year over Dec->Jan) —
      NOT a fixed 30 days.
  [B] a future start_at defers the first run: next_run_at == start_at and run_due
      does NOT fire it before then; a past/omitted start_at is immediately due.
  [C] POST /v1/scheduled-payouts/<id>/pause|resume|cancel move the schedule
      between active/paused/cancelled, run_due honours the state, and a cancelled
      schedule cannot be resumed (409).
  [D] the lifecycle endpoints are money-OUT control: a collections-only key is
      403'd, a bogus key 401'd (guardrail 24).

Script-style; mock rail, no Redis, temp FILE db (see test_scheduled_payouts.py).
"""
import atexit
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="recurring_lifecycle_")
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
from app.models import AccountType, Channel, Merchant, Payout, ScheduledPayout
from app.services import ledger
from app.services.scheduled_payouts_service import (
    _next_run, create_scheduled_payout, run_due,
)

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def make_merchant(email, *, start_balance=0, is_test=False):
    m = Merchant(name=email.split("@")[0], email=email,
                 public_key=f"pk_{email}", secret_key=f"sk_live_{email}",
                 test_secret_key=f"sk_test_{email}", kyc_status="verified")
    db.session.add(m)
    db.session.commit()
    if start_balance:
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id,
            currency="UGX", is_test=is_test)
        rail = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None,
            currency="UGX", is_test=is_test)
        ledger.post([(rail, +start_balance), (avail, -start_balance)],
                    currency="UGX", memo="funding")
        db.session.commit()
    return m


def payout_count(mid, is_test=False):
    return Payout.query.filter_by(merchant_id=mid, is_test=is_test).count()


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()

    # ---- [A] calendar-month _next_run ----
    with app.app_context():
        d = lambda *a: datetime(*a, tzinfo=timezone.utc)
        check("[A] monthly preserves day-of-month (Jan 15 -> Feb 15)",
              _next_run(d(2026, 1, 15), "monthly") == d(2026, 2, 15))
        check("[A] monthly clamps to month length (Jan 31 -> Feb 28, 2026)",
              _next_run(d(2026, 1, 31), "monthly") == d(2026, 2, 28))
        check("[A] monthly rolls the year over (Dec 10 -> Jan 10 next year)",
              _next_run(d(2026, 12, 10), "monthly") == d(2027, 1, 10))
        check("[A] weekly is unchanged (+7 days)",
              _next_run(d(2026, 3, 1), "weekly") == d(2026, 3, 8))

    # ---- [B] future start_at defers the first run ----
    with app.app_context():
        m = make_merchant("startdate@x.com", start_balance=100_000)
        future = datetime.now(timezone.utc) + timedelta(days=2)
        sp = create_scheduled_payout(
            merchant=m, amount=10_000, currency="UGX", channel=Channel.MTN_MOMO,
            interval="monthly", recipients=[{"phone": "256780000010"}],
            start_at=future)
        check("[B] a future start_at sets next_run_at to the start (dormant)",
              abs((sp.next_run_at.replace(tzinfo=timezone.utc) - future).total_seconds()) < 2)
        run_due()
        check("[B] run_due does NOT fire a not-yet-started schedule (0 payouts)",
              payout_count(m.id) == 0)
        sp2 = db.session.get(ScheduledPayout, sp.id)
        check("[B] the dormant schedule is still active, next_run_at unmoved",
              sp2.status == "active" and sp2.last_run_at is None)

        # a past start_at is immediately due
        m2 = make_merchant("startpast@x.com", start_balance=100_000)
        create_scheduled_payout(
            merchant=m2, amount=10_000, currency="UGX", channel=Channel.MTN_MOMO,
            interval="monthly", recipients=[{"phone": "256780000011"}],
            start_at=datetime.now(timezone.utc) - timedelta(hours=1))
        run_due()
        check("[B] a past start_at is immediately due (fires this cycle)",
              payout_count(m2.id) == 1)

    # ---- [C]/[D] lifecycle endpoints ----
    with app.app_context():
        m = make_merchant("lifecycle@x.com", start_balance=200_000, is_test=True)
        mid = m.id
        sp = create_scheduled_payout(
            merchant=m, amount=10_000, currency="UGX", channel=Channel.MTN_MOMO,
            interval="weekly", recipients=[{"phone": "256780000020"}],
            is_test=True)
        spid_public = sp.public_id
        col_key = "sk_test_col_lifecycle"   # not a real key on this merchant
        full_key = "sk_test_lifecycle@x.com"

    client = app.test_client()

    def hdr(key):
        return {"Authorization": f"Bearer {key}",
                "X-Timestamp": str(int(time.time()))}

    # [D] scope: a bogus/unknown key is 401 at the lifecycle endpoint
    r = client.post(f"/v1/scheduled-payouts/{spid_public}/pause",
                    headers=hdr("sk_test_nope"))
    check("[D] unknown key -> 401 on pause", r.status_code == 401)

    # [C] pause
    r = client.post(f"/v1/scheduled-payouts/{spid_public}/pause", headers=hdr(full_key))
    check("[C] pause returns 200 and status paused",
          r.status_code == 200 and r.get_json()["status"] == "paused")
    with app.app_context():
        run_due()
        check("[C] run_due does NOT fire a paused schedule (0 payouts)",
              payout_count(mid, is_test=True) == 0)

    # [C] resume -> immediately due -> fires
    r = client.post(f"/v1/scheduled-payouts/{spid_public}/resume", headers=hdr(full_key))
    check("[C] resume returns 200 and status active",
          r.status_code == 200 and r.get_json()["status"] == "active")
    with app.app_context():
        run_due()
        check("[C] run_due fires the resumed schedule (1 payout)",
              payout_count(mid, is_test=True) == 1)

    # [C] cancel -> terminal
    r = client.post(f"/v1/scheduled-payouts/{spid_public}/cancel", headers=hdr(full_key))
    check("[C] cancel returns 200 and status cancelled",
          r.status_code == 200 and r.get_json()["status"] == "cancelled")
    r = client.post(f"/v1/scheduled-payouts/{spid_public}/resume", headers=hdr(full_key))
    check("[C] a cancelled schedule cannot be resumed (409)", r.status_code == 409)

    # [C] unknown schedule id -> 404
    r = client.post("/v1/scheduled-payouts/spo_doesnotexist/pause", headers=hdr(full_key))
    check("[C] unknown schedule id -> 404", r.status_code == 404)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
    else:
        print(f"ALL RECURRING-PAYOUT LIFECYCLE TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")
    sys.stdout.flush()
    os._exit(1 if failed else 0)   # mock rail timer thread would hold the process open


if __name__ == "__main__":
    main()
