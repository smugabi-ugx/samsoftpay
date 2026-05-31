"""Dashboard routes — all protected by login + RBAC."""
import uuid

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import login_required

from ..extensions import db
from ..models import (
    Account,
    AccountType,
    Merchant,
    PaymentLink,
    Transaction,
    WebhookDelivery,
)
from ..services.reconciliation import run_reconciliation
from ..utils import admin_required, merchant_or_admin, verified_required

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    from flask_login import current_user
    if current_user.is_authenticated and current_user.role == "admin":
        merchants = Merchant.query.all()
    elif current_user.is_authenticated:
        merchants = Merchant.query.filter_by(id=current_user.id).all()
    else:
        merchants = []
    return render_template("index.html", merchants=merchants)


@bp.get("/dashboard")
@login_required
@admin_required
def list_merchants():
    merchants = Merchant.query.all()
    return render_template("merchants.html", merchants=merchants)


@bp.get("/admin")
@login_required
@admin_required
def admin_home():
    from ..models import AuditLog, Payout, TxnStatus
    total_merchants = Merchant.query.count()
    total_txns      = Transaction.query.count()
    total_succeeded = Transaction.query.filter_by(status=TxnStatus.SUCCEEDED).count()
    total_payouts   = Payout.query.count()
    recent_audits   = (
        AuditLog.query.order_by(AuditLog.created_at.desc()).limit(20).all()
    )
    return render_template(
        "admin.html",
        total_merchants=total_merchants,
        total_txns=total_txns,
        total_succeeded=total_succeeded,
        total_payouts=total_payouts,
        recent_audits=recent_audits,
    )


@bp.get("/dashboard/<int:merchant_id>")
@login_required
@verified_required
def merchant_detail(merchant_id: int):
    from ..models import Payout
    if not merchant_or_admin(merchant_id):
        abort(403)
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
    links = (
        PaymentLink.query.filter_by(merchant_id=merchant_id)
        .order_by(PaymentLink.created_at.desc())
        .limit(20)
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
        links=links,
        pending_balance=-pending.cached_balance if pending else 0,
        available_balance=-available.cached_balance if available else 0,
        webhooks=webhooks,
    )


