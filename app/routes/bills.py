"""Bill collection and tax configuration routes."""
import uuid

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Bill, BillCategory, TaxConfiguration
from ..services.tax import calculate, get_merchant_tax
from ..utils import verified_required

bp = Blueprint("bills", __name__)

_CATEGORIES = [
    ("school_fees",  "School Fees"),
    ("utility",      "Utility Bill (Water/Electricity)"),
    ("government",   "Government / Council Fee"),
    ("hospital",     "Hospital / Medical Bill"),
    ("membership",   "Membership / Club Fee"),
    ("rent",         "Rent"),
    ("other",        "Other"),
]


# ── Merchant bill management ───────────────────────────────────────────────────

@bp.get("/dashboard/bills")
@login_required
@verified_required
def list_bills():
    bills = (Bill.query
             .filter_by(merchant_id=current_user.id)
             .order_by(Bill.created_at.desc())
             .limit(200).all())
    tax_cfg = TaxConfiguration.query.filter_by(merchant_id=current_user.id).first()
    stats = {
        "active":  Bill.query.filter_by(merchant_id=current_user.id, status="active").count(),
        "paid":    Bill.query.filter_by(merchant_id=current_user.id, status="paid").count(),
        "overdue": Bill.query.filter_by(merchant_id=current_user.id, status="overdue").count(),
    }
    return render_template("bills.html", bills=bills, tax_cfg=tax_cfg,
                           stats=stats, categories=_CATEGORIES)


@bp.post("/dashboard/bills/new")
@login_required
@verified_required
def new_bill():
    is_variable = bool(request.form.get("is_variable"))
    try:
        amount = 0 if is_variable else int(request.form["amount"])
        category = BillCategory(request.form.get("category", "other"))
    except (KeyError, ValueError):
        return redirect(url_for("bills.list_bills"))

    due_raw = request.form.get("due_date", "").strip()
    due_date = None
    if due_raw:
        from datetime import datetime
        try:
            due_date = datetime.fromisoformat(due_raw)
        except ValueError:
            pass

    tax_cfg = get_merchant_tax(current_user.id)
    bill = Bill(
        public_id=f"bill_{uuid.uuid4().hex[:16]}",
        merchant_id=current_user.id,
        category=category,
        title=request.form.get("title", "").strip(),
        description=request.form.get("description", "").strip() or None,
        account_ref=request.form.get("account_ref", "").strip() or None,
        customer_name=request.form.get("customer_name", "").strip() or None,
        customer_phone=request.form.get("customer_phone", "").strip() or None,
        amount=amount,
        is_variable=is_variable,
        due_date=due_date,
        tax_rate_bps=tax_cfg.vat_rate_bps if tax_cfg.vat_enabled else 0,
        tax_inclusive=tax_cfg.tax_inclusive,
    )
    db.session.add(bill)
    db.session.commit()
    return redirect(url_for("bills.list_bills"))


@bp.post("/dashboard/bills/<int:bill_id>/cancel")
@login_required
@verified_required
def cancel_bill(bill_id: int):
    bill = Bill.query.filter_by(id=bill_id, merchant_id=current_user.id).first_or_404()
    bill.status = "cancelled"
    db.session.commit()
    return redirect(url_for("bills.list_bills"))


# ── Tax configuration ──────────────────────────────────────────────────────────

@bp.get("/dashboard/tax-settings")
@login_required
@verified_required
def tax_settings():
    cfg = TaxConfiguration.query.filter_by(merchant_id=current_user.id).first()
    return render_template("tax_settings.html", cfg=cfg)


@bp.post("/dashboard/tax-settings")
@login_required
@verified_required
def save_tax_settings():
    from datetime import datetime, timezone
    cfg = TaxConfiguration.query.filter_by(merchant_id=current_user.id).first()
    if not cfg:
        cfg = TaxConfiguration(merchant_id=current_user.id)
        db.session.add(cfg)

    cfg.vat_enabled    = bool(request.form.get("vat_enabled"))
    cfg.vat_number     = request.form.get("vat_number", "").strip() or None
    cfg.tax_inclusive  = bool(request.form.get("tax_inclusive"))
    cfg.show_levy      = bool(request.form.get("show_levy"))
    cfg.business_name  = request.form.get("business_name", "").strip() or None
    cfg.business_address = request.form.get("business_address", "").strip() or None
    try:
        cfg.vat_rate_bps = int(float(request.form.get("vat_rate", "18")) * 100)
    except ValueError:
        cfg.vat_rate_bps = 1800
    cfg.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return redirect(url_for("bills.tax_settings"))


