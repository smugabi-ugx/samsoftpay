"""Login fallback — an email outage must NEVER lock an operator out.

Run: MOMO_USE_REAL=0 .venv\Scripts\python.exe tests\test_login_fallback.py

Proves:
  [1] send_otp does NOT raise when SMTP delivery fails, and surfaces the code to
      the logs (break-glass) so it stays retrievable.
  [2] the full POST /login flow reaches the 2FA code-entry step (no 500) even
      when email is completely down, with the OTP stored server-side.
  [3] normal operation (no MAIL_HOST) is unchanged — code still printed, no raise.
"""
import io
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"          # SET, never pop() (dotenv would refill from .env)
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import Merchant
from app.services.email_service import send_otp


def _run():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})

    # ---- [1] send_otp is fail-safe when SMTP is dead ----
    with app.app_context():
        # An unreachable SMTP host -> connection refused inside send_otp.
        app.config["MAIL_HOST"] = "127.0.0.1"
        app.config["MAIL_PORT"] = 2   # nothing listening -> OSError
        app.config["MAIL_USERNAME"] = "x"
        app.config["MAIL_PASSWORD"] = "y"
        app.config["OTP_LOG_ON_FAILURE"] = True

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                send_otp("boss@example.com", "654321", purpose="login")
        except Exception as exc:  # noqa: BLE001
            print(f"[1] FAIL — send_otp raised on SMTP failure: {exc!r}")
            return 1
        out = buf.getvalue()
        assert "OTP FALLBACK" in out, "[1] expected break-glass log on failure"
        assert "654321" in out, "[1] the code must be retrievable from the logs"
        print("[1] PASS — mail outage: send_otp did not raise; code surfaced to logs")

    # ---- [2] the login flow survives a mail outage (no 500) ----
    with app.app_context():
        db.create_all()
        m = Merchant(
            name="Boss", email="boss@example.com", public_key="pk", secret_key="sk",
            handle="boss", password_hash=generate_password_hash("StrongPass1"),
            two_fa_enabled=True, email_verified=True, kyc_status="verified",
        )
        db.session.add(m)
        db.session.commit()

    client = app.test_client()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        resp = client.post("/login", data={"email": "boss@example.com",
                                            "password": "StrongPass1"},
                           follow_redirects=False)
    assert resp.status_code in (302, 303), \
        f"[2] login should redirect to 2FA, not error — got {resp.status_code}"
    assert "verify-2fa" in resp.headers.get("Location", ""), \
        f"[2] expected redirect to verify-2fa, got {resp.headers.get('Location')}"
    with app.app_context():
        stored = db.session.get(Merchant, 1)
        assert stored.otp_code, "[2] OTP must be stored so the logged code can be entered"
    assert "OTP FALLBACK" in buf.getvalue(), "[2] the login code must hit the logs when mail is down"
    print("[2] PASS — mail down: /login reached 2FA (no 500); code stored + logged")

    # ---- [3] dev mode (no MAIL_HOST) unchanged ----
    with app.app_context():
        app.config["MAIL_HOST"] = ""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                send_otp("dev@example.com", "111222", purpose="login")
        except Exception as exc:  # noqa: BLE001
            print(f"[3] FAIL — send_otp raised in dev mode: {exc!r}")
            return 1
        assert "111222" in buf.getvalue(), "[3] dev-mode console print must still work"
        print("[3] PASS — dev mode (no SMTP) unchanged")

    print("\nALL LOGIN-FALLBACK ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run())
