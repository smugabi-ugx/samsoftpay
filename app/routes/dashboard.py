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
    """Landing page — always visible to everyone."""
    return render_template("landing.html")


@bp.get("/home")
@login_required
@admin_required
def admin_index():
    merchants = Merchant.query.all()
    return render_template("index.html", merchants=merchants)


@bp.get("/dashboard")
@login_required
@admin_required
def list_merchants():
    merchants = Merchant.query.all()
    return render_template("merchants.html", merchants=merchants)


@bp.post("/admin/merchants/create")
@login_required
@admin_required
def admin_create_merchant():
    """Create a merchant account from the admin panel."""
    import secrets as _sec
    from werkzeug.security import generate_password_hash
    from flask import flash
    from ..routes.auth import _make_handle

    name     = request.form.get("name", "").strip()
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role     = request.form.get("role", "merchant")
    kyc      = request.form.get("kyc_status", "pending")

    if not name or not email or len(password) < 8:
        flash("Name, email and password (8+ chars) are required.", "error")
        return redirect(url_for("dashboard.list_merchants"))
    if Merchant.query.filter_by(email=email).first():
        flash(f"Email already exists: {email}", "error")
        return redirect(url_for("dashboard.list_merchants"))

    m = Merchant(
        name=name, email=email,
        password_hash=generate_password_hash(password),
        public_key="pk_live_" + _sec.token_urlsafe(20),
        secret_key="sk_live_" + _sec.token_urlsafe(28),
        test_public_key="pk_test_" + _sec.token_urlsafe(20),
        test_secret_key="sk_test_" + _sec.token_urlsafe(28),
        handle=_make_handle(name),
        role=role, kyc_status=kyc,
        email_verified=True, two_fa_enabled=False,
    )
    db.session.add(m)
    db.session.commit()
    flash(f"Created: {m.name} (ID {m.id})", "success")
    return redirect(url_for("dashboard.list_merchants"))


@bp.post("/admin/merchants/<int:merchant_id>/update")
@login_required
@admin_required
def admin_update_merchant(merchant_id: int):
    """Update merchant details from admin panel."""
    from flask import flash
    m = db.session.get(Merchant, merchant_id) or abort(404)
    m.name       = request.form.get("name", m.name).strip() or m.name
    m.email      = request.form.get("email", m.email).strip().lower() or m.email
    m.kyc_status = request.form.get("kyc_status", m.kyc_status)
    m.role       = request.form.get("role", m.role)
    m.is_active  = request.form.get("is_active") == "1"
    # Only touched when the admin form actually submits the field, so existing
    # forms that don't render it can't silently switch vending off.
    if "vending_enabled" in request.form:
        m.vending_enabled = request.form.get("vending_enabled") == "1"
    db.session.commit()
    flash(f"Updated: {m.name}", "success")
    return redirect(url_for("dashboard.list_merchants"))


@bp.get("/admin/merchants/<int:merchant_id>/delete-confirm")
@login_required
@admin_required
def admin_delete_confirm(merchant_id: int):
    m = db.session.get(Merchant, merchant_id) or abort(404)
    if m.email in ("smugabi@mail.com", "demo@samsoftpay.local", "smugabi@gmail.com"):
        from flask import flash
        flash("Cannot delete a protected admin account.", "error")
        return redirect(url_for("dashboard.list_merchants"))
    return render_template("admin_delete_confirm.html", merchant=m)


