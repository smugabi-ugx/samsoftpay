"""Transactional email via Resend's HTTP API.

The app previously only did SMTP (which Render's egress commonly blocks) with a
console fallback. This wires Resend as the preferred provider. No real email is
sent - requests.post is stubbed.

Proves:
  [1] With RESEND_API_KEY set, send_email POSTs to Resend with the right
      Authorization, from/to/subject/html/text.
  [2] send_otp goes through Resend too and carries the code.
  [3] send_email RAISES on a non-2xx Resend response (callers can react).
  [4] send_otp NEVER raises on a delivery failure (login must not break) - it
      falls back to logging the code.
  [5] _from_address formatting (bare address wrapped; existing display name kept;
      sane default before a domain is verified).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.services import email_service
import requests

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


class _Resp:
    def __init__(self, code=200, text='{"id":"e1"}'):
        self.status_code = code
        self.text = text


def main():
    app = create_app({"WTF_CSRF_ENABLED": False, "RESEND_API_KEY": "re_test_123",
                      "MAIL_FROM": "no-reply@samsoftpay.com", "MAIL_HOST": ""})
    cap = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        cap["url"] = url
        cap["headers"] = headers or {}
        cap["json"] = json or {}
        return _Resp(cap.get("_code", 200))

    orig = requests.post
    requests.post = fake_post
    try:
        with app.app_context():
            # [1] send_email -> Resend
            email_service.send_email("u@x.com", "Hi", "<b>hi</b>", "hi")
            check("[1] send_email POSTs to the Resend API",
                  cap.get("url") == "https://api.resend.com/emails")
            check("[1] with the Bearer API key",
                  cap["headers"].get("Authorization") == "Bearer re_test_123")
            j = cap["json"]
            check("[1] correct to/subject/from/html/text",
                  j.get("to") == ["u@x.com"] and j.get("subject") == "Hi"
                  and j.get("from") == "Samsoftpay <no-reply@samsoftpay.com>"
                  and j.get("html") == "<b>hi</b>" and j.get("text") == "hi")

            # [2] send_otp -> Resend, carries the code
            cap.clear()
            email_service.send_otp("u@x.com", "123456", "login")
            check("[2] send_otp POSTs to Resend", cap.get("url") == "https://api.resend.com/emails")
            check("[2] the OTP is in the email body", "123456" in cap["json"].get("html", ""))

            # [3] send_email raises on non-2xx
            cap["_code"] = 422
            raised = False
            try:
                email_service.send_email("u@x.com", "S", "<b>x</b>", "p")
            except Exception:
                raised = True
            check("[3] send_email raises on a Resend error response", raised)

            # [4] send_otp never raises on failure
            raised = False
            try:
                email_service.send_otp("u@x.com", "999999", "login")
            except Exception:
                raised = True
            check("[4] send_otp does NOT raise on a delivery failure", not raised)
            cap.pop("_code", None)
    finally:
        requests.post = orig

    # [5] _from_address formatting
    with app.app_context():
        app.config["MAIL_FROM"] = "ops@samsoftpay.com"
        check("[5] bare address is wrapped with the display name",
              email_service._from_address() == "Samsoftpay <ops@samsoftpay.com>")
        app.config["MAIL_FROM"] = "Support <help@samsoftpay.com>"
        check("[5] an existing display name is kept as-is",
              email_service._from_address() == "Support <help@samsoftpay.com>")
        app.config["MAIL_FROM"] = ""
        app.config["MAIL_USERNAME"] = ""
        check("[5] sane default before a domain is verified",
              email_service._from_address() == "Samsoftpay <onboarding@resend.dev>")

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL RESEND EMAIL TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
