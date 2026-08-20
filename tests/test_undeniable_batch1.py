"""Undeniable batch 1 — webhook self-service, replay header, money-safety gates.

What this proves:
  1. A replayed idempotent request carries `Idempotent-Replayed: true`; a
     fresh one does not.
  2. GET /v1/webhooks lists this merchant's deliveries (and only theirs).
  3. POST /v1/webhooks/<evt_id>/resend re-queues a failed/exhausted delivery
     (status back to pending, exhausted attempts reset, current URL used).
  4. Resending another merchant's event id 404s.
  5. `flask freeze-payouts on` (the platform flag) blocks LIVE payouts with
     zero writes; TEST payouts keep working; `off` restores.
  6. An OPEN critical ReconException on a merchant blocks THEIR live payouts;
     resolving it unblocks; other merchants unaffected.
  7. Settlement release invariant: a sweep that would drive merchant_pending
     positive is aborted (nothing released) while other merchants still settle.
  8. Dashboard resend route is merchant-scoped (cross-merchant 404).
  9. /docs.md and /docs/llms.txt serve 200 with the contract facts.
"""
import atexit
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="undeniable1_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass


from app import create_app
from app.extensions import db
from app.models import (
    Account, AccountType, Channel, Merchant, Payout, ReconException,
    Transaction, TxnStatus, WebhookDelivery, utcnow,
)
from app.services import ledger

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def hdrs(key, idem):
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))}


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None   # no broker offline

    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="Und Co", email="und@x.com", public_key="pk_u",
                     secret_key="sk_live_u", test_secret_key="sk_test_u",
                     kyc_status="verified", handle="und",
                     webhook_url="https://merchant.example/hooks",
                     password_hash=generate_password_hash("x"))
        m2 = Merchant(name="Other Co", email="oth@x.com", public_key="pk_o",
                      secret_key="sk_live_o", test_secret_key="sk_test_o",
                      kyc_status="verified", handle="oth",
                      webhook_url="https://other.example/hooks",
                      password_hash=generate_password_hash("x"))
        db.session.add_all([m, m2])
        db.session.commit()
        mid, m2id = m.id, m2.id
        for who in (mid, m2id):
            for is_test in (False, True):
                avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                                     merchant_id=who, currency="UGX", is_test=is_test)
                rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                                    merchant_id=None, currency="UGX", is_test=is_test)
                ledger.post([(rail, +200000), (avail, -200000)], currency="UGX", memo="fund")
        db.session.commit()

    c = app.test_client()

    # ── 1. Idempotent-Replayed header ──
    body = {"amount": 5000, "channel": "mtn_momo", "customer": {"phone": "256700000000"}}
    r1 = c.post("/v1/charges", headers=hdrs("sk_test_u", "rep1"), json=body)
    check("fresh charge has NO replay header",
          r1.status_code == 201 and "Idempotent-Replayed" not in r1.headers)
    r2 = c.post("/v1/charges", headers=hdrs("sk_test_u", "rep1"), json=body)
    check("replayed charge returns original + Idempotent-Replayed: true",
          r2.status_code == r1.status_code
          and r2.headers.get("Idempotent-Replayed") == "true"
          and r2.get_json()["id"] == r1.get_json()["id"])

    # ── 2/3/4. Webhook delivery list + resend ──
    with app.app_context():
        from app.services.webhooks import enqueue
        enqueue(db.session.get(Merchant, mid), "charge.succeeded", {"id": "txn_x"})
        enqueue(db.session.get(Merchant, m2id), "charge.succeeded", {"id": "txn_y"})
        wh = WebhookDelivery.query.filter_by(merchant_id=mid).first()
        wh.status = "failed"
        wh.attempts = 8          # exhausted — sweep would never retry it again
        wh.url = "https://old-wrong.example/hooks"
        db.session.commit()
        evt_mine = json.loads(wh.payload)["id"]
        wh2 = WebhookDelivery.query.filter_by(merchant_id=m2id).first()
        evt_other = json.loads(wh2.payload)["id"]

    r = c.get("/v1/webhooks", headers={"Authorization": "Bearer sk_live_u"})
    data = r.get_json()["data"]
    check("GET /v1/webhooks lists own deliveries only",
          r.status_code == 200 and len(data) == 1 and data[0]["id"] == evt_mine
          and data[0]["event"] == "charge.succeeded")

    r = c.post(f"/v1/webhooks/{evt_mine}/resend", headers=hdrs("sk_live_u", "rs1"))
    with app.app_context():
        wh = WebhookDelivery.query.filter_by(merchant_id=mid).first()
        check("resend re-queues: pending, attempts reset, CURRENT url",
              r.status_code == 202 and wh.status == "pending"
              and wh.attempts == 0 and wh.url == "https://merchant.example/hooks")

    r = c.post(f"/v1/webhooks/{evt_other}/resend", headers=hdrs("sk_live_u", "rs2"))
    check("resending another merchant's event 404s", r.status_code == 404)

    # ── 5. freeze-payouts kill switch ──
    def payout(key, idem, amount=2000):
        return c.post("/v1/payouts", headers=hdrs(key, idem),
                      json={"amount": amount, "recipient": {"phone": "256780000001"}})
    with app.app_context():
        from app.services import platform_flags
        platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "on", updated_by="test")
        pc_before = Payout.query.count()
    r = payout("sk_live_u", "fz1")
    with app.app_context():
        check("frozen: live payout refused with zero writes",
              r.status_code == 400 and "paused" in r.get_json()["error"]
              and Payout.query.count() == pc_before)
    r = payout("sk_test_u", "fz2")
    check("frozen: TEST payout still works", r.status_code == 201)
    with app.app_context():
        from app.services import platform_flags
        platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "off", updated_by="test")
    r = payout("sk_live_u", "fz3")
    check("unfrozen: live payout flows again", r.status_code == 201)

    # ── 6. open recon exception blocks that merchant's live payouts ──
    with app.app_context():
        rx = ReconException(rail_reference="ref-recon-1", merchant_id=mid,
                            kind="mtn_succeeded_local_not", severity="critical",
                            status="open")
        db.session.add(rx); db.session.commit()
        rx_id = rx.id
    r = payout("sk_live_u", "rc1")
    check("open critical recon exception pauses THIS merchant's live payouts",
          r.status_code == 400 and "reconciliation" in r.get_json()["error"])
    r = payout("sk_live_o", "rc2")
    check("other merchants unaffected by someone else's recon exception",
          r.status_code == 201)
    r = payout("sk_test_u", "rc3")
    check("test payouts unaffected by a recon exception", r.status_code == 201)
    with app.app_context():
        rx = db.session.get(ReconException, rx_id)
        rx.status = "resolved"
        db.session.commit()
    r = payout("sk_live_u", "rc4")
    check("resolved exception unblocks payouts", r.status_code == 201)

    # ── 7. settlement release invariant ──
    with app.app_context():
        from datetime import timedelta
        old = utcnow() - timedelta(hours=48)
        # m: a due txn whose pending account does NOT hold the money (simulated
        # dry posting — pending is empty). Sweep must ABORT m's release.
        t_bad = Transaction(public_id="txn_inv1", merchant_id=mid, amount=30000,
                            fee_amount=0, currency="UGX", channel=Channel.MTN_MOMO,
                            status=TxnStatus.SUCCEEDED, is_test=False,
                            customer_phone="256780000001", completed_at=old)
        # m2: a healthy due txn with real pending money — must still settle.
        t_ok = Transaction(public_id="txn_inv2", merchant_id=m2id, amount=10000,
                           fee_amount=0, currency="UGX", channel=Channel.MTN_MOMO,
                           status=TxnStatus.SUCCEEDED, is_test=False,
                           customer_phone="256780000001", completed_at=old)
        db.session.add_all([t_bad, t_ok])
        pend2 = ledger.get_or_create_account(type=AccountType.MERCHANT_PENDING,
                                             merchant_id=m2id, currency="UGX", is_test=False)
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                            merchant_id=None, currency="UGX", is_test=False)
        ledger.post([(rail, +10000), (pend2, -10000)], currency="UGX", memo="pend fund")
        db.session.commit()

        from app.services.settlement import sweep_to_available
        moved = sweep_to_available(hold_hours=24)
        t_bad = Transaction.query.filter_by(public_id="txn_inv1").first()
        t_ok = Transaction.query.filter_by(public_id="txn_inv2").first()
        pend1 = Account.query.filter_by(merchant_id=mid, type=AccountType.MERCHANT_PENDING,
                                        currency="UGX", is_test=False).first()
        check("invariant: dry-posting release ABORTED (txn unsettled, nothing moved)",
              t_bad.settled_at is None and moved.get(mid) is None
              and (pend1 is None or int(pend1.cached_balance) <= 0))
        check("invariant: healthy merchant still settles in the same sweep",
              t_ok.settled_at is not None and moved.get(m2id) == 10000)

    # ── 8. dashboard resend is merchant-scoped ──
    with app.app_context():
        wh2_id = WebhookDelivery.query.filter_by(merchant_id=m2id).first().id
    with c.session_transaction() as s:
        s["_user_id"] = str(mid); s["_fresh"] = True
    r = c.post(f"/account/webhooks/{wh2_id}/resend")
    check("dashboard resend of another merchant's delivery 404s", r.status_code == 404)

    # ── 9. docs pack live ──
    r_md = c.get("/docs.md")
    r_llm = c.get("/docs/llms.txt")
    check("/docs.md + /docs/llms.txt serve the contract",
          r_md.status_code == 200 and r_llm.status_code == 200
          and b"Idempotent-Replayed" in r_md.data
          and b"74.220.48.0/24" in r_md.data
          and b"insufficient_funds" in r_md.data)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL UNDENIABLE-BATCH-1 TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