@bp.post("/admin/merchants/<int:merchant_id>/delete")
@login_required
@admin_required
def admin_delete_merchant(merchant_id: int):
    """Hard-delete a test/demo merchant and all their data."""
    from flask import flash
    m = db.session.get(Merchant, merchant_id) or abort(404)

    # Protect the superadmin account
    if m.email in ("smugabi@mail.com", "demo@samsoftpay.local"):
        flash(f"Cannot delete protected account: {m.email}", "error")
        return redirect(url_for("dashboard.list_merchants"))

    name = m.name
    from ..models import (
        AuditLog, Bill, GiftCard, KYCApplication, KYCDirector, KYCDocument,
        PaymentLink, Payout, SettlementAccount, Subscription, SubscriptionPlan,
        TopUpRequest, Transaction, WebhookDelivery, WithdrawalRequest,
        Account, JournalEntry,
    )
    try:
        # 1. Children of KYCApplication
        kyc_apps = KYCApplication.query.filter_by(merchant_id=merchant_id).all()
        for app in kyc_apps:
            KYCDirector.query.filter_by(application_id=app.id).delete()
            KYCDocument.query.filter_by(application_id=app.id).delete()
        KYCApplication.query.filter_by(merchant_id=merchant_id).delete()

        # 2. Subscriptions before plans
        Subscription.query.filter_by(merchant_id=merchant_id).delete()
        SubscriptionPlan.query.filter_by(merchant_id=merchant_id).delete()

        # 3. Withdrawal & top-up requests (reference settlement accounts, payouts)
        WithdrawalRequest.query.filter_by(merchant_id=merchant_id).delete()
        TopUpRequest.query.filter_by(merchant_id=merchant_id).delete()
        SettlementAccount.query.filter_by(merchant_id=merchant_id).delete()

        # 4. Other direct merchant refs
        for model in [AuditLog, Bill, GiftCard, WebhookDelivery]:
            model.query.filter_by(merchant_id=merchant_id).delete()

        # 5. Payment links (before transactions that ref them)
        PaymentLink.query.filter_by(merchant_id=merchant_id).delete()
        Payout.query.filter_by(merchant_id=merchant_id).delete()
        Transaction.query.filter_by(merchant_id=merchant_id).delete()

        # 6. Ledger — journal entries ref accounts
        acct_ids = [a.id for a in Account.query.filter_by(merchant_id=merchant_id).all()]
        for chunk in [acct_ids[i:i+100] for i in range(0, len(acct_ids), 100)]:
            JournalEntry.query.filter(JournalEntry.account_id.in_(chunk)).delete()
        Account.query.filter_by(merchant_id=merchant_id).delete()

        db.session.delete(m)
        db.session.commit()
        flash(f"Deleted: {name}", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Delete failed: {exc}", "error")
    return redirect(url_for("dashboard.list_merchants"))


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
        merchant_id=merchant_id, type=AccountType.MERCHANT_PENDING, is_test=False
    ).first()
    available = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
    ).first()
    webhooks = (
        WebhookDelivery.query.filter_by(merchant_id=merchant_id)
        .order_by(WebhookDelivery.id.desc())
        .limit(20)
        .all()
    )
    # Real aggregates for the headline stat cards. Computing them from `txns`
    # in the template silently capped every number at the 50 most recent rows —
    # a merchant's "Total Volume" stopped growing at their 50th transaction.
    from sqlalchemy import func as safunc
    from ..models import TxnStatus
    succeeded_count, succeeded_volume = (
        db.session.query(safunc.count(Transaction.id),
                         safunc.coalesce(safunc.sum(Transaction.amount), 0))
        .filter(Transaction.merchant_id == merchant_id,
                Transaction.status == TxnStatus.SUCCEEDED)
        .one()
    )
    payout_count = Payout.query.filter_by(merchant_id=merchant_id).count()
    active_link_count = PaymentLink.query.filter_by(
        merchant_id=merchant_id, is_active=True).count()

    return render_template(
        "merchant_detail.html",
        merchant=merchant,
        txns=txns,
        payouts=payouts,
        links=links,
        pending_balance=-pending.cached_balance if pending else 0,
        available_balance=-available.cached_balance if available else 0,
        webhooks=webhooks,
        succeeded_count=succeeded_count,
        succeeded_volume=succeeded_volume,
        payout_count=payout_count,
        active_link_count=active_link_count,
    )


@bp.post("/dashboard/<int:merchant_id>/charge/<public_id>/refund")
@login_required
@verified_required
def dashboard_refund(merchant_id: int, public_id: str):
    """Refund a charge from the dashboard (self-service, no API/full-scope key).

    Delegates to the SAME refund_charge service the API uses — including its
    REFUNDED row-lock claim guard and that refunds draw from MERCHANT_AVAILABLE —
    so this is just a login-gated UI over the vetted money path.
    """
    from flask import flash
    from ..services.refunds import RefundError, refund_charge
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    txn = Transaction.query.filter_by(
        public_id=public_id, merchant_id=merchant_id).one_or_none()
    if txn is None:
        abort(404)
    try:
        result = refund_charge(txn, merchant)
    except RefundError as exc:
        flash(f"Refund failed: {exc}", "error")
        return redirect(url_for("dashboard.merchant_detail", merchant_id=merchant_id))
    if result.get("ok"):
        flash(f"Refund initiated for {public_id}.", "success")
    else:
        flash(f"Could not refund {public_id}: {result.get('error')}", "error")
    return redirect(url_for("dashboard.merchant_detail", merchant_id=merchant_id))


