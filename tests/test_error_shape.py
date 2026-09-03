"""Every /v1 error carries a stable machine-readable `code` and a `request_id`.

Before this, integrators had to substring-match the human `error` prose to tell
insufficient-balance from bad-request from rate-limit, and the OpenAPI Error
schema advertised a request_id the body never returned. Additive: `error` stays.
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="err_shape_")
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
from app.models import Merchant

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="E", email="e@x.com", public_key="pk_test_e",
                     secret_key="sk_live_e", test_secret_key="sk_test_ecode",
                     kyc_status="verified")
        db.session.add(m); db.session.commit()

    c = app.test_client()

    r = c.get("/v1/charges")
    j = r.get_json() or {}
    check("401 unauth -> code=unauthorized", r.status_code == 401 and j.get("code") == "unauthorized")
    check("  human error preserved", bool(j.get("error")))
    check("  request_id echoed in body", bool(j.get("request_id")))
    check("  request_id matches X-Request-ID header", j.get("request_id") == r.headers.get("X-Request-ID"))

    H = {"Authorization": "Bearer sk_test_ecode", "X-Timestamp": str(int(time.time()))}
    r = c.get("/v1/charges/txn_bogus", headers=H)
    j = r.get_json() or {}
    check("404 -> code=not_found", r.status_code == 404 and j.get("code") == "not_found")

    with app.test_request_context("https://api.samsoftpay.com/"):
        from app.services.openapi_spec import build_openapi
        spec = build_openapi("https://api.samsoftpay.com/")
        errprops = spec["components"]["schemas"]["Error"]["properties"]
        check("OpenAPI Error schema documents 'code'", "code" in errprops)
        payparams = [q.get("name") for q in spec["paths"]["/v1/payouts"]["get"]["parameters"]]
        check("OpenAPI /payouts documents starting_after cursor", "starting_after" in payparams)

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