@bp.get("/dashboard/<int:merchant_id>/new-link")
@login_required
@verified_required
def new_link_form(merchant_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    return render_template("new_link.html", merchant=merchant)


@bp.post("/dashboard/<int:merchant_id>/new-link")
@login_required
@verified_required
def new_link_submit(merchant_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    try:
        amount = int(request.form["amount"])
    except (KeyError, ValueError):
        return render_template(
            "new_link.html", merchant=merchant, error="Amount must be a number."
        )
    if amount <= 0:
        return render_template(
            "new_link.html", merchant=merchant, error="Amount must be positive."
        )
    link = PaymentLink(
        public_id=f"lnk_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        currency=request.form.get("currency", "UGX"),
        description=request.form.get("description") or None,
        reference=request.form.get("reference") or None,
        success_url=request.form.get("success_url") or None,
        cancel_url=request.form.get("cancel_url") or None,
        allow_multiple_uses=bool(request.form.get("allow_multiple_uses")),
    )
    db.session.add(link)
    db.session.commit()
    return redirect(url_for("dashboard.merchant_detail", merchant_id=merchant.id))


# ---------- Payout dashboard routes (single + bulk CSV) ----------

@bp.get("/dashboard/<int:merchant_id>/new-payout")
@login_required
@verified_required
def new_payout_form(merchant_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    # Show current available balance so the merchant knows what they can spend.
    avail = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    available = -avail.cached_balance if avail else 0
    return render_template(
        "new_payout.html", merchant=merchant, available=available
    )


@bp.post("/dashboard/<int:merchant_id>/new-payout")
@login_required
@verified_required
def new_payout_submit(merchant_id: int):
    from ..models import Channel as _Channel
    from ..services.payouts import PayoutError, create_payout
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    avail = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    available = -avail.cached_balance if avail else 0

    try:
        amount = int(request.form["amount"])
        phone = request.form["phone"].strip()
        name = request.form.get("recipient_name") or None
    except (KeyError, ValueError):
        return render_template(
            "new_payout.html", merchant=merchant, available=available,
            error="Amount and phone are required.",
        )
    try:
        create_payout(
            merchant=merchant, amount=amount, currency="UGX",
            recipient_phone=phone, recipient_name=name,
            channel=_Channel.MTN_MOMO,
        )
    except PayoutError as exc:
        return render_template(
            "new_payout.html", merchant=merchant, available=available,
            error=str(exc),
        )
    return redirect(url_for("dashboard.merchant_detail", merchant_id=merchant.id))


@bp.get("/dashboard/<int:merchant_id>/bulk-payout")
@login_required
@verified_required
def bulk_payout_form(merchant_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    avail = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    available = -avail.cached_balance if avail else 0
    return render_template(
        "bulk_payout.html", merchant=merchant, available=available
    )


@bp.post("/dashboard/<int:merchant_id>/bulk-payout")
@login_required
@verified_required
def bulk_payout_submit(merchant_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    """Parse a CSV (name, phone, amount), validate, then create payouts for each row.

    CSV format: header row required. Columns: name, phone, amount.
    Phone numbers can be in any common format; we normalize.
    """
    import csv
    import io as _io
    from ..models import Channel as _Channel, PayoutBatch
    from ..services.payouts import PayoutError, create_payout

    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    avail = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    available = -avail.cached_balance if avail else 0

    f = request.files.get("csv")
    if not f or not f.filename:
        return render_template(
            "bulk_payout.html", merchant=merchant, available=available,
            error="Please choose a CSV file.",
        )

    # Read & parse the CSV
    try:
        text = f.read().decode("utf-8-sig")  # strip BOM if Excel saved it
    except UnicodeDecodeError:
        return render_template(
            "bulk_payout.html", merchant=merchant, available=available,
            error="CSV must be UTF-8 encoded.",
        )
    reader = csv.DictReader(_io.StringIO(text))
    rows = []
    errors = []
    line_num = 1
    for row in reader:
        line_num += 1
        name = (row.get("name") or "").strip()
        phone = (row.get("phone") or "").strip()
        amount_raw = (row.get("amount") or "").strip()
        if not phone or not amount_raw:
            errors.append(f"Line {line_num}: missing phone or amount")
            continue
        try:
            amount = int(amount_raw.replace(",", ""))
        except ValueError:
            errors.append(f"Line {line_num}: amount '{amount_raw}' is not a number")
            continue
        if amount <= 0:
            errors.append(f"Line {line_num}: amount must be positive")
            continue
        rows.append((name, phone, amount))

    if errors:
        return render_template(
            "bulk_payout.html", merchant=merchant, available=available,
            error="\n".join(errors[:10]),
        )
    if not rows:
        return render_template(
            "bulk_payout.html", merchant=merchant, available=available,
            error="No valid rows found in CSV.",
        )

    total = sum(r[2] for r in rows)
    if total > available:
        return render_template(
            "bulk_payout.html", merchant=merchant, available=available,
            error=(
                f"Insufficient funds. Available: UGX {available:,}, "
                f"CSV total: UGX {total:,} across {len(rows)} payouts."
            ),
        )

    # Create the batch record and process each row.
    # We do this inline for the demo. In production this would go to a job queue
    # so the dashboard returns immediately and a worker processes the batch.
    batch = PayoutBatch(
        public_id=f"pbatch_{uuid.uuid4().hex[:14]}",
        merchant_id=merchant.id,
        currency="UGX",
        total_amount=total,
        total_count=len(rows),
        status="running",
    )
    db.session.add(batch)
    db.session.commit()

    created = 0
    failed = 0
    for name, phone, amount in rows:
        try:
            p = create_payout(
                merchant=merchant, amount=amount, currency="UGX",
                recipient_phone=phone, recipient_name=name or None,
                channel=_Channel.MTN_MOMO,
            )
            p.batch_id = batch.id
            db.session.commit()
            created += 1
        except PayoutError as exc:
            failed += 1
            continue

    batch.status = "done"
    batch.succeeded_count = created
    batch.failed_count = failed
    db.session.commit()

    return redirect(url_for("dashboard.merchant_detail", merchant_id=merchant.id))


@bp.get("/admin/reconciliation")
@login_required
@admin_required
def reconciliation():
    report = run_reconciliation()
    return render_template("reconciliation.html", report=report)


@bp.post("/admin/sweep-pending")
@login_required
@admin_required
def sweep_pending():
    """Expire stale PENDING/AUTHORIZED transactions and redirect back."""
    from ..services.sweep import sweep_stale_transactions
    result = sweep_stale_transactions(stale_minutes=10)
    return redirect(
        url_for("dashboard.reconciliation", swept=result["swept"],
                succeeded=result["succeeded"], failed=result["failed"])
    )
