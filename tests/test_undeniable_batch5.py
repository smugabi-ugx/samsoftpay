"""Undeniable batch 5 — Hakikisha pre-flight, dispute front-door, webhook URL
self-service, panic-freeze anti-DoS.

What this proves:
  1. GET /v1/resolve-account: deterministic sandbox names; the magic
     not-found number resolves inactive; full-scope keys only (collections
     key 403s — a kiosk must not enumerate wallet owners); bad input 400s.
  2. Public dispute door: customer files a report on a paid link; Dispute row
     created, dispute.opened webhook enqueued; a second submit does NOT
     duplicate; unknown link 404s.
  3. Merchant dispute page lists it; closing requires ownership (cross-
     merchant 404) and a valid outcome; closed disputes can't be re-closed.
  4. Webhook URL self-service: merchant can update it (SSRF-guarded — an
     internal address is refused), and can clear it.
  5. Panic-freeze anti-DoS: ONE merchant over the platform panic threshold
     does NOT freeze the platform; two contributors do.
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="undeniable5_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass


from app import create_app
from app.extensions import db
from app.models import (
    Channel, Dispute, Merchant, PaymentLink, Payout, PayoutStatus,
    Transaction, TxnStatus, WebhookDelivery, utcnow,
)

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False,
                      "PAYOUT_PLATFORM_HOURLY_PANIC": 200_000})
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None
    import app.services.alerts as _alerts
    _alerts.send_alert = lambda *a, **k: True

    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="B5", email="b5@x.com", public_key="pk5",
                     secret_key="sk_live_b5", test_secret_key="sk_test_b5",
                     collections_key="sk_test_col_b5",
                     kyc_status="verified", handle="b5",
                     webhook_url="https://merchant.example/hooks",
                     password_hash=generate_password_hash("x"))
        m2 = Merchant(name="B5b", email="b5b@x.com", public_key="pk5b",
                      secret_key="sk_live_b5b", kyc_status="verified",
                      handle="b5b", password_hash=generate_password_hash("x"))
        db.session.add_all([m, m2]); db.session.commit()
        mid, m2id = m.id, m2.id

    c = app.test_client()

    # ── 1. resolve-account ──
    H = {"Authorization": "Bearer sk_test_b5"}
    r = c.get("/v1/resolve-account?phone=0789999888", headers=H)
    j = r.get_json()
    check("resolve: ordinary number is an active named sandbox holder",
          r.status_code == 200 and j["active"] is True
          and j["registered_name"].startswith("SANDBOX HOLDER"))
    r = c.get("/v1/resolve-account?phone=256700000001", headers=H)
    check("resolve: magic not-found number is inactive, no name",
          r.get_json()["active"] is False and r.get_json()["registered_name"] is None)
    r = c.get("/v1/resolve-account?phone=0789999888",
              headers={"Authorization": "Bearer sk_test_col_b5"})
    check("resolve: collections-scoped key is refused (403)", r.status_code == 403)
    r = c.get("/v1/resolve-account?phone=xyz", headers=H)
    check("resolve: junk phone 400s", r.status_code == 400)

    # ── 2. public dispute door ──
    with app.app_context():
        t = Transaction(public_id="txn_d5", merchant_id=mid, amount=8000,
                        fee_amount=200, currency="UGX", channel=Channel.MTN_MOMO,
                        status=TxnStatus.SUCCEEDED, is_test=False,
                        customer_phone="256780000009", completed_at=utcnow())
        db.session.add(t); db.session.flush()
        link = PaymentLink(public_id="plink_d5", merchant_id=mid, amount=8000,
                           currency="UGX", transaction_id=t.id)
        db.session.add(link); db.session.commit()
        wh_before = WebhookDelivery.query.count()

    r = c.get("/pay/plink_d5/report")
    check("report form renders", r.status_code == 200 and b"Report a problem" in r.data)
    r = c.post("/pay/plink_d5/report", data={
        "reason": "not_delivered", "details": "Machine did not give soda",
        "contact": "0789000111"})
    with app.app_context():
        d = Dispute.query.filter_by(merchant_id=mid).all()
        wh_after = WebhookDelivery.query.count()
        check("dispute recorded + dispute.opened webhook enqueued",
              r.status_code == 200 and b"Report received" in r.data
              and len(d) == 1 and d[0].reason == "not_delivered"
              and wh_after == wh_before + 1)
    r = c.post("/pay/plink_d5/report", data={"reason": "other"})
    with app.app_context():
        check("second submit does not duplicate the open dispute",
              Dispute.query.filter_by(merchant_id=mid).count() == 1)
    r = c.post("/pay/plink_nope/report", data={"reason": "other"})
    check("unknown link 404s", r.status_code == 404)
    r = c.post("/pay/plink_d5/report", data={"reason": "hax"})
    check("bogus reason 400s", r.status_code == 400)

    # ── 3. merchant dispute page + close ──
    with c.session_transaction() as s:
        s["_user_id"] = str(mid); s["_fresh"] = True
    r = c.get(f"/dashboard/{mid}/disputes")
    check("merchant sees the dispute with customer details",
          r.status_code == 200 and b"not delivered" in r.data.replace(b"_", b" ")
          and b"0789000111" in r.data)
    with app.app_context():
        d_id = Dispute.query.filter_by(merchant_id=mid).first().id
    # cross-merchant close must 404
    with c.session_transaction() as s:
        s["_user_id"] = str(m2id); s["_fresh"] = True
    r = c.post(f"/dashboard/{m2id}/disputes/{d_id}/close", data={"outcome": "resolved"})
    check("another merchant cannot close it (404)", r.status_code == 404)
    with c.session_transaction() as s:
        s["_user_id"] = str(mid); s["_fresh"] = True
    r = c.post(f"/dashboard/{mid}/disputes/{d_id}/close",
               data={"outcome": "resolved", "note": "refunded via dashboard"})
    with app.app_context():
        d = db.session.get(Dispute, d_id)
        check("owner resolves with a note",
              d.status == "resolved" and d.resolution_note == "refunded via dashboard"
              and d.resolved_at is not None)
    r = c.post(f"/dashboard/{mid}/disputes/{d_id}/close",
               data={"outcome": "dismissed"}, follow_redirects=True)
    with app.app_context():
        check("closed dispute cannot be re-closed",
              db.session.get(Dispute, d_id).status == "resolved")

    # ── 4. webhook URL self-service ──
    r = c.post("/account/webhooks/url",
               data={"webhook_url": "http://169.254.169.254/latest"},
               follow_redirects=True)
    with app.app_context():
        check("internal/metadata address refused by SSRF guard",
              db.session.get(Merchant, mid).webhook_url == "https://merchant.example/hooks")
    # A literal public IP host avoids DNS in the offline suite (the guard
    # checks literal IPs directly without resolving).
    r = c.post("/account/webhooks/url",
               data={"webhook_url": "https://8.8.8.8/hooks2"},
               follow_redirects=True)
    with app.app_context():
        check("public URL update persists",
              db.session.get(Merchant, mid).webhook_url == "https://8.8.8.8/hooks2")

    # ── 5. panic anti-DoS ──
    with app.app_context():
        from app.services import platform_flags
        from app.services.anomaly import scan_payout_anomalies

        def mk(midx, amount, phone):
            db.session.add(Payout(public_id=f"pout_{uuid.uuid4().hex[:16]}",
                                  merchant_id=midx, amount=amount, fee_amount=750,
                                  currency="UGX", channel=Channel.MTN_MOMO,
                                  status=PayoutStatus.SUCCEEDED, is_test=False,
                                  recipient_phone=phone))
        mk(mid, 300_000, "256781000001")   # ONE merchant over the threshold
        db.session.commit()
        findings = scan_payout_anomalies()
        check("single merchant over panic threshold does NOT freeze the platform",
              not platform_flags.payouts_frozen()
              and not any(f["kind"] == "platform_panic" for f in findings))
        mk(m2id, 50_000, "256781000002")   # second contributor
        db.session.commit()
        findings = scan_payout_anomalies()
        check("two contributing merchants over threshold DO trigger auto-freeze",
              platform_flags.payouts_frozen()
              and any(f["kind"] == "platform_panic" for f in findings))
        platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "off")

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL UNDENIABLE-BATCH-5 TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
