"""Merchant auth: signup, login, logout, email verification, 2FA."""
import hmac
import re
import secrets
from urllib.parse import urlparse

from flask import (
    Blueprint, flash, redirect, render_template,
    request, session, url_for,
)
from flask_login import (
    current_user, login_required, login_user, logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db, limiter
from ..models import Merchant
from ..services.email_service import generate_otp, otp_expiry, send_otp
from ..utils import verified_required

bp = Blueprint("auth", __name__)

_PENDING_2FA_KEY = "_pending_2fa_id"


def _safe_redirect(next_url: str | None, fallback: str) -> str:
    """Return next_url only if it is a relative path (no host) to prevent open redirect."""
    if next_url and urlparse(next_url).netloc == "":
        return next_url
    return fallback


def _otp_matches(stored: str, provided: str) -> bool:
    """Constant-time OTP comparison to prevent timing attacks."""
    return hmac.compare_digest(stored, provided)


def _make_handle(name: str) -> str:
    """Turn 'Acme Traders Ltd' into a unique 'acme-traders' handle."""
    base = re.sub(r"[^\w\s]", "", name.lower())
    base = re.sub(r"\s+", "-", base.strip())[:30].rstrip("-") or "merchant"
    handle, i = base, 1
    while Merchant.query.filter_by(handle=handle).first():
        handle = f"{base}-{i}"
        i += 1
    return handle


# ---------- signup ----------

@bp.get("/signup")
def signup_page():
    if current_user.is_authenticated:
        return redirect(url_for("auth.account"))
    return render_template("signup.html")


@bp.post("/signup")
@limiter.limit("5 per minute")
def signup():
    name        = request.form.get("name", "").strip()
    email       = request.form.get("email", "").strip().lower()
    password    = request.form.get("password", "")
    raw_webhook = request.form.get("webhook_url", "").strip()
    if raw_webhook and not re.match(r"^https?://", raw_webhook):
        raw_webhook = ""   # reject non-http(s) URLs — prevents SSRF
    webhook_url = raw_webhook or None

    error = None
    if not name:
        error = "Business name is required."
    elif not email or "@" not in email:
        error = "A valid email address is required."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif Merchant.query.filter_by(email=email).first():
        error = "An account with this email already exists."

    if error:
        return render_template("signup.html", error=error, form=request.form)

    raw_handle = request.form.get("handle", "").strip().lower()
    raw_handle = re.sub(r"[^\w-]", "", raw_handle)[:30]

    otp = generate_otp()
    merchant = Merchant(
        name=name,
        email=email,
        password_hash=generate_password_hash(password),
        public_key="pk_live_" + secrets.token_urlsafe(20),
        secret_key="sk_live_" + secrets.token_urlsafe(28),
        test_public_key="pk_test_" + secrets.token_urlsafe(20),
        test_secret_key="sk_test_" + secrets.token_urlsafe(28),
        kyc_status="pending",
        webhook_url=webhook_url,
        handle=raw_handle if raw_handle else _make_handle(name),
        email_verified=False,
        otp_code=otp,
        otp_expires_at=otp_expiry(),
    )
    db.session.add(merchant)
    db.session.commit()

    send_otp(email, otp, purpose="verification")
    login_user(merchant, remember=False)
    return redirect(url_for("auth.verify_email_page"))


# ---------- email verification ----------

@bp.get("/verify-email")
@login_required
def verify_email_page():
    if current_user.email_verified:
        return redirect(url_for("auth.account"))
    return render_template("verify_email.html")


@bp.post("/verify-email")
@login_required
def verify_email():
    if current_user.email_verified:
        return redirect(url_for("auth.account"))

    code = request.form.get("code", "").strip()
    m = db.session.get(Merchant, current_user.id)
    from datetime import datetime, timezone, timedelta

    if _is_locked(m):
        logout_user()
        return render_template("login.html", error="Account locked due to too many OTP attempts. Try again in 30 minutes.")

    if (
        not m.otp_code
        or not _otp_matches(m.otp_code, code)
        or not m.otp_expires_at
        or datetime.now(timezone.utc) > m.otp_expires_at.replace(tzinfo=timezone.utc)
    ):
        m.otp_attempts = (m.otp_attempts or 0) + 1
        if m.otp_attempts >= _MAX_OTP_ATTEMPTS:
            m.locked_until = datetime.now(timezone.utc) + timedelta(minutes=_LOCK_MINUTES)
            m.otp_code = None
            db.session.commit()
            logout_user()
            return render_template("login.html", error="Account locked: too many incorrect codes.")
        db.session.commit()
        remaining = _MAX_OTP_ATTEMPTS - m.otp_attempts
        return render_template("verify_email.html",
            error=f"Invalid or expired code. {remaining} attempt(s) remaining.")

    m.email_verified = True
    m.otp_code = None
    m.otp_expires_at = None
    m.otp_attempts = 0
    db.session.commit()
    return redirect(url_for("auth.account"))


@bp.post("/verify-email/resend")
@login_required
def resend_verification():
    if current_user.email_verified:
        return redirect(url_for("auth.account"))
    m = db.session.get(Merchant, current_user.id)
    otp = generate_otp()
    m.otp_code = otp
    m.otp_expires_at = otp_expiry()
    db.session.commit()
    send_otp(m.email, otp, purpose="verification")
    return render_template("verify_email.html", info="A new code has been sent to your email.")


# ---------- login ----------

@bp.get("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("auth.account"))
    return render_template("login.html")


_MAX_LOGIN_ATTEMPTS = 5
_LOCK_MINUTES       = 30
_MAX_OTP_ATTEMPTS   = 5


def _is_locked(m) -> bool:
    """True if the account is currently locked out."""
    from datetime import datetime, timezone
    if m.locked_until:
        if datetime.now(timezone.utc) < m.locked_until.replace(tzinfo=timezone.utc):
            return True
        # Lock expired — reset
        m.locked_until    = None
        m.login_attempts  = 0
        m.otp_attempts    = 0
        db.session.commit()
    return False


def _client_ip_auth() -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


@bp.post("/login")
@limiter.limit("10 per minute")
def login():
    from datetime import datetime, timezone, timedelta
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    merchant = Merchant.query.filter_by(email=email).first()

    # Always use a constant-time check even for non-existent accounts
    dummy_hash = "pbkdf2:sha256:600000$x$" + "a" * 64
    if merchant and merchant.password_hash:
        pwd_ok = check_password_hash(merchant.password_hash, password)
    else:
        check_password_hash(dummy_hash, password)   # timing attack prevention
        pwd_ok = False

    if not merchant or not merchant.password_hash or not pwd_ok:
        if merchant:
            if _is_locked(merchant):
                return render_template("login.html",
                    error="Account locked. Too many failed attempts. Try again in 30 minutes.")
            merchant.login_attempts = (merchant.login_attempts or 0) + 1
            if merchant.login_attempts >= _MAX_LOGIN_ATTEMPTS:
                merchant.locked_until = datetime.now(timezone.utc) + timedelta(minutes=_LOCK_MINUTES)
                db.session.commit()
                # Alert the account owner
                send_otp(merchant.email, "LOCK",
                         purpose="login")  # reuse channel; email_service handles subject
                return render_template("login.html",
                    error=f"Account locked for {_LOCK_MINUTES} minutes after {_MAX_LOGIN_ATTEMPTS} failed attempts. A notification has been sent to your email.")
            db.session.commit()
        return render_template("login.html", error="Invalid email or password.", form=request.form)

    # Password correct — check lockout
    if _is_locked(merchant):
        return render_template("login.html",
            error="Account locked. Too many failed attempts. Try again in 30 minutes.")

    # Reset failed counter on success
    merchant.login_attempts = 0
    merchant.locked_until   = None

    # Log IP; alert if this is a new IP address
    incoming_ip = _client_ip_auth()
    new_device  = merchant.last_login_ip and merchant.last_login_ip != incoming_ip
    merchant.last_login_ip = incoming_ip
    merchant.last_login_at = datetime.now(timezone.utc)
    db.session.commit()

    if new_device:
        send_otp(merchant.email, "🔔",
                 purpose="login")   # triggers a "new login" style email

    if merchant.two_fa_enabled and merchant.email_verified:
        otp = generate_otp()
        merchant.otp_code      = otp
        merchant.otp_expires_at = otp_expiry()
        merchant.otp_attempts  = 0
        db.session.commit()
        send_otp(merchant.email, otp, purpose="login")
        session[_PENDING_2FA_KEY] = merchant.id
        return redirect(url_for("auth.verify_2fa_page"))

    login_user(merchant, remember=False)   # no persistent cookies by default
    return redirect(_safe_redirect(request.args.get("next"), url_for("auth.account")))


# ---------- 2FA verification ----------

@bp.get("/verify-2fa")
def verify_2fa_page():
    if _PENDING_2FA_KEY not in session:
        return redirect(url_for("auth.login_page"))
    return render_template("verify_2fa.html")


@bp.post("/verify-2fa")
@limiter.limit("10 per minute")
def verify_2fa():
    pending_id = session.get(_PENDING_2FA_KEY)
    if not pending_id:
        return redirect(url_for("auth.login_page"))

    code = request.form.get("code", "").strip()
    m = db.session.get(Merchant, pending_id)
    from datetime import datetime, timezone

    if not m or _is_locked(m):
        session.pop(_PENDING_2FA_KEY, None)
        return render_template("login.html", error="Account locked. Try again in 30 minutes.")

    if (
        not m.otp_code
        or not _otp_matches(m.otp_code, code)
        or not m.otp_expires_at
        or datetime.now(timezone.utc) > m.otp_expires_at.replace(tzinfo=timezone.utc)
    ):
        from datetime import timedelta
        m.otp_attempts = (m.otp_attempts or 0) + 1
        if m.otp_attempts >= _MAX_OTP_ATTEMPTS:
            m.locked_until = datetime.now(timezone.utc) + timedelta(minutes=_LOCK_MINUTES)
            m.otp_code = None
            db.session.commit()
            session.pop(_PENDING_2FA_KEY, None)
            return render_template("login.html", error="Account locked: too many incorrect codes. Try again in 30 minutes.")
        db.session.commit()
        remaining = _MAX_OTP_ATTEMPTS - m.otp_attempts
        return render_template("verify_2fa.html", error=f"Incorrect code. {remaining} attempt(s) remaining.")

    m.otp_code = None
    m.otp_expires_at = None
    db.session.commit()
    session.pop(_PENDING_2FA_KEY, None)
    login_user(m, remember=True)
    return redirect(_safe_redirect(request.args.get("next"), url_for("auth.account")))


@bp.post("/verify-2fa/resend")
def resend_2fa():
    pending_id = session.get(_PENDING_2FA_KEY)
    if not pending_id:
        return redirect(url_for("auth.login_page"))
    m = db.session.get(Merchant, pending_id)
    if not m:
        return redirect(url_for("auth.login_page"))
    otp = generate_otp()
    m.otp_code = otp
    m.otp_expires_at = otp_expiry()
    db.session.commit()
    send_otp(m.email, otp, purpose="login")
    return render_template("verify_2fa.html", info="A new code has been sent.")


# ---------- logout ----------

@bp.get("/logout")
@login_required
def logout():
    session.pop(_PENDING_2FA_KEY, None)
    logout_user()
    return redirect(url_for("auth.login_page"))


# ---------- account ----------

@bp.get("/account")
@login_required
@verified_required
def account():
    from ..models import Payout, Transaction, TxnStatus
    txn_count = Transaction.query.filter_by(merchant_id=current_user.id).count()
    succeeded = Transaction.query.filter_by(
        merchant_id=current_user.id, status=TxnStatus.SUCCEEDED
    ).count()
    payout_count = Payout.query.filter_by(merchant_id=current_user.id).count()
    return render_template(
        "account.html",
        txn_count=txn_count,
        succeeded=succeeded,
        payout_count=payout_count,
    )


@bp.post("/account/toggle-2fa")
@login_required
@verified_required
def toggle_2fa():
    m = db.session.get(Merchant, current_user.id)
    m.two_fa_enabled = not m.two_fa_enabled
    db.session.commit()
    return redirect(url_for("auth.account"))


@bp.post("/account/rotate-keys/<key_type>")
@login_required
@verified_required
def rotate_keys(key_type: str):
    """Rotate test or live API keys. Invalidates old keys immediately."""
    if key_type not in ("test", "live"):
        from flask import abort
        abort(400)
    m = db.session.get(Merchant, current_user.id)
    if key_type == "test":
        m.test_public_key = "pk_test_" + secrets.token_urlsafe(20)
        m.test_secret_key = "sk_test_" + secrets.token_urlsafe(28)
        msg = "Test keys rotated. Update your development environment."
    else:
        m.public_key  = "pk_live_" + secrets.token_urlsafe(20)
        m.secret_key  = "sk_live_" + secrets.token_urlsafe(28)
        msg = "Live keys rotated. Update your production environment immediately."
    db.session.commit()
    from flask import flash
    flash(msg, "success")
    return redirect(url_for("auth.account") + "#keys-" + key_type)
