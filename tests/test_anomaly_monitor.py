"""Charge-side anomaly detection + the persisted, reviewable admin feed.

The payout/refund scans already watch money OUT; nothing watched money IN for
fraud/abuse, and no anomaly was persisted for an admin to review. This proves:

  1. failed_charge_storm     — a burst of FAILED charges for one merchant is flagged
  2. failed_charge_storm_phone — repeated failures from ONE phone is flagged
  3. charge_velocity         — a merchant's charge COUNT over the floor is flagged
  4. large_charge            — a single live charge over the floor is flagged
  5. record_anomaly DEDUPES  — a persisting condition doesn't spam new rows
  6. resolve then re-detect  — a resolved anomaly can re-open on recurrence
  7. TEST-mode charges are ignored (sandbox never alerts)
  8. the admin feed route renders and resolve works
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="anomaly_")
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
from app.models import Merchant, Transaction, TxnStatus, Channel, AnomalyEvent

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def _txn(mid, status, amount=5000, phone="256700000001", is_test=False):
    return Transaction(
        public_id=f"txn_{uuid.uuid4().hex[:16]}", merchant_id=mid, amount=amount,
        fee_amount=0, currency="UGX", channel=Channel.MTN_MOMO, status=status,
        is_test=is_test, customer_phone=phone)


def open_kinds(mid=None):
    q = AnomalyEvent.query.filter_by(status="open")
    if mid is not None:
        q = q.filter_by(merchant_id=mid)
    return sorted({a.kind for a in q.all()})


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        app.config.update(
            CHARGE_FAILED_STORM_COUNT=3, CHARGE_FAILED_PHONE_COUNT=3,
            CHARGE_VELOCITY_COUNT=5, LARGE_CHARGE_AMOUNT=1_000_000,
            ALERTS_ENABLED="0")   # no alert side effects in the test

        m = Merchant(name="Anom Co", email="anom@x.com", public_key="pk_test_anom",
                     secret_key="sk_live_anom", test_secret_key="sk_test_anom",
                     kyc_status="verified")
        m2 = Merchant(name="Sandbox Co", email="sbx@x.com", public_key="pk_test_sbx",
                      secret_key="sk_live_sbx", test_secret_key="sk_test_sbx",
                      kyc_status="verified")
        db.session.add_all([m, m2]); db.session.commit()
        mid, mid2 = m.id, m2.id

        from app.services.anomaly import scan_charge_anomalies

        # 4 FAILED live charges for m from one phone → merchant storm + phone storm
        for _ in range(4):
            db.session.add(_txn(mid, TxnStatus.FAILED, phone="256700000009"))
        # 1 large succeeded live charge → large_charge (and pushes count to 5 → velocity)
        db.session.add(_txn(mid, TxnStatus.SUCCEEDED, amount=2_000_000, phone="256700000009"))
        # TEST-mode failures for m2 (should be IGNORED)
        for _ in range(6):
            db.session.add(_txn(mid2, TxnStatus.FAILED, phone="256700000008", is_test=True))
        db.session.commit()

        scan_charge_anomalies()
        kinds = open_kinds(mid)
        check("1. failed_charge_storm flagged for the merchant", "failed_charge_storm" in kinds)
        check("2. failed_charge_storm_phone flagged", any(
            a.kind == "failed_charge_storm_phone" for a in AnomalyEvent.query.all()))
        check("3. charge_velocity flagged (>=5 charges)", "charge_velocity" in kinds)
        check("4. large_charge flagged", "large_charge" in kinds)
        check("7. TEST-mode failures ignored (no anomaly for sandbox merchant)",
              open_kinds(mid2) == [])

        # 5. DEDUPE — a second scan on the same data must not spam new open rows
        before = AnomalyEvent.query.filter_by(status="open").count()
        scan_charge_anomalies()
        after = AnomalyEvent.query.filter_by(status="open").count()
        check("5. re-scan does not create duplicate open rows", before == after)

        # 6. resolve the merchant storm, then re-scan → it re-opens
        storm = (AnomalyEvent.query
                 .filter_by(kind="failed_charge_storm", merchant_id=mid, status="open")
                 .first())
        storm.status = "resolved"; db.session.commit()
        scan_charge_anomalies()
        reopened = (AnomalyEvent.query
                    .filter_by(kind="failed_charge_storm", merchant_id=mid, status="open")
                    .count())
        total_storm = AnomalyEvent.query.filter_by(
            kind="failed_charge_storm", merchant_id=mid).count()
        check("6. a resolved anomaly re-opens on recurrence (new open row)",
              reopened == 1 and total_storm == 2)

    # 8. admin feed route renders + resolve works
    with app.app_context():
        admin = Merchant(name="Admin", email="admin@x.com", public_key="pk_test_adm",
                         secret_key="sk_live_adm", test_secret_key="sk_test_adm",
                         role="admin", kyc_status="verified")
        db.session.add(admin); db.session.commit()
        admin_id = admin.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)
        sess["_fresh"] = True
    r = client.get("/admin/anomalies")
    check("8. /admin/anomalies renders for admin (200)", r.status_code == 200)
    check("   feed shows an anomaly kind",
          "charge" in (r.get_data(as_text=True) or "").lower())

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