@bp.get("/dashboard/<int:merchant_id>/export/transactions.csv")
@login_required
@verified_required
def export_transactions_csv(merchant_id: int):
    """Stream the merchant's transactions as a CSV statement.

    Owner-or-admin gated (never another merchant's data). Optional filters:
    status, after/before (ISO 8601). Includes a `mode` column so a merchant can
    tell test rows apart rather than us silently hiding them.
    """
    import csv
    import io
    from datetime import datetime
    from flask import Response
    from ..models import TxnStatus
    if not merchant_or_admin(merchant_id):
        abort(403)
    db.session.get(Merchant, merchant_id) or abort(404)

    q = Transaction.query.filter_by(merchant_id=merchant_id)
    status_f = request.args.get("status")
    if status_f:
        try:
            q = q.filter(Transaction.status == TxnStatus(status_f))
        except ValueError:
            abort(400, description=f"invalid status: {status_f}")

    def _dt(name):
        raw = request.args.get(name)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            abort(400, description=f"{name} must be an ISO 8601 datetime")
    after, before = _dt("after"), _dt("before")
    if after:
        q = q.filter(Transaction.created_at >= after)
    if before:
        q = q.filter(Transaction.created_at <= before)

    rows = q.order_by(Transaction.created_at.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "status", "amount", "fee", "currency", "channel",
                "reference", "customer_phone", "mode", "completed_at"])
    for t in rows:
        w.writerow([
            t.public_id,
            t.created_at.isoformat() if t.created_at else "",
            t.status.value, t.amount, t.fee_amount, t.currency, t.channel.value,
            t.merchant_reference or "", t.customer_phone or "",
            "test" if t.is_test else "live",
            t.completed_at.isoformat() if t.completed_at else "",
        ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=samsoftpay_transactions_{merchant_id}.csv"})


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


# ---------- Vending machines (XY connector) ----------

@bp.get("/dashboard/<int:merchant_id>/vending")
@login_required
@verified_required
def vending_settings(merchant_id: int):
    """Merchant-facing switch and order log for the vending connector."""
    import os

    from ..services.vending import read_meta

    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)

    orders = (
        PaymentLink.query
        .filter(PaymentLink.merchant_id == merchant_id,
                PaymentLink.vending_meta.isnot(None))
        .order_by(PaymentLink.created_at.desc())
        .limit(25)
        .all()
    )
    # Pair each order with its machine + payment status for the table.
    rows = []
    for o in orders:
        meta = read_meta(o) or {}
        txn = db.session.get(Transaction, o.transaction_id) if o.transaction_id else None
        rows.append({
            "link": o,
            "machine": meta.get("machine", "—"),
            "payment_status": txn.status.value if txn else "unpaid",
        })

    from ..services import vending as _vending
    from ..services import xy_vending

    creds = xy_vending.for_merchant(merchant)
    machines = _vending.machines_for(merchant)

    return render_template(
        "vending_settings.html",
        merchant=merchant,
        rows=rows,
        machines=machines,
        # Never render the secret itself — only whether one exists, and where
        # it came from, so the merchant can tell their own key from ours.
        connector_configured=creds.configured,
        using_own_credentials=bool(merchant.xy_key and merchant.xy_secret_encrypted),
        platform_fallback=bool(os.environ.get("XY_KEY") and os.environ.get("XY_SECRET")),
        xy_base_url=creds.base(),
    )


@bp.post("/dashboard/<int:merchant_id>/vending/credentials")
@login_required
@verified_required
def vending_credentials(merchant_id: int):
    """Save the merchant's OWN supplier credentials (key/secret/shbh)."""
    from flask import flash

    from ..services.audit import log_event
    from ..services.secrets_box import encrypt

    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)

    merchant.xy_key = (request.form.get("xy_key") or "").strip()[:120] or None
    merchant.xy_merchant_no = (request.form.get("xy_merchant_no") or "").strip()[:60] or None
    merchant.xy_base_url = (request.form.get("xy_base_url") or "").strip()[:200] or None
    # Blank secret = leave the stored one alone (the form never echoes it back).
    secret = (request.form.get("xy_secret") or "").strip()
    if secret:
        merchant.xy_secret_encrypted = encrypt(secret)
    if request.form.get("clear_secret") == "1":
        merchant.xy_secret_encrypted = None

    db.session.commit()
    log_event("vending.credentials_updated", merchant_id=merchant.id,
              detail={"has_key": bool(merchant.xy_key),
                      "has_secret": bool(merchant.xy_secret_encrypted)})
    flash("Machine operator credentials saved.", "success")
    return redirect(url_for("dashboard.vending_settings", merchant_id=merchant.id))


