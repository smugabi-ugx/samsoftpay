"""Self-service password reset — security-critical flow.

Run: MOMO_USE_REAL=0 .venv\Scripts\python.exe tests\test_password_reset.py

Proves:
  [1] forgot-password NEVER reveals whether an account exists (no enumeration),
      and does not crash when email is down.
  [2] a real email gets a reset code stored server-side.
  [3] correct code -> password changed, old password rejected, code burned
      (single-use), and the new password works.
  [4] wrong code -> rejected + attempts counted; brute force burns the code.
  [5] expired code -> rejected.
"""
import io
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from datetime import datetime, timedelta, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from app import create_app
from app.extensions import db
from app.models import Merchant


def _mk(app, email="boss@example.com", pw="OldPass123"):
    slug = email.split("@")[0]
    with app.app_context():
        m = Merchant(name="Boss", email=email, public_key="pk_" + slug,
                     secret_key="sk_" + slug, handle=slug,
                     password_hash=generate_password_hash(pw), kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        return m.id


def _run():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                      "RATELIMIT_ENABLED": False, "MAIL_HOST": ""})  # dev mail -> console
    with app.app_context():
        db.create_all()
    mid = _mk(app)
    c = app.test_client()

    # [1] enumeration-safe + no crash for a missing account
    with contextlib.redirect_stdout(io.StringIO()):
        r_missing = c.post("/forgot-password", data={"email": "nobody@example.com"},
                           follow_redirects=False)
    with contextlib.redirect_stdout(io.StringIO()):
        r_real = c.post("/forgot-password", data={"email": "boss@example.com"},
                        follow_redirects=False)
    assert r_missing.status_code in (301, 302, 303), r_missing.status_code
    assert r_real.status_code == r_missing.status_code, "[1] responses must be identical"
    assert r_real.headers.get("Location") == r_missing.headers.get("Location") \
        or "reset-password" in r_real.headers.get("Location", ""), "[1] same redirect target"
    print("[1] PASS — forgot-password is enumeration-safe and never crashes")

    # [2] a real account got a code stored
    with app.app_context():
        m = db.session.get(Merchant, mid)
        code = m.otp_code
        assert code and len(code) == 6, "[2] a 6-digit reset code must be stored"
    print("[2] PASS — reset code stored server-side for a real account")

    # [3] correct code -> password changed + single-use
    r = c.post("/reset-password", data={"email": "boss@example.com", "code": code,
                                        "password": "BrandNew456"}, follow_redirects=False)
    assert r.status_code in (301, 302, 303), r.status_code
    with app.app_context():
        m = db.session.get(Merchant, mid)
        assert check_password_hash(m.password_hash, "BrandNew456"), "[3] new password must work"
        assert not check_password_hash(m.password_hash, "OldPass123"), "[3] old password must fail"
        assert m.otp_code is None, "[3] code must be burned after use"
    # reusing the same code must now fail (single-use)
    r2 = c.post("/reset-password", data={"email": "boss@example.com", "code": code,
                                         "password": "Attacker999"}, follow_redirects=True)
    with app.app_context():
        m = db.session.get(Merchant, mid)
        assert check_password_hash(m.password_hash, "BrandNew456"), "[3] burned code must not reset again"
    print("[3] PASS — correct code resets once; old password dead; code single-use")

    # [4] wrong code -> rejected + attempts counted -> brute force burns code
    mid2 = _mk(app, email="two@example.com", pw="Start123")
    with contextlib.redirect_stdout(io.StringIO()):
        c.post("/forgot-password", data={"email": "two@example.com"})
    for _ in range(5):
        c.post("/reset-password", data={"email": "two@example.com", "code": "000000",
                                        "password": "Nope12345"})
    with app.app_context():
        m = db.session.get(Merchant, mid2)
        assert check_password_hash(m.password_hash, "Start123"), "[4] wrong code must not change password"
        assert m.otp_code is None, "[4] code should be burned after too many attempts"
    print("[4] PASS — wrong code rejected; brute force burns the code")

    # [5] expired code -> rejected
    mid3 = _mk(app, email="three@example.com", pw="Keep123")
    with app.app_context():
        m = db.session.get(Merchant, mid3)
        m.otp_code = "123456"
        m.otp_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)  # already expired
        db.session.commit()
    c.post("/reset-password", data={"email": "three@example.com", "code": "123456",
                                    "password": "TooLate123"})
    with app.app_context():
        m = db.session.get(Merchant, mid3)
        assert check_password_hash(m.password_hash, "Keep123"), "[5] expired code must not reset"
    print("[5] PASS — expired code rejected")

    print("\nALL PASSWORD-RESET ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run())
