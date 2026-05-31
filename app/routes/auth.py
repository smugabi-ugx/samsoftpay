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
    from datetime import datetime, timezone

    if (
        not m.otp_code
        or not _otp_matches(m.otp_code, code)
        or not m.otp_expires_at
        or datetime.now(timezone.utc) > m.otp_expires_at.replace(tzinfo=timezone.utc)
    ):
        return render_template("verify_email.html", error="Invalid or expired code. Try resending.")

    m.email_verified = True
    m.otp_code = None
    m.otp_expires_at = None
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


@bp.post("/login")
@limiter.limit("10 per minute")
def login():
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    merchant = Merchant.query.filter_by(email=email).first()

    if (
        not merchant
        or not merchant.password_hash
        or not check_password_hash(merchant.password_hash, password)
    ):
        return render_template("login.html", error="Invalid email or password.", form=request.form)

    if merchant.two_fa_enabled and merchant.email_verified:
        # Step 1 done — send OTP for step 2
        otp = generate_otp()
        merchant.otp_code = otp
        merchant.otp_expires_at = otp_expiry()
        db.session.commit()
        send_otp(merchant.email, otp, purpose="login")
        session[_PENDING_2FA_KEY] = merchant.id
        return redirect(url_for("auth.verify_2fa_page"))

    # 2FA disabled or email not yet verified — log straight in
    login_user(merchant, remember=True)
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

    if not m or (
        not m.otp_code
        or not _otp_matches(m.otp_code, code)
        or not m.otp_expires_at
        or datetime.now(timezone.utc) > m.otp_expires_at.replace(tzinfo=timezone.utc)
    ):
        return render_template("verify_2fa.html", error="Invalid or expired code.")

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
