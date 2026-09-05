"""Email sending — OTPs and notifications.

If MAIL_HOST is not configured the code falls back to printing the OTP
to the console (dev/local only). In production MAIL_HOST must be set.
"""
import secrets
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app


def _from_address() -> str:
    """The sender address, formatted for both Resend and SMTP.

    Resend requires the `from` to be on a domain you've verified in Resend; set
    MAIL_FROM to e.g. `no-reply@samsoftpay.com` once the domain is connected.
    A bare address is wrapped with the Samsoftpay display name; an address that
    already carries a display name (`Name <addr>`) is used as-is."""
    raw = (current_app.config.get("MAIL_FROM", "")
           or current_app.config.get("MAIL_USERNAME", "")).strip()
    if not raw:
        # Resend's shared sandbox sender — works before a domain is verified, but
        # only delivers to the account owner's own address. Replace via MAIL_FROM.
        return "Samsoftpay <onboarding@resend.dev>"
    return raw if "<" in raw else f"Samsoftpay <{raw}>"


def _send_via_resend(to_email: str, subject: str, html: str, plain: str | None) -> None:
    """Deliver one email through Resend's HTTP API. Raises on a non-2xx response.

    Preferred over SMTP on Render, whose network egress commonly blocks outbound
    SMTP ports (25/465/587) — the HTTP API is unaffected. Enabled by setting
    RESEND_API_KEY; the sender comes from MAIL_FROM (a Resend-verified domain)."""
    import requests
    key = current_app.config.get("RESEND_API_KEY", "")
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "from": _from_address(),
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": plain or "",
        },
        timeout=15,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend API {resp.status_code}: {resp.text[:300]}")


def send_email(to_email: str, subject: str, html: str, plain: str | None = None) -> None:
    """Send a general HTML email over the same SMTP config the OTP mailer uses.

    Raises on delivery failure (callers that must not be disrupted — e.g. alerts —
    wrap this). Prefers Resend (RESEND_API_KEY) over SMTP (MAIL_HOST); no-op with
    a console print when neither is configured (dev/local), matching send_otp.
    """
    if current_app.config.get("RESEND_API_KEY", ""):
        _send_via_resend(to_email, subject, html, plain)
        return
    host = current_app.config.get("MAIL_HOST", "")
    if not host:
        print(f"\n[DEV EMAIL] To: {to_email} | Subject: {subject}\n{plain or ''}\n", flush=True)
        return

    port      = int(current_app.config.get("MAIL_PORT", 587))
    username  = current_app.config.get("MAIL_USERNAME", "")
    password  = current_app.config.get("MAIL_PASSWORD", "").replace(" ", "")
    from_addr = current_app.config.get("MAIL_FROM", username)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Samsoftpay <{from_addr}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(plain or "", "plain"))
    msg.attach(MIMEText(html, "html"))

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
    if username and password:
        server.login(username, password)
    server.sendmail(from_addr, to_email, msg.as_string())
    server.quit()


def _log_otp_fallback(to_email: str, otp: str, purpose: str) -> None:
    """Break-glass: print the OTP to the server log when email delivery FAILS,
    so an email outage can never lock an operator out of their own account.

    Called ONLY on a delivery failure — never in normal operation — so it is a
    fallback, not a standing leak. Retrieve it from Render -> Logs (grep
    'OTP FALLBACK'). Gated by config OTP_LOG_ON_FAILURE (default True)."""
    print(f"\n{'='*60}", flush=True)
    print(f"  [OTP FALLBACK] email delivery failed — code logged so you can still sign in", flush=True)
    print(f"  [OTP FALLBACK] To:      {to_email}", flush=True)
    print(f"  [OTP FALLBACK] Code:    {otp}", flush=True)
    print(f"  [OTP FALLBACK] Purpose: {purpose}", flush=True)
    print(f"{'='*60}\n", flush=True)
    sys.stdout.flush()


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def otp_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=10)


