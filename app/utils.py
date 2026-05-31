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
    """Route requires email to be verified. Redirects to verify page if not."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.is_authenticated and not current_user.email_verified:
            return redirect(url_for("auth.verify_email_page"))
        return f(*args, **kwargs)
    return decorated


def merchant_or_admin(merchant_id: int) -> bool:
    """Return True if the current user owns this merchant account or is admin."""
    return current_user.is_authenticated and (
        current_user.role == "admin" or current_user.id == merchant_id
    )
