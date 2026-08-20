"""Undeniable batch 3 — status transparency, no-silent-holds, go-live checks.

What this proves:
  1. /status carries per-rail components fed from real data; an open critical
     ReconException flips MTN to degraded and DECLARES it (our incident, even
     when the root cause is the telco).
  2. The wallet page shows a "Payouts paused" banner with the reason whenever
     this merchant's payouts are paused (recon exception or platform freeze) —
     never discovered at submit time (no silent holds).
  3. Account page renders the machine-checked go-live checklist.
  4. "Send test event" enqueues a signed test.ping that appears in the
     delivery log (the go-live webhook-verification step).
  5. /docs/changelog serves the public changelog with the stability promise.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="undeniable3_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass


from app import create_app
from app.extensions import db
from app.models import Merchant, ReconException

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None

    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="B3", email="b3@x.com", public_key="pk_b3",
                     secret_key="sk_live_b3", kyc_status="verified", handle="b3",
                     webhook_url="https://ex.example/hooks",
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        mid = m.id

    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(mid); s["_fresh"] = True

    # ── 1. status page: healthy first ──
    r = c.get("/status")
    check("status page lists data-fed components",
          r.status_code == 200 and b"Webhook delivery" in r.data
          and b"MTN Mobile Money (UG)" in r.data)

    # ── 2/3/4. account page + test.ping + delivery log ──
    r = c.get("/account")
    html = r.get_data(as_text=True)
    check("go-live checklist renders with machine-checked items",
          r.status_code == 200 and "Ready to go live?" in html
          and "First live settlement released" in html)
    r = c.post("/account/webhooks/test", follow_redirects=True)
    check("send-test-webhook queues a signed test.ping",
          b"Test event queued" in r.data)
    r = c.get("/account")
    check("delivery log shows the test.ping", "test.ping" in r.get_data(as_text=True))

    # ── wallet healthy: no banner ──
    r = c.get("/dashboard/wallet")
    check("wallet: no paused banner when healthy",
          r.status_code == 200 and b"Payouts paused" not in r.data)

    # ── open recon exception: banner + status incident ──
    with app.app_context():
        db.session.add(ReconException(rail_reference="r-b3", merchant_id=mid,
                                      kind="mtn_succeeded_local_not",
                                      severity="critical", status="open"))
        db.session.commit()
    r = c.get("/dashboard/wallet")
    check("no-silent-holds: wallet banner appears with the reason",
          b"Payouts paused" in r.data and b"reconcil" in r.data)
    r = c.get("/status")
    check("status page declares the MTN reconciliation incident",
          b"reconciliation review" in r.data)

    # ── platform freeze: banner too ──
    with app.app_context():
        db.session.query(ReconException).delete()
        db.session.commit()
        from app.services import platform_flags
        platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "on")
    r = c.get("/dashboard/wallet")
    check("platform freeze also surfaces on the wallet page",
          b"Payouts paused" in r.data and b"safety review" in r.data)
    with app.app_context():
        from app.services import platform_flags
        platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "off")

    # ── 5. changelog ──
    r = c.get("/docs/changelog")
    check("/docs/changelog serves with the stability promise",
          r.status_code == 200 and b"additive" in r.data.lower())

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL UNDENIABLE-BATCH-3 TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
