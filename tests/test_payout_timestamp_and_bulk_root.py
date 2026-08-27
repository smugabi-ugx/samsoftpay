"""Payout robustness: millisecond X-Timestamp + `items`/`payouts` bulk root.

Run: MOMO_USE_REAL=0 .venv\Scripts\python.exe tests\test_payout_timestamp_and_bulk_root.py

Backbone incident: /v1/payouts rejected their timestamp as 'too far in the
future' (they sent Date.now() milliseconds) while /v1/balance (no replay guard)
appeared to accept it; and bulk used {items:[...]} while the endpoint read
{payouts:[...]}. Both are now tolerated.
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import Merchant


def main():
    app = create_app({"WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="BB", email="b@x.com", public_key="pk",
                     secret_key="sk_live_bbx", handle="bb", kyc_status="verified",
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
    c = app.test_client()
    ms = str(int(time.time() * 1000))   # Date.now() milliseconds
    sec = str(int(time.time()))
    H = {"Authorization": "Bearer sk_live_bbx", "Content-Type": "application/json"}

    # [1] millisecond timestamp is accepted (normalised), not 'too far in the future'
    r = c.post("/v1/payouts", headers={**H, "X-Timestamp": ms, "Idempotency-Key": "k1"},
               json={"amount": 1000, "currency": "UGX", "channel": "mtn_momo",
                     "recipient": {"phone": "256780000001", "name": "x"}, "reference": "BB-1"})
    body = r.get_data(as_text=True)
    assert "too far in the future" not in body, f"[1] ms timestamp rejected: {body}"
    print("[1] PASS — millisecond X-Timestamp accepted (Date.now())")

    # [1b] seconds still work
    r = c.post("/v1/payouts", headers={**H, "X-Timestamp": sec, "Idempotency-Key": "k1b"},
               json={"amount": 1000, "currency": "UGX", "channel": "mtn_momo",
                     "recipient": {"phone": "256780000001"}, "reference": "BB-1b"})
    assert "too far in the future" not in r.get_data(as_text=True), "[1b] seconds broke"
    print("[1b] PASS — seconds X-Timestamp still accepted")

    # [2] bulk accepts {items:[...]}
    r = c.post("/v1/payouts/bulk", headers={**H, "X-Timestamp": ms, "Idempotency-Key": "k2"},
               json={"channel": "mtn_momo", "items": [
                   {"amount": 1000, "recipient": {"phone": "256780000001", "name": "x"}, "reference": "BB-I1"}]})
    b = r.get_data(as_text=True)
    assert "no payout items" not in b and "too far in the future" not in b, f"[2] items root failed: {b}"
    assert json.loads(b)["results"][0]["reference"] == "BB-I1"
    print("[2] PASS — bulk parses {items:[...]}")

    # [2b] bulk still accepts {payouts:[...]}
    r = c.post("/v1/payouts/bulk", headers={**H, "X-Timestamp": sec, "Idempotency-Key": "k3"},
               json={"payouts": [
                   {"amount": 1000, "recipient": {"phone": "256780000002"}, "reference": "BB-P1"}]})
    b = r.get_data(as_text=True)
    assert "no payout items" not in b and json.loads(b)["results"][0]["reference"] == "BB-P1"
    print("[2b] PASS — bulk still parses {payouts:[...]}")

    print("\nALL PAYOUT-ROBUSTNESS ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