def send_otp(to_email: str, otp: str, purpose: str = "verification") -> None:
    """Send a 6-digit OTP. Console fallback ONLY when MAIL_HOST is not set."""
    subjects = {
        "verification": "Verify your Samsoftpay account",
        "login":        "Your Samsoftpay login code",
        "transaction":  "Samsoftpay transaction confirmation code",
        "reset":        "Reset your Samsoftpay password",
    }
    subject = subjects.get(purpose, "Your Samsoftpay security code")

    html = f"""
<div style="font-family:Inter,sans-serif;max-width:480px;margin:0 auto;padding:2rem;">
  <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:12px;padding:2rem;text-align:center;margin-bottom:1.5rem;">
    <h1 style="color:white;margin:0;font-size:1.5rem;">Samsoftpay</h1>
  </div>
  <h2 style="color:#0f172a;">Your verification code</h2>
  <p style="color:#475569;">Use the code below. It expires in <strong>10 minutes</strong>.</p>
  <div style="background:#f1f5f9;border-radius:8px;padding:1.5rem;text-align:center;margin:1.5rem 0;">
    <span style="font-size:2.5rem;font-weight:700;letter-spacing:0.3em;color:#6366f1;font-family:monospace;">{otp}</span>
  </div>
  <p style="color:#94a3b8;font-size:0.875rem;">If you didn't request this code, you can safely ignore this email.</p>
</div>
"""
    plain = f"Your Samsoftpay code: {otp}\n\nExpires in 10 minutes. Do not share it."

    resend_key = current_app.config.get("RESEND_API_KEY", "")
    host = current_app.config.get("MAIL_HOST", "")
    if not resend_key and not host:
        # Dev mode — no email provider configured (Resend or SMTP)
        print(f"\n{'='*55}", flush=True)
        print(f"  [DEV OTP]  To: {to_email}", flush=True)
        print(f"  [DEV OTP]  Code: {otp}", flush=True)
        print(f"  [DEV OTP]  Purpose: {purpose}", flush=True)
        print(f"{'='*55}\n", flush=True)
        sys.stdout.flush()
        return

    try:
        if resend_key:
            # Preferred: Resend HTTP API (unaffected by Render's SMTP egress block).
            _send_via_resend(to_email, subject, html, plain)
        else:
            port      = int(current_app.config.get("MAIL_PORT", 587))
            username  = current_app.config.get("MAIL_USERNAME", "")
            password  = current_app.config.get("MAIL_PASSWORD", "").replace(" ", "")
            from_addr = current_app.config.get("MAIL_FROM", username)
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"Samsoftpay <{from_addr}>"
            msg["To"]      = to_email
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html,  "html"))
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                server = smtplib.SMTP(host, port, timeout=15)
                server.ehlo()
                server.starttls()
                server.ehlo()
            if username and password:
                server.login(username, password)
            server.sendmail(from_addr, to_email, msg.as_string())
            server.quit()
        print(f"[MAIL] OTP sent to {to_email} via "
              f"{'resend' if resend_key else 'smtp'}", flush=True)
        sys.stdout.flush()

    except Exception as exc:
        # LOGIN FALLBACK — email delivery must NEVER lock anyone out. The OTP is
        # already stored server-side; if delivery fails we surface it in the
        # server logs (break-glass) and DO NOT raise, so a mail outage cannot
        # 500 the login flow — the user still reaches the code-entry page and an
        # operator can read the code from Render -> Logs. This logs the code
        # ONLY on a delivery failure, never in normal operation.
        hint = ""
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            hint = (" — auth failed: check MAIL_USERNAME/MAIL_PASSWORD "
                    "(Gmail needs an App Password; Resend SMTP username is 'resend')")
        print(f"[MAIL ERROR] OTP delivery to {to_email} failed: {exc}{hint}", flush=True)
        if current_app.config.get("OTP_LOG_ON_FAILURE", True):
            _log_otp_fallback(to_email, otp, purpose)
        sys.stdout.flush()
        # Intentionally NO re-raise. Delivery is best-effort; the login flow
        # must proceed to the code-entry step regardless of a mail outage.
