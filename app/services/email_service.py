"""Email sending — OTPs and notifications.

If MAIL_HOST is not configured the code falls back to printing the OTP
to the console, which is perfect for local development.
"""
import random
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app


def generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"


def otp_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def send_otp(to_email: str, otp: str, purpose: str = "verification") -> None:
    """Send a 6-digit OTP. Console fallback when MAIL_HOST is not set."""
    subjects = {
        "verification": "Verify your Samsoftpay account",
        "login":        "Your Samsoftpay login code",
        "transaction":  "Samsoftpay transaction confirmation code",
    }
    subject = subjects.get(purpose, "Your Samsoftpay security code")

    html = f"""
<div style="font-family:Inter,sans-serif;max-width:480px;margin:0 auto;padding:2rem;">
  <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:12px;padding:2rem;text-align:center;margin-bottom:1.5rem;">
    <h1 style="color:white;margin:0;font-size:1.5rem;">Samsoftpay</h1>
  </div>
  <h2 style="color:#0f172a;">Your verification code</h2>
  <p style="color:#475569;">Use the code below to complete your action. It expires in <strong>10 minutes</strong>.</p>
  <div style="background:#f1f5f9;border-radius:8px;padding:1.5rem;text-align:center;margin:1.5rem 0;">
    <span style="font-size:2.5rem;font-weight:700;letter-spacing:0.3em;color:#6366f1;font-family:monospace;">{otp}</span>
  </div>
  <p style="color:#94a3b8;font-size:0.875rem;">If you didn't request this code, you can safely ignore this email.</p>
</div>
"""
    plain = f"Your Samsoftpay code: {otp}\n\nExpires in 10 minutes. Do not share it."

    host = current_app.config.get("MAIL_HOST")
    if not host:
        print(f"\n{'='*55}")
        print(f"  [DEV EMAIL]  To: {to_email}")
        print(f"  [DEV EMAIL]  Subject: {subject}")
        print(f"  [DEV EMAIL]  OTP: {otp}")
        print(f"{'='*55}\n")
        return

    port = int(current_app.config.get("MAIL_PORT", 587))
    username = current_app.config.get("MAIL_USERNAME", "")
    password = current_app.config.get("MAIL_PASSWORD", "")
    from_addr = current_app.config.get("MAIL_FROM", username)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Samsoftpay <{from_addr}>"
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            if port != 465:
                server.starttls()
            if username:
                server.login(username, password)
            server.sendmail(from_addr, to_email, msg.as_string())
    except Exception as exc:
        print(f"[EMAIL ERROR] Failed to send to {to_email}: {exc} — OTP was: {otp}")
