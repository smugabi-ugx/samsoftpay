"""POST /v1/vending/dispense: the 'no payment, no dispense' gate (guardrail 11).

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_vending_dispense_guard.py

Regression for a CRITICAL free-vending exploit found by an audit: charge_id used
to be OPTIONAL, and the SUCCEEDED-charge check + the atomic single-use consume
were BOTH wrapped in `if charge_id:`. Omitting charge_id skipped ALL payment
verification and called the supplier directly -> a real product dispensed for
free, unlimited times (a decompiled collections key on a kiosk could drain a
machine). charge_id is now mandatory. This proves:

  [1] no charge_id            -> 400, supplier NEVER called (the exploit is shut)
  [2] valid SUCCEEDED charge  -> 200, supplier called exactly once
  [3] same charge again       -> 409 (one charge pays for one dispense)
  [4] non-succeeded charge    -> 400, supplier NEVER called
  [5] unknown charge_id       -> 404, supplier NEVER called
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from app import create_app
from app.extensions import db
from app.models import Merchant, Transaction, TxnStatus, Channel, utcnow
from app.services import xy_vending

# Record supplier calls; never touch XY's cloud.
CALLS = []


def fake_apply_export_goods(**kwargs):
    CALLS.append(kwargs)
    return {"code": "1", "message": "ok"}


def _mk_txn(merchant_id, public_id, status, is_test=False):
    return Transaction(
        public_id=public_id, merchant_id=merchant_id, amount=2500, fee_amount=100,
        currency="UGX", channel=Channel.MTN_MOMO, status=status, is_test=is_test,
        customer_phone="256780000001", completed_at=utcnow())


def main():
    # The endpoint imports xy_vending lazily and calls xy_vending.apply_export_goods,
    # so patching the module attribute is enough.
    xy_vending.apply_export_goods = fake_apply_export_goods

    app = create_app({"WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="TK", email="tk@x.com", public_key="pk_v",
                     secret_key="sk_live_v", kyc_status="verified",
                     vending_enabled=True)
        db.session.add(m); db.session.commit()
        db.session.add(_mk_txn(m.id, "txn_ok", TxnStatus.SUCCEEDED))
        db.session.add(_mk_txn(m.id, "txn_pending", TxnStatus.AUTHORIZED))
        db.session.commit()

    c = app.test_client()
    H = {"Authorization": "Bearer sk_live_v", "Content-Type": "application/json",
         "X-Timestamp": str(int(time.time()))}
    goods = [{"spbh": "0001", "spmc": "Coke", "spdj": 2500}]

    # [1] THE EXPLOIT: no charge_id must be REFUSED before any supplier call.
    CALLS.clear()
    r = c.post("/v1/vending/dispense", headers=H,
               json={"machine": "XY000123", "order_id": "free-1", "goods": goods})
    assert r.status_code == 400, f"[1] no-charge_id should 400, got {r.status_code}: {r.data}"
    assert b"charge_id" in r.data, f"[1] error should name charge_id: {r.data}"
    assert len(CALLS) == 0, f"[1] supplier was called with NO payment! {CALLS}"
    print("[1] PASS — dispense with no charge_id refused (400), supplier never called")

    # [2] A real SUCCEEDED charge dispenses, once.
    CALLS.clear()
    r = c.post("/v1/vending/dispense", headers=H,
               json={"machine": "XY000123", "order_id": "o-2", "goods": goods,
                     "charge_id": "txn_ok"})
    assert r.status_code == 200, f"[2] valid charge should 200: {r.status_code} {r.data}"
    assert len(CALLS) == 1, f"[2] expected exactly one supplier call, got {len(CALLS)}"
    assert CALLS[0]["third_party_txn_id"] == "txn_ok", "[2] must sign with the charge id"
    print("[2] PASS — valid SUCCEEDED charge dispenses exactly once")

    # [3] The SAME charge cannot pay for a second dispense.
    CALLS.clear()
    r = c.post("/v1/vending/dispense", headers=H,
               json={"machine": "XY000123", "order_id": "o-3", "goods": goods,
                     "charge_id": "txn_ok"})
    assert r.status_code == 409, f"[3] reused charge should 409: {r.status_code} {r.data}"
    assert len(CALLS) == 0, f"[3] reused charge dispensed again! {CALLS}"
    print("[3] PASS — one charge pays for one dispense (reuse -> 409)")

    # [4] A charge that never succeeded cannot dispense.
    CALLS.clear()
    r = c.post("/v1/vending/dispense", headers=H,
               json={"machine": "XY000123", "order_id": "o-4", "goods": goods,
                     "charge_id": "txn_pending"})
    assert r.status_code == 400, f"[4] non-succeeded should 400: {r.status_code} {r.data}"
    assert len(CALLS) == 0, f"[4] non-succeeded charge dispensed! {CALLS}"
    print("[4] PASS — non-succeeded charge refused (400), supplier never called")

    # [5] An unknown charge id is a 404, not a dispense.
    CALLS.clear()
    r = c.post("/v1/vending/dispense", headers=H,
               json={"machine": "XY000123", "order_id": "o-5", "goods": goods,
                     "charge_id": "txn_nope"})
    assert r.status_code == 404, f"[5] unknown charge should 404: {r.status_code} {r.data}"
    assert len(CALLS) == 0, f"[5] unknown charge dispensed! {CALLS}"
    print("[5] PASS — unknown charge refused (404), supplier never called")

    print("\nALL VENDING-DISPENSE-GUARD ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
