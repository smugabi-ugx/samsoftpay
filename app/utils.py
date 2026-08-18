"""Auth decorators and RBAC helpers."""
from functools import wraps

from flask import abort, redirect, url_for
from flask_login import current_user


def admin_required(f):
    """Route requires login + admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login_page"))
        if current_user.role != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated


def verified_required(f):
    """KYC verification wall for money features.

    Was a NO-OP ("disabled while email service is being configured") — every
    route wearing it, including withdrawals and payouts, was open to
    unverified merchants. Found by the engineering audit's auth agent. It now
    enforces what the dashboard banner already tells merchants: complete
    verification to unlock money movement. Gated on kyc_status, not email —
    signup deliberately bypasses email verification until SMTP exists.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import flash, redirect, url_for
        if not current_user.is_authenticated:
            abort(401)
        if current_user.role == "admin" or current_user.kyc_status == "verified":
            return f(*args, **kwargs)
        flash("Complete business verification to unlock this feature.", "info")
        return redirect(url_for("kyc.kyc_home"))
    return decorated


def merchant_or_admin(merchant_id: int) -> bool:
    """Return True if the current user owns this merchant account or is admin."""
    return current_user.is_authenticated and (
        current_user.role == "admin" or current_user.id == merchant_id
    )
