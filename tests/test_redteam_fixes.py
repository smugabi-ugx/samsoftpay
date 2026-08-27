"""Regressions for two red-team findings (adversarial product test).

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_redteam_fixes.py

[1] success_url scheme guard: a `javascript:` success_url on POST /v1/vending/orders
    is rejected 400 (was stored -> reflected into the checkout page as XSS). https ok.
[2] bulk payout transient failure: a non-PayoutError exception (e.g.
    DisbursementUnavailable) no longer 500s the whole batch and no longer wedges the
    reference IN_FLIGHT forever — the item is retryable and the reservation released.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import Merchant
from app.services import payouts as payouts_mod


def main():
    app = create_app({"WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="K", email="k@x.com", public_key="pk", secret_key="sk_live_k",
                     handle="k", kyc_status="verified", vending_enabled=True,
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()

    c = app.test_client()
    H = {"Authorization": "Bearer sk_live_k", "Content-Type": "application/json",
         "X-Timestamp": str(int(time.time()))}

    # [1] javascript: success_url rejected
    r = c.post("/v1/vending/orders", headers={**H, "Idempotency-Key": "v1"},
               json={"machine": "XY1", "amount": 2000, "goods": [{"spbh": "0001"}],
                     "success_url": "javascript:alert(document.cookie)"})
    assert r.status_code == 400, f"[1a] js success_url should 400, got {r.status_code}: {r.data}"
    assert b"success_url" in r.data, f"[1a] error should name success_url: {r.data}"
    print("[1a] PASS — javascript: success_url rejected (400)")

    # ...and a normal https success_url is accepted
    r = c.post("/v1/vending/orders", headers={**H, "Idempotency-Key": "v2"},
               json={"machine": "XY1", "amount": 2000, "goods": [{"spbh": "0001"}],
                     "success_url": "https://shop.example/thanks"})
    assert r.status_code == 201, f"[1b] https success_url should 201, got {r.status_code}: {r.data}"
    print("[1b] PASS — https success_url accepted (201)")

    # [2] bulk payout: force a transient non-PayoutError failure
    real_create = payouts_mod.create_payout

    def boom(**kwargs):
        raise RuntimeError("disbursement adapter momentarily unavailable")
    payouts_mod.create_payout = boom
    try:
        r = c.post("/v1/payouts/bulk", headers={**H, "Idempotency-Key": "b1"},
                   json={"channel": "mtn_momo", "payouts": [
                       {"amount": 1000, "recipient": {"phone": "256780000001"}, "reference": "BB-T1"}]})
        assert r.status_code == 200, f"[2a] batch must not 500 on transient error, got {r.status_code}"
        item = r.get_json()["results"][0]
        assert item["ok"] is False and item.get("retryable") is True, f"[2a] expected retryable failure: {item}"
        print("[2a] PASS — transient failure -> 200 batch, item retryable (no 500)")
    finally:
        payouts_mod.create_payout = real_create

    # The reference must NOT be wedged IN_FLIGHT: a retry attempts again (here it
    # reaches the balance check and fails 'insufficient', NOT 'still in flight').
    r = c.post("/v1/payouts/bulk", headers={**H, "Idempotency-Key": "b2"},
               json={"channel": "mtn_momo", "payouts": [
                   {"amount": 1000, "recipient": {"phone": "256780000001"}, "reference": "BB-T1"}]})
    item = r.get_json()["results"][0]
    assert "still in flight" not in (item.get("error") or ""), \
        f"[2b] reference wedged IN_FLIGHT after transient failure: {item}"
    print("[2b] PASS — reference not wedged; retry is attempted (reservation released)")

    print("\nALL RED-TEAM-FIX ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
