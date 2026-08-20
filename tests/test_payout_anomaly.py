"""Payout anomaly detection — the aggregate/velocity gap the Flutterwave ₦11B
attackers exploited (every transfer under the per-txn threshold) and the
Pegasus weekend raid (nobody watching).

What this proves:
  1. A quiet platform scans clean (no findings).
  2. merchant_hourly_cap: many small live payouts summing over the cap are
     flagged even though each one is individually unremarkable.
  3. destination_daily_cap: one phone collecting from MULTIPLE merchants over
     the daily cap is flagged (mule concentration — invisible per merchant).
  4. Test-mode payouts never count toward live anomaly sums.
  5. platform_panic (armed): the scan AUTO-FREEZES payouts platform-wide and
     create_payout then refuses live payouts.
  6. Alerts are attempted for findings (send_alert called; never raises).
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="anomaly_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass


from app import create_app
from app.extensions import db
from app.models import Channel, Merchant, Payout, PayoutStatus, utcnow

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def mk_payout(mid, amount, phone, *, is_test=False, status=PayoutStatus.SUCCEEDED):
    p = Payout(public_id=f"pout_{uuid.uuid4().hex[:16]}", merchant_id=mid,
               amount=amount, fee_amount=750, currency="UGX",
               channel=Channel.MTN_MOMO, status=status, is_test=is_test,
               recipient_phone=phone)
    db.session.add(p)
    return p


def main():
    app = create_app({
        "WTF_CSRF_ENABLED": False,
        "PAYOUT_MERCHANT_HOURLY_CAP": 100_000,
        "PAYOUT_DESTINATION_DAILY_CAP": 150_000,
        "PAYOUT_PLATFORM_HOURLY_PANIC": 0,   # disarmed for the first checks
    })

    alerts_sent = []
    import app.services.alerts as _alerts
    real_send = _alerts.send_alert
    def fake_alert(title, body, **kw):
        alerts_sent.append(title)
        return True
    _alerts.send_alert = fake_alert

    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m1 = Merchant(name="A1", email="a1@x.com", public_key="pk1", secret_key="sk1",
                      kyc_status="verified", handle="a1",
                      password_hash=generate_password_hash("x"))
        m2 = Merchant(name="A2", email="a2@x.com", public_key="pk2", secret_key="sk2",
                      kyc_status="verified", handle="a2",
                      password_hash=generate_password_hash("x"))
        db.session.add_all([m1, m2]); db.session.commit()
        mid1, mid2 = m1.id, m2.id

        from app.services.anomaly import scan_payout_anomalies

        # ── 1. quiet platform ──
        check("quiet platform scans clean", scan_payout_anomalies() == [])

        # ── 2. sub-threshold drip: 30 x 4000 = 120k > 100k hourly cap ──
        for i in range(30):
            mk_payout(mid1, 4000, f"25678{i:07d}")
        db.session.commit()
        findings = scan_payout_anomalies()
        check("sub-threshold drip flagged by merchant hourly cap",
              any(f["kind"] == "merchant_hourly_cap" and f["merchant_id"] == mid1
                  and f["total"] == 120000 for f in findings))
        check("alert attempted for the finding",
              any("merchant_hourly_cap" in t for t in alerts_sent))

        # clear m1's payouts for the next scenario
        Payout.query.delete(); db.session.commit()

        # ── 3. mule concentration across merchants ──
        mk_payout(mid1, 90_000, "256781112223")
        mk_payout(mid2, 90_000, "256781112223")   # same destination, other merchant
        db.session.commit()
        findings = scan_payout_anomalies()
        check("cross-merchant destination concentration flagged",
              any(f["kind"] == "destination_daily_cap"
                  and f["total"] == 180_000 for f in findings))
        check("neither merchant tripped their own hourly cap (that's the point)",
              not any(f["kind"] == "merchant_hourly_cap" for f in findings))

        # ── 4. test-mode payouts don't count ──
        Payout.query.delete(); db.session.commit()
        for i in range(40):
            mk_payout(mid1, 4000, f"25670{i:07d}", is_test=True)
        db.session.commit()
        check("sandbox payouts never count toward live anomaly sums",
              scan_payout_anomalies() == [])

        # ── 5. platform panic auto-freeze ──
        app.config["PAYOUT_PLATFORM_HOURLY_PANIC"] = 200_000
        Payout.query.delete(); db.session.commit()
        mk_payout(mid1, 150_000, "256782000001")
        mk_payout(mid2, 150_000, "256782000002")
        db.session.commit()
        findings = scan_payout_anomalies()
        from app.services import platform_flags
        check("panic threshold auto-freezes payouts platform-wide",
              any(f["kind"] == "platform_panic" for f in findings)
              and platform_flags.payouts_frozen())

        # and create_payout now refuses live money-out
        from flask import g
        from app.services.payouts import PayoutError, create_payout
        with app.test_request_context():
            g.api_mode = "live"
            refused = False
            try:
                create_payout(merchant=db.session.get(Merchant, mid1),
                              amount=1000, currency="UGX",
                              recipient_phone="256780000009")
            except PayoutError as e:
                refused = "paused" in str(e)
            check("frozen platform refuses live payouts end-to-end", refused)
        platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "off")

    _alerts.send_alert = real_send
    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL PAYOUT-ANOMALY TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