# ── Public bill payment page ───────────────────────────────────────────────────

@bp.get("/pay/@<handle>/bill/<bill_public_id>")
def pay_bill_page(handle: str, bill_public_id: str):
    from ..models import Merchant
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if not merchant:
        abort(404)
    bill = Bill.query.filter_by(
        public_id=bill_public_id, merchant_id=merchant.id
    ).one_or_none()
    if not bill or bill.status not in ("active",):
        abort(404)
    tax_cfg = get_merchant_tax(merchant.id)
    breakdown = None
    if bill.amount > 0:
        breakdown = calculate(
            amount=bill.amount,
            vat_rate_bps=bill.tax_rate_bps,
            tax_inclusive=bill.tax_inclusive,
            levy_rate_bps=tax_cfg.levy_rate_bps,
            show_levy=tax_cfg.show_levy,
        )
    return render_template("pay_bill.html",
                           merchant=merchant, bill=bill,
                           tax_cfg=tax_cfg, breakdown=breakdown)


@bp.post("/pay/@<handle>/bill/<bill_public_id>")
def pay_bill_submit(handle: str, bill_public_id: str):
    from flask import g
    from ..models import Channel, Merchant
    from ..services.orchestrator import OrchestratorError, create_charge
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if not merchant:
        abort(404)
    bill = Bill.query.filter_by(
        public_id=bill_public_id, merchant_id=merchant.id
    ).one_or_none()
    if not bill or bill.status not in ("active",):
        abort(404)

    tax_cfg = get_merchant_tax(merchant.id)
    phone = request.form.get("phone", "").strip()
    channel_str = request.form.get("channel", "mtn_momo")
    try:
        channel = Channel(channel_str)
    except ValueError:
        channel = Channel.MTN_MOMO

    # Determine amount
    if bill.is_variable:
        try:
            entered = int(request.form.get("amount", 0))
        except ValueError:
            entered = 0
        if entered < 500:
            breakdown = None
            return render_template("pay_bill.html", merchant=merchant, bill=bill,
                                   tax_cfg=tax_cfg, breakdown=breakdown,
                                   error="Please enter a valid amount (minimum UGX 500).")
        charge_amount = entered
    else:
        breakdown = calculate(
            amount=bill.amount,
            vat_rate_bps=bill.tax_rate_bps,
            tax_inclusive=bill.tax_inclusive,
            levy_rate_bps=tax_cfg.levy_rate_bps,
            show_levy=tax_cfg.show_levy,
        )
        charge_amount = breakdown.total

    if not phone:
        breakdown = calculate(amount=bill.amount, vat_rate_bps=bill.tax_rate_bps,
                              tax_inclusive=bill.tax_inclusive) if bill.amount else None
        return render_template("pay_bill.html", merchant=merchant, bill=bill,
                               tax_cfg=tax_cfg, breakdown=breakdown,
                               error="Phone number is required.")

    g.api_mode = "live"
    try:
        txn = create_charge(
            merchant=merchant,
            amount=charge_amount,
            currency=bill.currency,
            channel=channel,
            customer_phone=phone,
            customer_email=None,
            merchant_reference=f"{bill.public_id}|{bill.account_ref or ''}",
        )
        bill.transaction_id = txn.id
        bill.status = "paid"
        db.session.commit()
        return render_template("pay_bill.html", merchant=merchant, bill=bill,
                               tax_cfg=tax_cfg, breakdown=None,
                               success=True, txn=txn)
    except OrchestratorError as exc:
        breakdown = calculate(amount=bill.amount, vat_rate_bps=bill.tax_rate_bps,
                              tax_inclusive=bill.tax_inclusive) if bill.amount else None
        return render_template("pay_bill.html", merchant=merchant, bill=bill,
                               tax_cfg=tax_cfg, breakdown=breakdown,
                               error=str(exc))


# ── Bill lookup by account reference ─────────────────────────────────────────

@bp.get("/pay/@<handle>/bills")
def bill_lookup(handle: str):
    from ..models import Merchant
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if not merchant:
        abort(404)
    account_ref = request.args.get("ref", "").strip()
    bills = []
    if account_ref:
        bills = Bill.query.filter_by(
            merchant_id=merchant.id, account_ref=account_ref, status="active"
        ).all()
    return render_template("bill_lookup.html", merchant=merchant,
                           account_ref=account_ref, bills=bills)