@bp.post("/dashboard/<int:merchant_id>/vending/sync")
@login_required
@verified_required
def vending_sync(merchant_id: int):
    """Pull the merchant's machine list from the supplier."""
    from flask import flash

    from ..services import vending

    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    try:
        added, updated = vending.sync_machines(merchant)
        flash(f"Machines synced: {added} new, {updated} updated.", "success")
    except vending.VendingError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        flash(f"Could not reach the machine operator's system: {exc}", "error")
    return redirect(url_for("dashboard.vending_settings", merchant_id=merchant.id))


@bp.post("/dashboard/<int:merchant_id>/vending/toggle")
@login_required
@verified_required
def vending_toggle(merchant_id: int):
    """Turn the vending connector on or off for this merchant."""
    from flask import flash

    from ..services.audit import log_event

    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    merchant.vending_enabled = request.form.get("enabled") == "1"
    db.session.commit()
    log_event("vending.toggled", merchant_id=merchant.id,
              detail={"enabled": merchant.vending_enabled})
    flash(
        "Vending machine payments enabled." if merchant.vending_enabled
        else "Vending machine payments turned off.",
        "success",
    )
    return redirect(url_for("dashboard.vending_settings", merchant_id=merchant.id))


@bp.post("/dashboard/<int:merchant_id>/vending/test-order")
@login_required
@verified_required
def vending_test_order(merchant_id: int):
    """Create an order by hand — the demo/QA path for a real machine."""
    from flask import flash

    from ..services import vending

    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    try:
        link = vending.create_order(
            merchant=merchant,
            machine=(request.form.get("machine") or "").strip(),
            goods=[{
                "spbh": (request.form.get("spbh") or "").strip(),
                "spmc": (request.form.get("spmc") or "").strip(),
                "spdj": amount,
            }],
            amount=amount,
            reference=(request.form.get("reference") or "").strip() or None,
        )
    except vending.VendingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard.vending_settings", merchant_id=merchant.id))

    return redirect(url_for("checkout.vending_display", public_id=link.public_id))


@bp.post("/dashboard/<int:merchant_id>/vending/<public_id>/retry")
@login_required
@verified_required
def vending_retry(merchant_id: int, public_id: str):
    """Retry a dispense the supplier rejected. Payment guard still applies."""
    from flask import flash

    from ..services import vending

    if not merchant_or_admin(merchant_id):
        abort(403)
    link = PaymentLink.query.filter_by(
        public_id=public_id, merchant_id=merchant_id
    ).one_or_none() or abort(404)
    try:
        ok = vending.retry_dispense(link)
        flash("Dispensed." if ok else f"Still failing: {link.vending_error}",
              "success" if ok else "error")
    except vending.VendingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard.vending_settings", merchant_id=merchant_id))


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
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
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
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
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
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
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
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
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
    from ..models import ReconException
    report = run_reconciliation()
    recon_exceptions = (ReconException.query.filter_by(status="open")
                        .order_by(ReconException.severity, ReconException.created_at.desc())
                        .limit(200).all())
    return render_template("reconciliation.html", report=report,
                           recon_exceptions=recon_exceptions)


@bp.post("/admin/reconciliation/run-mtn")
@login_required
@admin_required
def run_mtn_reconciliation():
    """Run the MTN reconciliation on demand from the dashboard."""
    from flask import flash
    from ..services.reconciliation import reconcile_against_mtn
    summary = reconcile_against_mtn()
    flash(f"MTN reconciliation: checked {summary['checked']}, "
          f"{summary['open_exceptions']} open exception(s).",
          "error" if summary["open_exceptions"] else "success")
    return redirect(url_for("dashboard.reconciliation"))


@bp.post("/admin/reconciliation/exceptions/<int:exc_id>/resolve")
@login_required
@admin_required
def resolve_recon_exception(exc_id: int):
    from datetime import datetime, timezone
    from flask import flash
    from flask_login import current_user
    from ..models import ReconException
    exc = db.session.get(ReconException, exc_id) or abort(404)
    exc.status = "resolved"
    exc.resolved_at = datetime.now(timezone.utc)
    exc.resolved_by = current_user.email
    db.session.commit()
    flash(f"Exception {exc.rail_reference} marked resolved.", "success")
    return redirect(url_for("dashboard.reconciliation"))


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
