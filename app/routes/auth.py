"""Merchant self-service: signup, login, logout, account page."""
import secrets

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import Merchant

bp = Blueprint("auth", __name__)


@bp.get("/signup")
def signup_page():
    if current_user.is_authenticated:
        return redirect(url_for("auth.account"))
    return render_template("signup.html")


@bp.post("/signup")
def signup():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    webhook_url = request.form.get("webhook_url", "").strip() or None

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

    merchant = Merchant(
        name=name,
        email=email,
        password_hash=generate_password_hash(password),
        public_key="pk_live_" + secrets.token_urlsafe(20),
        secret_key="sk_live_" + secrets.token_urlsafe(28),
        test_public_key="pk_test_" + secrets.token_urlsafe(20),
        test_secret_key="sk_test_" + secrets.token_urlsafe(28),
        kyc_status="verified",
        webhook_url=webhook_url,
    )
    db.session.add(merchant)
    db.session.commit()
    login_user(merchant, remember=True)
    return redirect(url_for("auth.account"))


@bp.get("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("auth.account"))
    return render_template("login.html")


@bp.post("/login")
def login():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    merchant = Merchant.query.filter_by(email=email).first()
    if (
        not merchant
        or not merchant.password_hash
        or not check_password_hash(merchant.password_hash, password)
    ):
        return render_template("login.html", error="Invalid email or password.", form=request.form)
    login_user(merchant, remember=True)
    next_url = request.args.get("next")
    return redirect(next_url or url_for("auth.account"))


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login_page"))


@bp.get("/account")
@login_required
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
