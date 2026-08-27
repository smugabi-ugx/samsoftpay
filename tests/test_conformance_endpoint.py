"""POST /v1/vending/conformance — the self-serve signature certification gate.

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_conformance_endpoint.py

A machine vendor posts a sample signed callback; we report whether it verifies
against the merchant's signing profile. Moves NO money, changes NO state.
  [1] a correctly-signed sample -> {ok: true}
  [2] a wrongly-signed sample   -> {ok: false, expected_reqData_any_of: [...]}
  [3] missing payload/sign      -> 400
  [4] merchant with no vendor secret -> 400 (nothing to verify against)
"""
import hashlib
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
from app.services.secrets_box import encrypt

SECRET = "vendor-secret-xyz"


def main():
    app = create_app({"WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="TK", email="tk@x.com", public_key="pk",
                     secret_key="sk_test_tk", handle="tk", kyc_status="verified",
                     vending_enabled=True, xy_key="k1",
                     xy_secret_encrypted=encrypt(SECRET),
                     password_hash=generate_password_hash("x"))
        db.session.add(m)
        # A merchant with NO vendor secret (for [4]).
        n = Merchant(name="NoSec", email="no@x.com", public_key="pk2",
                     secret_key="sk_test_nosec", handle="nosec", kyc_status="verified",
                     password_hash=generate_password_hash("x"))
        db.session.add(n)
        db.session.commit()

    c = app.test_client()
    ts13 = str(int(time.time() * 1000))
    payload = {
        "jqbh": "XY1", "ddbh": "vnd_1", "dsfjybh": "txn_1", "status": "1",
        "tkje": "0", "tksj": "0", "timestamp": ts13,
        "splist": [{"chsl": "1", "spbh": "0001"}],
    }
    # The vendor's firmware equivalent: MD5(secret + timestamp + alphabetical reqData)
    scalars = {k: v for k, v in payload.items()
               if k not in ("splist", "timestamp", "sign", "key")}
    base = "&".join(f"{k}={scalars[k]}" for k in sorted(scalars))
    good_sign = hashlib.md5(f"{SECRET}{ts13}{base}".encode()).hexdigest()

    H = {"Authorization": "Bearer sk_test_tk", "Content-Type": "application/json",
         "X-Timestamp": str(int(time.time()))}

    # [1] correct signature
    r = c.post("/v1/vending/conformance", headers=H,
               json={"payload": payload, "sign": good_sign})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True, f"[1] expected ok:true, got {r.status_code} {j}"
    assert j["vendor"] == "xy", f"[1] vendor should be xy: {j}"
    print("[1] PASS — a correctly-signed sample verifies (ok:true, vendor=xy)")

    # [2] wrong signature -> ok:false with the accepted bases shown
    r = c.post("/v1/vending/conformance", headers=H,
               json={"payload": payload, "sign": "deadbeef"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is False, f"[2] expected ok:false: {j}"
    assert j.get("expected_reqData_any_of"), f"[2] should list expected bases: {j}"
    assert any("ddbh=vnd_1" in b for b in j["expected_reqData_any_of"]), "[2] base should contain fields"
    print("[2] PASS — a wrong signature returns ok:false + the expected reqData bases")

    # [3] missing sign -> 400
    r = c.post("/v1/vending/conformance", headers=H, json={"payload": payload})
    assert r.status_code == 400, f"[3] missing sign should 400, got {r.status_code}"
    print("[3] PASS — missing payload/sign -> 400")

    # [4] merchant with no vendor secret -> 400
    H2 = {"Authorization": "Bearer sk_test_nosec", "Content-Type": "application/json",
          "X-Timestamp": str(int(time.time()))}
    r = c.post("/v1/vending/conformance", headers=H2,
               json={"payload": payload, "sign": good_sign})
    assert r.status_code == 400, f"[4] no-secret merchant should 400, got {r.status_code} {r.get_data(as_text=True)}"
    print("[4] PASS — a merchant with no vendor secret -> 400")

    print("\nALL CONFORMANCE-ENDPOINT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
