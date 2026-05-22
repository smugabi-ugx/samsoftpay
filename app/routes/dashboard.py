"""Merchant-facing dashboard. Demo only — no auth beyond an in-URL merchant id.

A real dashboard has login, MFA, RBAC, audit logs, etc.
"""
from flask import Blueprint, abort, render_template

from ..extensions import db
from ..models import Account, AccountType, Merchant, Transaction, WebhookDelivery
from ..services.reconciliation import run_reconciliation

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    merchants = Merchant.query.all()
    return render_template("index.html", merchants=merchants)


@bp.get("/dashboard")
def list_merchants():
    merchants = Merchant.query.all()
    return render_template("merchants.html", merchants=merchants)


@bp.get("/dashboard/<int:merchant_id>")
def merchant_detail(merchant_id: int):
    from ..models import Payout
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    txns = (
        Transaction.query.filter_by(merchant_id=merchant_id)
        .order_by(Transaction.created_at.desc())
        .limit(50)
        .all()
    )
    payouts = (
        Payout.query.filter_by(merchant_id=merchant_id)
        .order_by(Payout.created_at.desc())
        .limit(50)
        .all()
    )
    pending = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_PENDING
    ).first()
    available = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    webhooks = (
        WebhookDelivery.query.filter_by(merchant_id=merchant_id)
        .order_by(WebhookDelivery.id.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "merchant_detail.html",
        merchant=merchant,
        txns=txns,
        payouts=payouts,
        pending_balance=-pending.cached_balance if pending else 0,
        available_balance=-available.cached_balance if available else 0,
        webhooks=webhooks,
    )


@bp.get("/admin/reconciliation")
def reconciliation():
    report = run_reconciliation()
    return render_template("reconciliation.html", report=report)
