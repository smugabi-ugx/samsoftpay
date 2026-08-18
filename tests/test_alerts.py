"""Operational alerting, heartbeat, and money-stuck detection.

A log line nobody reads is not an alert. These prove the alerting layer:
  1. send_alert fans out to every configured channel (Slack + email).
  2. A channel that RAISES never propagates — the money task is never disrupted.
  3. Dedupe: a repeat within the window is suppressed (via the Redis throttle).
  4. ALERTS_ENABLED=0 suppresses everything.
  5. Heartbeat: record then read back the age.
  6. check_money_stuck fires an alert for a stranded payout + a stuck-AUTHORIZED
     charge, and stays quiet when there's nothing wrong.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="alerts_test_")
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
    Channel, Merchant, Payout, PayoutStatus, Transaction, TxnStatus, utcnow,
)
from app.services import alerts

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


class FakeRedis:
    """Minimal in-memory stand-in supporting the subset alerts.py uses."""
    def __init__(self):
        self.store = {}

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = str(val)
        return True

    def get(self, key):
        v = self.store.get(key)
        return v.encode() if isinstance(v, str) else v


def main():
    app = create_app({
        "SLACK_WEBHOOK_URL": "https://hooks.slack.test/xxx",
        "MAIL_HOST": "smtp.test",   # makes the email channel attempt a send
        "ALERT_EMAIL": "ops@samsoftpay.test",
        "ALERTS_ENABLED": "1",
    })

    # Force a deterministic fake Redis, and capture the outbound channels.
    fake = FakeRedis()
    alerts._redis_client = fake
    alerts._redis_tried = True

    slack_calls, email_calls = [], []

    def fake_slack(title, body, severity):
        slack_calls.append((title, body, severity))

    def fake_email(title, body, severity):
        email_calls.append((title, body, severity))

    orig_slack, orig_email = alerts._post_slack, alerts._send_email
    orig_sentry = alerts._capture_sentry
    alerts._post_slack = fake_slack
    alerts._send_email = fake_email
    alerts._capture_sentry = lambda *a, **k: None

    with app.app_context():
        db.create_all()

        # 1. Fan-out to both channels.
        sent = alerts.send_alert("Test", "hello", severity="warning",
                                 key="k-fanout", dedupe_seconds=3600)
        check("send_alert returns True when dispatched", sent is True)
        check("slack channel invoked", len(slack_calls) == 1)
        check("email channel invoked", len(email_calls) == 1)

        # 3. Dedupe — same key within window is suppressed.
        again = alerts.send_alert("Test", "hello again", severity="warning",
                                  key="k-fanout", dedupe_seconds=3600)
        check("repeat within window is suppressed (returns False)", again is False)
        check("suppressed alert sent nothing new to slack", len(slack_calls) == 1)

        # A DIFFERENT key still goes out.
        alerts.send_alert("Other", "x", key="k-other", dedupe_seconds=3600)
        check("different key is not throttled", len(slack_calls) == 2)

        # 2. A raising channel must not propagate.
        def boom(*a, **k):
            raise RuntimeError("slack is down")
        alerts._post_slack = boom
        try:
            r = alerts.send_alert("Raises", "y", key="k-raise", dedupe_seconds=0)
            raised = False
        except Exception:
            raised = True
        check("a channel that raises never propagates", not raised)
        check("email still delivered despite slack raising", len(email_calls) == 3)
        alerts._post_slack = fake_slack

        # 4. ALERTS_ENABLED=0 suppresses everything.
        app.config["ALERTS_ENABLED"] = "0"
        before = len(slack_calls)
        off = alerts.send_alert("Off", "z", key="k-off", dedupe_seconds=0)
        check("ALERTS_ENABLED=0 returns False", off is False)
        check("disabled alert sends nothing", len(slack_calls) == before)
        app.config["ALERTS_ENABLED"] = "1"

        # 5. Heartbeat round-trip.
        alerts.record_heartbeat()
        age = alerts.heartbeat_age_seconds()
        check("heartbeat age is a small non-negative number",
              isinstance(age, int) and 0 <= age < 5)

        # 6. Money-stuck detection.
        m = Merchant(name="StuckCo", email="stuck@x.com", public_key="pk_test_s",
                     secret_key="sk_live_s", kyc_status="verified")
        db.session.add(m)
        db.session.commit()

        # A stranded payout: PENDING + no rail_reference.
        db.session.add(Payout(
            public_id="po_stranded", merchant_id=m.id, amount=25000, currency="UGX",
            channel=Channel.MTN_MOMO, status=PayoutStatus.PENDING,
            recipient_phone="256700123456", rail_reference=None))
        # A charge stuck AUTHORIZED for 8 hours.
        db.session.add(Transaction(
            public_id="ch_stuck", merchant_id=m.id, amount=9000, currency="UGX",
            channel=Channel.MTN_MOMO, status=TxnStatus.AUTHORIZED,
            created_at=utcnow() - timedelta(hours=8)))
        db.session.commit()

        from app.tasks.monitoring import check_money_stuck
        slack_before = len(slack_calls)
        result = check_money_stuck()
        check("check_money_stuck flags problems", result["ok"] is False and len(result["problems"]) == 2)
        check("money-stuck fired an alert", len(slack_calls) == slack_before + 1)

        # Quiet when clean: resolve both, re-run, no NEW alert (also dedupe key differs).
        Payout.query.filter_by(public_id="po_stranded").update(
            {"status": PayoutStatus.FAILED})
        Transaction.query.filter_by(public_id="ch_stuck").update(
            {"status": TxnStatus.SUCCEEDED})
        db.session.commit()
        slack_before = len(slack_calls)
        result2 = check_money_stuck()
        check("check_money_stuck clear when nothing stuck", result2["ok"] is True)
        check("no alert when clear", len(slack_calls) == slack_before)

    alerts._post_slack, alerts._send_email = orig_slack, orig_email
    alerts._capture_sentry = orig_sentry

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL ALERTING TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
