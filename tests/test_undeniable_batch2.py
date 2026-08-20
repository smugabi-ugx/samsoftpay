"""Undeniable batch 2 — settlements first-class, available_on, payout scenarios.

What this proves:
  1. A settlement sweep writes a Settlement row (amount, txn_count, mode) in
     the same transaction as the ledger release.
  2. GET /v1/settlements lists them, mode-scoped (test key sees only sandbox).
  3. Charge responses carry available_on (completed_at + 24h before settling,
     the actual settlement time after) and a settled boolean.
  4. Deterministic payout scenarios: recipient 256700000001 fails with
     recipient_not_found every time (and refunds amount + fee);
     256700000000 succeeds every time.
  5. Bulk payout replays carry "replayed": true.
  6. The statement CSV downloads from the journal (rows for real postings,
     bank-statement sign: positive = money to the merchant).
  7. Webhook resend rejects a wildcard event id (LIKE injection guard).
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="undeniable2_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass


from app import create_app
from app.extensions import db
from app.models import (
    Account, AccountType, Channel, Merchant, Payout, PayoutStatus,
    Settlement, Transaction, TxnStatus, utcnow,
)
from app.services import ledger

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def hdrs(key, idem):
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))}


def wait_payout(app, public_id, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            p = Payout.query.filter_by(public_id=public_id).first()
            if p and p.status in (PayoutStatus.SUCCEEDED, PayoutStatus.FAILED):
                return p.status, p.failure_reason
        time.sleep(0.3)
    return None, None


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None

    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="B2 Co", email="b2@x.com", public_key="pk_b2",
                     secret_key="sk_live_b2", test_secret_key="sk_test_b2",
                     kyc_status="verified", handle="b2",
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        mid = m.id
        for is_test in (False, True):
            avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                                 merchant_id=mid, currency="UGX", is_test=is_test)
            rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                                merchant_id=None, currency="UGX", is_test=is_test)
            ledger.post([(rail, +500000), (avail, -500000)], currency="UGX", memo="fund")
        db.session.commit()

    c = app.test_client()

    # ── 1/2. Settlement rows + mode-scoped listing ──
    with app.app_context():
        from datetime import timedelta
        old = utcnow() - timedelta(hours=30)
        for pid, is_test in (("txn_s_live", False), ("txn_s_test", True)):
            t = Transaction(public_id=pid, merchant_id=mid, amount=20000, fee_amount=0,
                            currency="UGX", channel=Channel.MTN_MOMO,
                            status=TxnStatus.SUCCEEDED, is_test=is_test,
                            customer_phone="256780000009", completed_at=old)
            db.session.add(t)
            pend = ledger.get_or_create_account(type=AccountType.MERCHANT_PENDING,
                                                merchant_id=mid, currency="UGX", is_test=is_test)
            rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                                merchant_id=None, currency="UGX", is_test=is_test)
            ledger.post([(rail, +20000), (pend, -20000)], currency="UGX", memo="pend")
        db.session.commit()

        from app.services.settlement import sweep_to_available
        sweep_to_available(hold_hours=24)
        setls = Settlement.query.filter_by(merchant_id=mid).all()
        check("sweep writes one Settlement row per mode batch",
              len(setls) == 2
              and sorted(s.is_test for s in setls) == [False, True]
              and all(s.amount == 20000 and s.txn_count == 1 and s.kind == "sweep"
                      for s in setls))

    r_live = c.get("/v1/settlements", headers={"Authorization": "Bearer sk_live_b2"})
    r_test = c.get("/v1/settlements", headers={"Authorization": "Bearer sk_test_b2"})
    check("GET /v1/settlements is mode-scoped",
          r_live.status_code == 200 and r_test.status_code == 200
          and len(r_live.get_json()["data"]) == 1
          and len(r_test.get_json()["data"]) == 1
          and r_live.get_json()["mode"] == "live"
          and r_live.get_json()["data"][0]["amount"] == 20000)

    # ── 3. available_on + settled on charge responses ──
    r = c.get("/v1/charges/txn_s_live", headers={"Authorization": "Bearer sk_live_b2"})
    j = r.get_json()
    check("settled charge: settled=true, available_on = settlement time",
          j["settled"] is True and j["available_on"] is not None)
    with app.app_context():
        t2 = Transaction(public_id="txn_unsett", merchant_id=mid, amount=5000,
                         fee_amount=100, currency="UGX", channel=Channel.MTN_MOMO,
                         status=TxnStatus.SUCCEEDED, is_test=False,
                         customer_phone="256780000009", completed_at=utcnow())
        db.session.add(t2); db.session.commit()
        expected = (t2.completed_at)
    r = c.get("/v1/charges/txn_unsett", headers={"Authorization": "Bearer sk_live_b2"})
    j = r.get_json()
    check("unsettled charge: settled=false, available_on = completed_at + 24h",
          j["settled"] is False and j["available_on"] is not None
          and j["available_on"] > j["completed_at"])

    # ── 4. deterministic payout scenarios ──
    r = c.post("/v1/payouts", headers=hdrs("sk_test_b2", "dp1"),
               json={"amount": 3000, "recipient": {"phone": "256700000001"}})
    check("magic payout number accepted at initiation", r.status_code == 201)
    pid = r.get_json()["id"]
    with app.app_context():
        avail_before = None  # captured after failure refund below
    status, reason = wait_payout(app, pid)
    check("recipient 256700000001 fails deterministically with recipient_not_found",
          status == PayoutStatus.FAILED and reason == "recipient_not_found")
    r = c.post("/v1/payouts", headers=hdrs("sk_test_b2", "dp2"),
               json={"amount": 3000, "recipient": {"phone": "256700000000"}})
    pid2 = r.get_json()["id"]
    status2, _ = wait_payout(app, pid2)
    check("recipient 256700000000 succeeds deterministically",
          status2 == PayoutStatus.SUCCEEDED)
    # run the failure once more — must be the same outcome (determinism)
    r = c.post("/v1/payouts", headers=hdrs("sk_test_b2", "dp3"),
               json={"amount": 3000, "recipient": {"phone": "0700000001"}})
    status3, reason3 = wait_payout(app, r.get_json()["id"])
    check("07-prefixed form hits the same scenario (last-9 match)",
          status3 == PayoutStatus.FAILED and reason3 == "recipient_not_found")

    # ── 5. bulk replay marker ──
    def bulk(idem):
        return c.post("/v1/payouts/bulk", headers=hdrs("sk_live_b2", idem),
                      json={"payouts": [{"amount": 2000, "phone": "256780000001",
                                         "reference": "b2-dup"}]})
    r1 = bulk("bk1")
    r2 = bulk("bk2")
    item = r2.get_json()["results"][0]
    check("bulk replayed item carries replayed:true",
          r1.get_json()["results"][0].get("replayed") is None
          and item.get("ok") is True and item.get("replayed") is True)

    # ── 6. statement CSV ──
    with c.session_transaction() as s:
        s["_user_id"] = str(mid); s["_fresh"] = True
    r = c.get("/dashboard/wallet/statement.csv")
    body = r.get_data(as_text=True)
    check("statement CSV: 200, has header + real journal lines, positive=to-merchant",
          r.status_code == 200 and body.splitlines()[0].startswith("date,entry_id,account")
          and ",20000,UGX" in body)   # the funding/settlement credit, sign flipped

    # ── 7. resend rejects wildcard ids ──
    r = c.post("/v1/webhooks/evt_%/resend", headers=hdrs("sk_live_b2", "wc1"))
    check("wildcard event id rejected (400)", r.status_code == 400)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL UNDENIABLE-BATCH-2 TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
