"""Dashboard routes — all protected by login + RBAC."""
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    Account,
    AccountType,
    Merchant,
    PaymentLink,
    Transaction,
    WebhookDelivery,
)
from ..pagination import page_arg
from ..services.reconciliation import run_reconciliation
from ..utils import admin_required, merchant_or_admin, verified_required

bp = Blueprint("dashboard", __name__)

# Transactions load a small page at a time via infinite scroll (the list can be
# huge for a busy merchant). 5 keeps the first paint light and scrolls smoothly.
_ACTIVITY_PAGE = 5


@bp.app_context_processor
def _inject_maintenance_banner():
    """Expose maintenance_mode to all templates for the global banner. Reads a
    single PK-indexed flag; fails safe to False so no page can break on it."""
    try:
        from ..services.platform_flags import maintenance_on
        return {"maintenance_mode": maintenance_on()}
    except Exception:
        return {"maintenance_mode": False}


# Endpoints that stay usable WHILE an admin is in read-only "view as" mode.
# This is an ALLOWLIST: only these named endpoints may run a state-changing
# (non-GET) request during impersonation. Kept tiny on purpose — the exit
# paths, nothing that moves money.
_VIEW_AS_ALLOWED_ENDPOINTS = {
    "dashboard.stop_view_as",   # the Exit button (POST)
    "dashboard.start_view_as",  # switching straight to another merchant (POST, admin-only, session-flag only)
    "auth.logout",              # signing out (POST) also ends the view
    "static",
}


@bp.before_app_request
def _enforce_view_as_readonly():
    """Make admin 'view as' STRICTLY read-only, app-wide.

    While a view-as session is active only non-mutating methods
    (GET/HEAD/OPTIONS) run, plus the explicit exit/logout endpoints. Every
    other POST/PUT/PATCH/DELETE — the only way to create a payout, withdrawal,
    refund, rotate a key or change any setting — is refused with 403 BEFORE
    the view function executes (zero writes). Allowlist-by-method means a
    money endpoint added later is blocked automatically.
    """
    from flask import session
    if not session.get("view_as_merchant_id"):
        return
    # Only an authenticated admin may carry this flag; anyone else (logged
    # out, role changed, forged) has it dropped and proceeds normally.
    if not (current_user.is_authenticated and getattr(current_user, "role", None) == "admin"):
        session.pop("view_as_merchant_id", None)
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.endpoint in _VIEW_AS_ALLOWED_ENDPOINTS:
        return
    abort(403, description="Read-only support view is active — click Exit "
                           "in the banner before making any changes.")


@bp.app_context_processor
def _inject_view_as():
    """Expose the viewed merchant to every template (the persistent banner).
    Self-heals a stale flag pointing at a deleted merchant."""
    from flask import session
    mid = session.get("view_as_merchant_id")
    if not mid:
        return {"view_as_merchant": None}
    m = db.session.get(Merchant, mid)
    if m is None:
        session.pop("view_as_merchant_id", None)
    return {"view_as_merchant": m}


@bp.get("/")
def index():
    """Landing page — always visible to everyone."""
    return render_template("landing.html")


@bp.get("/home")
@login_required
@admin_required
def admin_index():
    pag = Merchant.query.paginate(page=page_arg("page"), per_page=50, error_out=False)
    merchants = pag.items
    return render_template("index.html", merchants=merchants, pag=pag)


@bp.get("/dashboard")
@login_required
@admin_required
def list_merchants():
    # Legacy route — redirect to the paginated admin console.
    return redirect(url_for("dashboard.admin_merchants"))


# ── Enriched admin console: manage merchants + features ──────────────────────

@bp.get("/admin/merchants")
@login_required
@admin_required
def admin_merchants():
    """Searchable, paginated, filterable merchant list (replaces the two
    unbounded Merchant.query.all() screens that would OOM at scale)."""
    from sqlalchemy import or_
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    kyc = request.args.get("kyc") or ""
    show_managed = request.args.get("managed") == "1"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    query = Merchant.query
    if not show_managed:
        query = query.filter((Merchant.is_managed.is_(False)) | (Merchant.is_managed.is_(None)))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Merchant.name.ilike(like),
                                 Merchant.email.ilike(like),
                                 Merchant.handle.ilike(like)))
    if status == "active":
        query = query.filter(Merchant.is_active.is_(True))
    elif status == "suspended":
        query = query.filter(Merchant.is_active.is_(False))
    if kyc in ("pending", "verified", "rejected"):
        query = query.filter(Merchant.kyc_status == kyc)

    pag = query.order_by(Merchant.id.desc()).paginate(page=page, per_page=50, error_out=False)
    # Batch-load live available balances in ONE query (no N+1).
    ids = [m.id for m in pag.items]
    balances = {}
    if ids:
        rows = (db.session.query(Account.merchant_id, Account.cached_balance)
                .filter(Account.merchant_id.in_(ids),
                        Account.type == AccountType.MERCHANT_AVAILABLE,
                        Account.is_test.is_(False)).all())
        balances = {mid: -bal for mid, bal in rows}
    return render_template("admin_merchants.html", pag=pag, merchants=pag.items,
                           balances=balances, q=q, status=status, kyc=kyc,
                           show_managed=show_managed)


@bp.get("/admin/merchants/<int:merchant_id>")
@login_required
@admin_required
def admin_merchant_console(merchant_id: int):
    """The operator's per-merchant hub — identity, balances, activity, flags,
    and the audit timeline, with all admin actions."""
    from sqlalchemy import func as safunc
    from ..models import AuditLog, KYCApplication, Payout, TxnStatus
    m = db.session.get(Merchant, merchant_id) or abort(404)

    def _bal(t, is_test):
        a = Account.query.filter_by(merchant_id=merchant_id, type=t, is_test=is_test).first()
        return -a.cached_balance if a else 0

    succ_count, succ_vol = (
        db.session.query(safunc.count(Transaction.id),
                         safunc.coalesce(safunc.sum(Transaction.amount), 0))
        .filter(Transaction.merchant_id == merchant_id,
                Transaction.status == TxnStatus.SUCCEEDED).one())
    charges = (Transaction.query.filter_by(merchant_id=merchant_id)
               .order_by(Transaction.id.desc()).limit(10).all())
    payouts = (Payout.query.filter_by(merchant_id=merchant_id)
               .order_by(Payout.id.desc()).limit(10).all())
    audits = (AuditLog.query.filter_by(merchant_id=merchant_id)
              .order_by(AuditLog.id.desc()).limit(25).all())
    kyc_app = (KYCApplication.query.filter_by(merchant_id=merchant_id)
               .order_by(KYCApplication.id.desc()).first())
    return render_template(
        "admin_merchant_console.html", m=m,
        live_available=_bal(AccountType.MERCHANT_AVAILABLE, False),
        live_pending=_bal(AccountType.MERCHANT_PENDING, False),
        sandbox_available=_bal(AccountType.MERCHANT_AVAILABLE, True),
        succ_count=succ_count, succ_vol=succ_vol,
        charges=charges, payouts=payouts, audits=audits, kyc_app=kyc_app,
        keys_present={
            "live_secret": bool(m.secret_key), "test_secret": bool(m.test_secret_key),
            "collections": bool(getattr(m, "collections_key", None)),
        })


def _admin_log(event, merchant_id, **detail):
    from ..services.audit import log_event
    log_event(event, merchant_id=merchant_id, detail=detail)
    db.session.commit()


@bp.post("/admin/merchants/<int:merchant_id>/suspend")
@login_required
@admin_required
def admin_suspend_merchant(merchant_id: int):
    from datetime import datetime, timezone
    m = db.session.get(Merchant, merchant_id) or abort(404)
    reason = (request.form.get("reason") or "").strip()[:500]
    if not reason:
        flash("A suspension reason is required.", "error")
        return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))
    # is_active is the source of truth — api._auth 401s and create_charge/payout
    # refuse it, so this instantly halts all new money movement. In-flight
    # settlement of already-owed money is deliberately unaffected.
    m.is_active = False
    m.suspended_at = datetime.now(timezone.utc)
    m.suspended_by = current_user.email
    m.suspend_reason = reason
    db.session.commit()
    _admin_log("merchant.suspended", merchant_id, by=current_user.email, reason=reason)
    flash(f"{m.name} suspended — new charges, payouts and API access are halted.", "success")
    return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))


@bp.post("/admin/merchants/<int:merchant_id>/reactivate")
@login_required
@admin_required
def admin_reactivate_merchant(merchant_id: int):
    m = db.session.get(Merchant, merchant_id) or abort(404)
    m.is_active = True
    m.suspended_at = None
    m.suspended_by = None
    m.suspend_reason = None
    db.session.commit()
    _admin_log("merchant.reactivated", merchant_id, by=current_user.email)
    flash(f"{m.name} reactivated.", "success")
    return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))


@bp.post("/admin/merchants/<int:merchant_id>/force-kyc")
@login_required
@admin_required
def admin_force_kyc(merchant_id: int):
    from ..models import KYCApplication
    m = db.session.get(Merchant, merchant_id) or abort(404)
    target = request.form.get("kyc_status")
    if target not in ("pending", "verified", "rejected"):
        abort(400)
    m.kyc_status = target
    # Best-effort sync an existing application so the two don't drift.
    app_row = (KYCApplication.query.filter_by(merchant_id=merchant_id)
               .order_by(KYCApplication.id.desc()).first())
    if app_row:
        app_row.status = {"verified": "approved", "rejected": "draft",
                          "pending": "submitted"}.get(target, app_row.status)
    db.session.commit()
    _admin_log("merchant.kyc_forced", merchant_id, by=current_user.email, to=target,
               reason=(request.form.get("reason") or "").strip()[:500])
    flash(f"KYC status set to {target}.", "success")
    return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))


@bp.post("/admin/merchants/<int:merchant_id>/reset-password")
@login_required
@admin_required
def admin_reset_password(merchant_id: int):
    import secrets as _secrets
    from werkzeug.security import generate_password_hash
    m = db.session.get(Merchant, merchant_id) or abort(404)
    temp = "Ssp-" + _secrets.token_urlsafe(9)
    m.password_hash = generate_password_hash(temp)
    m.login_attempts = 0
    m.locked_until = None
    db.session.commit()
    _admin_log("merchant.password_reset", merchant_id, by=current_user.email)
    # Shown ONCE (no SMTP) — the admin relays it to the merchant.
    flash(f"Temporary password for {m.email}: {temp} — give it to the merchant; "
          f"they should change it after signing in.", "success")
    return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))


@bp.post("/admin/merchants/<int:merchant_id>/toggle-feature")
@login_required
@admin_required
def admin_toggle_feature(merchant_id: int):
    m = db.session.get(Merchant, merchant_id) or abort(404)
    feature = request.form.get("feature")
    allowed = {"vending_enabled", "instant_settlement"}
    if feature not in allowed:
        abort(400)
    new_val = not bool(getattr(m, feature))
    setattr(m, feature, new_val)
    db.session.commit()
    _admin_log("merchant.feature_toggled", merchant_id, by=current_user.email,
               feature=feature, enabled=new_val)
    flash(f"{feature.replace('_', ' ').title()} {'enabled' if new_val else 'disabled'} "
          f"for {m.name}.", "success")
    return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))


@bp.post("/admin/merchants/<int:merchant_id>/limits")
@login_required
@admin_required
def admin_set_limits(merchant_id: int):
    """Set per-merchant money limits and an optional fee override.

    Blank field = clear (NULL = no limit / standard fee). The three are
    independent. max_charge_amount is enforced in orchestrator.create_charge,
    max_payout_amount in payouts.create_payout (both before any write), and
    fee_bps_override feeds fees.calculate_fee (standard UGX min/cap preserved).
    """
    m = db.session.get(Merchant, merchant_id) or abort(404)
    MAXCAP = (1 << 63) - 1

    def _opt_int(field, *, lo, hi, label):
        # Returns (value_or_None, error_or_None). Blank -> (None, None) = cleared.
        raw = (request.form.get(field) or "").strip().replace(",", "")
        if raw == "":
            return None, None
        try:
            val = int(raw)
        except ValueError:
            return None, f"{label} must be a whole number."
        if val < lo or val > hi:
            return None, f"{label} must be between {lo:,} and {hi:,}."
        return val, None

    max_charge, e1 = _opt_int("max_charge_amount", lo=1, hi=MAXCAP,
                              label="Max charge amount")
    max_payout, e2 = _opt_int("max_payout_amount", lo=1, hi=MAXCAP,
                              label="Max payout amount")
    fee_bps, e3 = _opt_int("fee_bps_override", lo=0, hi=10000,
                           label="Fee override (bps)")
    err = e1 or e2 or e3
    if err:
        flash(err, "error")
        return redirect(url_for("dashboard.admin_merchant_console",
                                merchant_id=merchant_id) + "#actions")

    m.max_charge_amount = max_charge
    m.max_payout_amount = max_payout
    m.fee_bps_override = fee_bps
    db.session.commit()
    _admin_log("merchant.limits_updated", merchant_id, by=current_user.email,
               max_charge_amount=max_charge, max_payout_amount=max_payout,
               fee_bps_override=fee_bps)
    flash("Limits & fee override updated.", "success")
    return redirect(url_for("dashboard.admin_merchant_console",
                            merchant_id=merchant_id) + "#actions")


@bp.post("/admin/merchants/<int:merchant_id>/view-as")
@login_required
@admin_required
def start_view_as(merchant_id: int):
    """Enter read-only support view for a merchant.

    POST (not GET) so link-prefetch/CSRF can never flip the session state.
    We do NOT call login_user(m) — the admin stays the admin (truthful audit
    trail, no privilege transfer); the flag + before_app_request guard keep
    it strictly read-only.
    """
    from flask import session
    m = db.session.get(Merchant, merchant_id) or abort(404)
    # Never impersonate another admin — no support reason to.
    if getattr(m, "role", None) == "admin":
        flash("Cannot view-as an admin account.", "error")
        return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))
    session["view_as_merchant_id"] = m.id
    _admin_log("merchant.view_as_started", merchant_id, by=current_user.email)
    flash(f"Now viewing {m.name} in read-only support mode. No money can move "
          f"until you exit.", "info")
    return redirect(url_for("dashboard.merchant_detail", merchant_id=m.id))


@bp.post("/admin/merchants/<int:merchant_id>/view-as/stop")
@login_required
@admin_required
def stop_view_as(merchant_id: int):
    """Exit read-only support view (allowlisted in the guard so this POST runs)."""
    from flask import session
    session.pop("view_as_merchant_id", None)
    _admin_log("merchant.view_as_stopped", merchant_id, by=current_user.email)
    flash("Exited support view.", "success")
    return redirect(url_for("dashboard.admin_merchant_console", merchant_id=merchant_id))


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


def _is_protected_merchant(m) -> bool:
    """A merchant that must NEVER be hard-deleted.

    ONE authoritative rule, shared by the confirm page and the destructive POST,
    so they can never disagree: the POST used to protect a smaller email set than
    the confirm page implied, and neither protected any admin by role nor the
    configured ADMIN_EMAIL. Gate on role first (any admin is untouchable), then a
    known-account allowlist.
    """
    import os
    protected_emails = {
        "smugabi@mail.com", "demo@samsoftpay.local", "smugabi@gmail.com",
        "samsoftware75@gmail.com",
        (os.environ.get("ADMIN_EMAIL") or "").strip().lower(),
    }
    return (getattr(m, "role", None) == "admin"
            or (m.email or "").strip().lower() in protected_emails)


@bp.get("/admin/merchants/<int:merchant_id>/delete-confirm")
@login_required
@admin_required
def admin_delete_confirm(merchant_id: int):
    m = db.session.get(Merchant, merchant_id) or abort(404)
    if _is_protected_merchant(m):
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

    # Same authoritative guard as the confirm page — never delete an admin or a
    # protected account (the POST previously used a smaller list than the GET).
    if _is_protected_merchant(m):
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
    """Platform-ops metrics console — every figure derived from the LEDGER.

    Read-only. All money is LIVE-scoped (is_test=False) and, for ledger
    Account balances, sign-flipped because psp/suspense accounts are stored
    credit-normal (guardrail 6). Each block is ONE aggregate query — no N+1.
    """
    from datetime import datetime, time, timedelta, timezone
    from flask import current_app
    from sqlalchemy import func as safunc
    from ..models import (
        Account, AccountType, AuditLog, Dispute, KYCApplication, Payout,
        PayoutStatus, ReconException, Subscription, SubscriptionPlan,
        Transaction, TxnStatus, WithdrawalRequest, utcnow,
    )
    from ..services.alerts import heartbeat_age_seconds

    now = utcnow()
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    stuck_cutoff = now - timedelta(hours=6)

    live_volume = (db.session.query(
        safunc.coalesce(safunc.sum(Transaction.amount), 0))
        .filter(Transaction.status == TxnStatus.SUCCEEDED,
                Transaction.is_test.is_(False)).scalar() or 0)
    active_merchants = (Merchant.query
        .filter(Merchant.is_active.is_(True),
                (Merchant.is_managed.is_(False)) | (Merchant.is_managed.is_(None)))
        .count())
    today_charge_count, today_charge_sum = (db.session.query(
        safunc.count(Transaction.id),
        safunc.coalesce(safunc.sum(Transaction.amount), 0))
        .filter(Transaction.is_test.is_(False),
                Transaction.created_at >= today_start).one())
    today_payout_count, today_payout_sum = (db.session.query(
        safunc.count(Payout.id),
        safunc.coalesce(safunc.sum(Payout.amount), 0))
        .filter(Payout.is_test.is_(False),
                Payout.created_at >= today_start).one())
    float_rows = dict(db.session.query(
        Account.type, safunc.coalesce(safunc.sum(Account.cached_balance), 0))
        .filter(Account.is_test.is_(False),
                Account.type.in_([AccountType.PSP_FLOAT, AccountType.SUSPENSE,
                                  AccountType.PSP_REVENUE]))
        .group_by(Account.type).all())
    platform_float    = -(float_rows.get(AccountType.PSP_FLOAT, 0) or 0)
    platform_suspense = -(float_rows.get(AccountType.SUSPENSE, 0) or 0)
    platform_revenue  = -(float_rows.get(AccountType.PSP_REVENUE, 0) or 0)

    mrr_rows = (db.session.query(
        SubscriptionPlan.interval,
        safunc.coalesce(safunc.sum(SubscriptionPlan.amount), 0))
        .join(Subscription, Subscription.plan_id == SubscriptionPlan.id)
        .filter(Subscription.status == "active",
                SubscriptionPlan.is_active.is_(True))
        .group_by(SubscriptionPlan.interval).all())
    _MONTHLY = {"weekly": 52.0 / 12.0, "monthly": 1.0, "yearly": 1.0 / 12.0}
    mrr = int(sum((amt or 0) * _MONTHLY.get((interval or "").lower(), 1.0)
                  for interval, amt in mrr_rows))

    recon_open_critical = (ReconException.query
        .filter_by(status="open", severity="critical").count())
    stranded_payouts = (Payout.query
        .filter(Payout.status == PayoutStatus.AUTHORIZED,
                Payout.created_at <= stuck_cutoff).count())
    stuck_charges = (Transaction.query
        .filter(Transaction.status == TxnStatus.AUTHORIZED,
                Transaction.created_at <= stuck_cutoff).count())
    hb_age = heartbeat_age_seconds()
    hb_stale_after = int(current_app.config.get("HEARTBEAT_STALE_SECONDS", 300))
    hb_state = ("unknown" if hb_age is None
                else "ok" if hb_age <= hb_stale_after else "stale")

    # Rail configuration — a mocked MTN rail in production is a TOTAL outage of
    # mobile money (checkout hides it, plans cannot be created, top-ups land in
    # the sandbox ledger) yet nothing used to say so. Surface it, with the raw
    # value, so the cause is obvious instead of taking an hour to find.
    import os as _os
    rail_mocked = bool(_os.environ.get("RENDER")) and not current_app.config.get("MOMO_USE_REAL")
    rail_raw = _os.environ.get("MOMO_USE_REAL", "(not set)")
    # Name the service too: env vars are PER SERVICE on Render, so "it works in
    # my shell" usually means the shell was a different service than the one
    # serving web traffic. This says which one you are actually looking at.
    rail_service = (_os.environ.get("RENDER_SERVICE_NAME")
                    or _os.environ.get("RENDER_SERVICE_ID") or "this service")
    rail_env_mismatch = None
    if current_app.config.get("MOMO_USE_REAL"):
        _base = str(current_app.config.get("MOMO_BASE_URL") or "")
        _tgt = str(current_app.config.get("MOMO_TARGET_ENV") or "")
        _cur = str(current_app.config.get("MOMO_CURRENCY") or "")
        _is_sandbox_url = "sandbox.momodeveloper" in _base
        if _is_sandbox_url and (_tgt != "sandbox" or _cur.upper() != "EUR"):
            rail_env_mismatch = (f"sandbox base URL with MOMO_TARGET_ENV={_tgt} / "
                                 f"MOMO_CURRENCY={_cur} — sandbox needs sandbox/EUR")
        elif not _is_sandbox_url and _tgt == "sandbox":
            rail_env_mismatch = (f"production base URL with MOMO_TARGET_ENV=sandbox "
                                 f"— production needs mtnuganda")

    kyc_pending = (KYCApplication.query
        .filter(KYCApplication.status.in_(["submitted", "under_review"])).count())
    withdrawals_pending = WithdrawalRequest.query.filter_by(status="pending").count()
    disputes_open = Dispute.query.filter_by(status="open").count()
    recon_open = ReconException.query.filter_by(status="open").count()
    recent_audits = (AuditLog.query
        .order_by(AuditLog.created_at.desc()).limit(20).all())

    return render_template(
        "admin.html",
        live_volume=live_volume,
        active_merchants=active_merchants,
        today_charge_count=today_charge_count, today_charge_sum=today_charge_sum,
        today_payout_count=today_payout_count, today_payout_sum=today_payout_sum,
        platform_float=platform_float, platform_suspense=platform_suspense,
        platform_revenue=platform_revenue, mrr=mrr,
        recon_open_critical=recon_open_critical,
        stranded_payouts=stranded_payouts, stuck_charges=stuck_charges,
        heartbeat_age=hb_age, heartbeat_state=hb_state,
        heartbeat_stale_after=hb_stale_after,
        kyc_pending=kyc_pending, withdrawals_pending=withdrawals_pending,
        disputes_open=disputes_open, recon_open=recon_open,
        recent_audits=recent_audits,
        rail_mocked=rail_mocked, rail_raw=rail_raw,
        rail_env_mismatch=rail_env_mismatch, rail_service=rail_service,
    )


@bp.get("/admin/flags")
@login_required
@admin_required
def admin_flags():
    """Platform-wide operational switchboard (freeze payouts, maintenance).

    Read-only view of every whitelisted PlatformFlag: current state plus who
    flipped it and when. Flag reads fail safe so this page always renders."""
    from ..services import platform_flags as pf
    flags = []
    for key, meta in pf.FLAG_META.items():
        row = pf.get_flag_row(key)
        flags.append({
            "key": key,
            "label": meta["label"],
            "description": meta["description"],
            "danger": meta["danger"],
            "enforced": meta["enforced"],
            "on": bool(row and row.value == "on"),
            "updated_by": row.updated_by if row else None,
            "updated_at": row.updated_at if row else None,
        })
    from ..services.settlement import get_hold_minutes, get_sandbox_hold_minutes
    return render_template("admin_flags.html", flags=flags,
                           settlement_hold_minutes=get_hold_minutes(),
                           sandbox_hold_minutes=get_sandbox_hold_minutes())


@bp.post("/admin/flags/set")
@login_required
@admin_required
def admin_set_flag():
    """Toggle one platform flag. Whitelisted keys only; value coerced on/off."""
    from ..services import platform_flags as pf
    key = request.form.get("key")
    if key not in pf.FLAG_META:
        abort(400)
    value = "on" if request.form.get("value") == "on" else "off"
    pf.set_flag(key, value, updated_by=current_user.email)
    _admin_log("platform.flag_set", None, by=current_user.email, flag=key, value=value)
    flash(f"{pf.FLAG_META[key]['label']} is now {value.upper()}.", "success")
    return redirect(url_for("dashboard.admin_flags"))


@bp.post("/admin/settlement-hold")
@login_required
@admin_required
def admin_set_settlement_hold():
    """Set how long a payment stays in `pending` before it clears to withdrawable
    `available`. Admin-adjustable at runtime, no deploy. Bounded 0 min .. 30 days.
    Applies to money not yet past its hold; already-aged money sweeps next run."""
    from ..services import platform_flags as pf
    scope = "sandbox" if request.form.get("scope") == "sandbox" else "live"
    try:
        minutes = int(request.form.get("minutes", ""))
    except (TypeError, ValueError):
        flash("Enter a whole number of minutes.", "error")
        return redirect(url_for("dashboard.admin_flags"))
    if not (0 <= minutes <= 43_200):
        flash("Settlement hold must be between 0 minutes and 30 days (43200 min).", "error")
        return redirect(url_for("dashboard.admin_flags"))
    key = pf.SANDBOX_SETTLEMENT_HOLD_MINUTES if scope == "sandbox" else pf.SETTLEMENT_HOLD_MINUTES
    pf.set_flag(key, str(minutes), updated_by=current_user.email)
    _admin_log("platform.settlement_hold_set", None, by=current_user.email,
               scope=scope, minutes=minutes)
    flash(f"{scope.title()} settlement hold is now {minutes} minute(s). New payments "
          f"clear that fast; money already past its hold clears on the next sweep.", "success")
    return redirect(url_for("dashboard.admin_flags"))


@bp.get("/dashboard/<int:merchant_id>")
@login_required
def merchant_detail(merchant_id: int):
    from ..models import Payout
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    # Fetch one extra row to know whether there IS a next page — the load-more
    # control was gated on a hardcoded 25 while the page size is 5, so it never
    # appeared. activity_has_more drives it honestly.
    _txn_rows = (
        Transaction.query.filter_by(merchant_id=merchant_id)
        .order_by(Transaction.id.desc())   # id-desc = stable load-more cursor
        .limit(_ACTIVITY_PAGE + 1)
        .all()
    )
    activity_has_more = len(_txn_rows) > _ACTIVITY_PAGE
    txns = _txn_rows[:_ACTIVITY_PAGE]
    po_pag = (
        Payout.query.filter_by(merchant_id=merchant_id)
        .order_by(Payout.created_at.desc())
        .paginate(page=page_arg("page_po"), per_page=10, error_out=False)
    )
    payouts = po_pag.items
    lk_pag = (
        PaymentLink.query.filter_by(merchant_id=merchant_id)
        .order_by(PaymentLink.created_at.desc())
        .paginate(page=page_arg("page_lk"), per_page=10, error_out=False)
    )
    links = lk_pag.items
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
    # LIVE money only (is_test=False) so these headline figures reconcile with the
    # live balance hero above them — otherwise sandbox test charges (which the mock
    # rail flips to SUCCEEDED) inflate "collected" while the balance excludes them.
    succeeded_count, succeeded_volume = (
        db.session.query(safunc.count(Transaction.id),
                         safunc.coalesce(safunc.sum(Transaction.amount), 0))
        .filter(Transaction.merchant_id == merchant_id,
                Transaction.status == TxnStatus.SUCCEEDED,
                Transaction.is_test.is_(False))
        .one()
    )
    payout_count = Payout.query.filter_by(merchant_id=merchant_id).count()
    active_link_count = PaymentLink.query.filter_by(
        merchant_id=merchant_id, is_active=True).count()
    from ..models import Dispute
    open_disputes = Dispute.query.filter_by(
        merchant_id=merchant_id, status="open").count()

    # Chart distributions from the FULL live history (GROUP BY), not the 5-row
    # activity slice — the doughnuts were reading only the most-recent 5 rows.
    status_counts = dict(
        db.session.query(Transaction.status, safunc.count(Transaction.id))
        .filter(Transaction.merchant_id == merchant_id, Transaction.is_test.is_(False))
        .group_by(Transaction.status).all())
    channel_counts = dict(
        db.session.query(Transaction.channel, safunc.count(Transaction.id))
        .filter(Transaction.merchant_id == merchant_id, Transaction.is_test.is_(False))
        .group_by(Transaction.channel).all())
    chart_status = {getattr(k, "value", str(k)): v for k, v in status_counts.items()}
    chart_channel = {getattr(k, "value", str(k)): v for k, v in channel_counts.items()}

    return render_template(
        "merchant_detail.html",
        open_disputes=open_disputes,
        merchant=merchant,
        txns=txns,
        activity_has_more=activity_has_more,
        chart_status=chart_status,
        chart_channel=chart_channel,
        payouts=payouts,
        po_pag=po_pag,
        links=links,
        lk_pag=lk_pag,
        pending_balance=-pending.cached_balance if pending else 0,
        available_balance=-available.cached_balance if available else 0,
        webhooks=webhooks,
        succeeded_count=succeeded_count,
        succeeded_volume=succeeded_volume,
        payout_count=payout_count,
        active_link_count=active_link_count,
    )


@bp.get("/dashboard/<int:merchant_id>/activity.json")
@login_required
def activity_page(merchant_id: int):
    """Next page of activity rows for the load-more/infinite scroll.

    Returns the SAME server-rendered partial the page uses (no client-side
    markup duplication), 25 rows per page, cursor = before_id (id-desc).
    Owner-or-admin gated like every dashboard surface.
    """
    from flask import jsonify
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    try:
        before_id = int(request.args.get("before_id", 0))
    except ValueError:
        abort(400)
    q = Transaction.query.filter_by(merchant_id=merchant_id)
    if before_id:
        q = q.filter(Transaction.id < before_id)
    rows = q.order_by(Transaction.id.desc()).limit(_ACTIVITY_PAGE + 1).all()
    has_more = len(rows) > _ACTIVITY_PAGE
    rows = rows[:_ACTIVITY_PAGE]
    html = render_template("_activity_rows.html", txns=rows, merchant=merchant)
    return jsonify(html=html, has_more=has_more,
                   next_before=(rows[-1].id if rows else None))


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
def export_transactions_csv(merchant_id: int):
    """Stream the merchant's transactions as a CSV statement.

    Owner-or-admin gated (never another merchant's data). Optional filters:
    status, after/before (ISO 8601). Includes a `mode` column so a merchant can
    tell test rows apart rather than us silently hiding them.
    """
    import csv
    import io
    from flask import Response
    if not merchant_or_admin(merchant_id):
        abort(403)
    db.session.get(Merchant, merchant_id) or abort(404)

    # SAME filters as the Transactions page (status/channel/mode/date/search),
    # so "Download CSV" gives exactly the rows on screen, never a wider set.
    base = Transaction.query.filter_by(merchant_id=merchant_id)
    q, _f = _txn_filters(base, model=Transaction)
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


def _txn_filters(q, *, model):
    """Apply the shared status/channel/mode/date/search filters to a Transaction
    query from request.args. Used by BOTH the Transactions page and its CSV
    export so a downloaded statement is EXACTLY the filtered view — never a
    different set of rows than what the merchant is looking at.

    Returns (query, filters_dict). Bad filter values 400 rather than silently
    returning everything (a merchant reconciling money must never be shown a
    wider set than they asked for and not know it)."""
    from datetime import datetime
    from sqlalchemy import or_
    from ..models import TxnStatus, Channel
    f = {"q": (request.args.get("q") or "").strip(),
         "status": request.args.get("status") or "",
         "channel": request.args.get("channel") or "",
         # Default to LIVE so the Transactions page (and its Collected total)
         # reconciles with the home dashboard, which is live-only. Sandbox test
         # charges (flipped SUCCEEDED by the mock rail) otherwise inflate the
         # figure. Pass ?mode=test explicitly to see sandbox rows.
         "mode": request.args.get("mode") or "live",
         "after": request.args.get("after") or "",
         "before": request.args.get("before") or ""}

    if f["status"]:
        try:
            q = q.filter(model.status == TxnStatus(f["status"]))
        except ValueError:
            abort(400, description=f"invalid status: {f['status']}")
    if f["channel"]:
        try:
            q = q.filter(model.channel == Channel(f["channel"]))
        except ValueError:
            abort(400, description=f"invalid channel: {f['channel']}")
    if f["mode"] == "live":
        q = q.filter(model.is_test.is_(False))
    elif f["mode"] == "test":
        q = q.filter(model.is_test.is_(True))

    def _dt(name):
        raw = f[name]
        if not raw:
            return None
        try:
            # accept a bare date (YYYY-MM-DD) from an <input type=date> too
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            abort(400, description=f"{name} must be a date or ISO 8601 datetime")
    after, before = _dt("after"), _dt("before")
    if after:
        q = q.filter(model.created_at >= after)
    if before:
        q = q.filter(model.created_at <= before)

    if f["q"]:
        like = f"%{f['q']}%"
        q = q.filter(or_(model.public_id.ilike(like),
                         model.merchant_reference.ilike(like),
                         model.customer_phone.ilike(like),
                         model.rail_reference.ilike(like)))
    return q, f


@bp.get("/dashboard/<int:merchant_id>/transactions")
@login_required
def transactions(merchant_id: int):
    """Dedicated, filterable, paginated transaction ledger — the view a busy
    merchant (KarlPOS, a vending route) needs to reconcile a month, not the
    5-row infinite scroll on Home. Search by reference/phone/confirmation code,
    filter by status/channel/mode/date, page through, and download the EXACT
    filtered set as CSV. Owner-or-admin gated like every dashboard surface."""
    from sqlalchemy import func as safunc, case
    from ..models import TxnStatus, Channel
    if not merchant_or_admin(merchant_id):
        abort(403)
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    base = Transaction.query.filter_by(merchant_id=merchant_id)
    q, f = _txn_filters(base, model=Transaction)
    pag = q.order_by(Transaction.id.desc()).paginate(
        page=page, per_page=50, error_out=False)

    # Totals for the FILTERED set (all pages), so the header reflects the query,
    # not just the 50 rows on screen. Succeeded-only sum = money actually earned.
    filtered_ids_q, _ = _txn_filters(
        Transaction.query.filter_by(merchant_id=merchant_id), model=Transaction)
    match_count, succ_count, succ_sum = (
        filtered_ids_q.with_entities(
            safunc.count(Transaction.id),
            safunc.coalesce(safunc.sum(
                case((Transaction.status == TxnStatus.SUCCEEDED, 1), else_=0)), 0),
            safunc.coalesce(safunc.sum(
                case((Transaction.status == TxnStatus.SUCCEEDED, Transaction.amount), else_=0)), 0),
        ).one())

    # querystring without `page`, so pagination links preserve the filters
    from urllib.parse import urlencode
    keep = {k: v for k, v in request.args.items() if k != "page" and v}
    qs = urlencode(keep)
    return render_template(
        "transactions.html", merchant=merchant, pag=pag, txns=pag.items, f=f,
        qs=qs, match_count=match_count, succ_count=succ_count, succ_sum=succ_sum,
        statuses=[s.value for s in TxnStatus],
        channels=[c.value for c in Channel])


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
    # Redirect-URL scheme guard: success_url/cancel_url are echoed into the
    # checkout status page (anchor href + window.location), so a `javascript:`
    # URI would be stored XSS. Only http(s) — same rule as the API's _safe_url.
    import re as _re
    _surl = request.form.get("success_url") or None
    _curl = request.form.get("cancel_url") or None
    for _u in (_surl, _curl):
        if _u and not _re.match(r"^https?://", str(_u)):
            return render_template(
                "new_link.html", merchant=merchant,
                error="Success/cancel URLs must start with http:// or https://.")
    link = PaymentLink(
        public_id=f"lnk_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        currency=request.form.get("currency", "UGX"),
        description=request.form.get("description") or None,
        reference=request.form.get("reference") or None,
        success_url=_surl,
        cancel_url=_curl,
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

    pag = (
        PaymentLink.query
        .filter(PaymentLink.merchant_id == merchant_id,
                PaymentLink.vending_meta.isnot(None))
        .order_by(PaymentLink.created_at.desc())
        .paginate(page=page_arg("page"), per_page=25, error_out=False)
    )
    orders = pag.items
    # Pair each order with its machine + payment status for the table.
    # Batch-load the orders' transactions in ONE query instead of one .get()
    # per row (N+1).
    _tids = [o.transaction_id for o in orders if o.transaction_id]
    _txns = {t.id: t for t in Transaction.query.filter(Transaction.id.in_(_tids)).all()} if _tids else {}
    rows = []
    for o in orders:
        meta = read_meta(o) or {}
        txn = _txns.get(o.transaction_id)
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
        pag=pag,
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

    # Include the per-payout flat fee in the pre-check — create_payout requires
    # available >= amount + fee PER ROW, so a CSV summing to exactly the balance
    # would pass here then fail partway with rows silently dropped.
    from ..services.fees import calculate_payout_fee
    total_amount = sum(r[2] for r in rows)
    total_fees = sum(calculate_payout_fee(amount=r[2], currency="UGX") for r in rows)
    total = total_amount + total_fees
    if total > available:
        return render_template(
            "bulk_payout.html", merchant=merchant, available=available,
            error=(
                f"Insufficient funds. Available: UGX {available:,}, "
                f"CSV needs UGX {total:,} (UGX {total_amount:,} + UGX {total_fees:,} "
                f"in 1.5% fees across {len(rows)} payouts)."
            ),
        )

    # Create the batch record and process each row inline. Fine for the modest
    # CSV sizes the dashboard accepts; a very large batch should move to the
    # Celery worker so the request returns immediately (tracked as a follow-up).
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

    # Never return to a normal dashboard implying everything sent — surface the
    # real per-row outcome (some rows can fail on bad phone, KYC, or fee shortfall).
    if failed:
        flash(f"Bulk payout: {created} sent, {failed} failed of {len(rows)}. "
              f"Failed rows were not paid — check the amounts/phones and retry them.",
              "warning" if created else "error")
    else:
        flash(f"Bulk payout: all {created} payouts submitted.", "success")
    return redirect(url_for("dashboard.merchant_detail", merchant_id=merchant.id))


@bp.get("/admin/reconciliation")
@login_required
@admin_required
def reconciliation():
    from ..models import ReconException
    report = run_reconciliation()
    pag = (ReconException.query.filter_by(status="open")
           .order_by(ReconException.severity, ReconException.created_at.desc())
           .paginate(page=page_arg("page"), per_page=50, error_out=False))
    recon_exceptions = pag.items
    return render_template("reconciliation.html", report=report,
                           recon_exceptions=recon_exceptions, pag=pag)


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


# ── Fraud / abuse anomaly feed ──────────────────────────────────────────────

@bp.get("/admin/anomalies")
@login_required
@admin_required
def anomalies():
    """Reviewable feed of detected fraud/abuse anomalies (charge storms,
    velocity, large charges, payout drain, refund outliers). The scans also
    page via alerts; this is the in-app record + the bank's monitoring trail."""
    from ..models import AnomalyEvent
    status = request.args.get("status", "open")
    q = AnomalyEvent.query
    if status in ("open", "resolved", "dismissed"):
        q = q.filter_by(status=status)
    pag = (q.order_by(AnomalyEvent.severity, AnomalyEvent.created_at.desc())
           .paginate(page=page_arg("page"), per_page=50, error_out=False))
    open_count = AnomalyEvent.query.filter_by(status="open").count()
    return render_template("anomalies.html", anomalies=pag.items, pag=pag,
                           status=status, open_count=open_count)


@bp.post("/admin/anomalies/scan")
@login_required
@admin_required
def run_anomaly_scan():
    """Run the charge-side anomaly scan on demand (payout/refund scans run on
    their beats)."""
    from flask import flash
    from ..services.anomaly import scan_charge_anomalies
    findings = scan_charge_anomalies()
    flash(f"Charge anomaly scan: {len(findings)} finding(s).",
          "error" if findings else "success")
    return redirect(url_for("dashboard.anomalies"))


@bp.post("/admin/anomalies/<int:anom_id>/<string:action>")
@login_required
@admin_required
def update_anomaly(anom_id: int, action: str):
    from datetime import datetime, timezone
    from flask import flash
    from flask_login import current_user
    from ..models import AnomalyEvent
    if action not in ("resolve", "dismiss"):
        abort(404)
    a = db.session.get(AnomalyEvent, anom_id) or abort(404)
    a.status = "resolved" if action == "resolve" else "dismissed"
    a.resolved_at = datetime.now(timezone.utc)
    a.resolved_by = current_user.email
    db.session.commit()
    flash(f"Anomaly {a.kind} marked {a.status}.", "success")
    return redirect(url_for("dashboard.anomalies"))


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


# ---------- disputes (the customer recourse door, merchant side) ----------

@bp.get("/dashboard/<int:merchant_id>/disputes")
@login_required
@verified_required
def disputes_page(merchant_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    from ..models import Dispute
    merchant = db.session.get(Merchant, merchant_id) or abort(404)
    _disputes_q = (db.session.query(Dispute, Transaction)
                   .join(Transaction, Dispute.transaction_id == Transaction.id)
                   .filter(Dispute.merchant_id == merchant_id)
                   .order_by(Dispute.id.desc()))
    pag = db.paginate(_disputes_q, page=page_arg("page"), per_page=50, error_out=False)
    disputes = pag.items
    return render_template("disputes.html", merchant=merchant, disputes=disputes, pag=pag)


@bp.post("/dashboard/<int:merchant_id>/disputes/<int:dispute_id>/close")
@login_required
@verified_required
def close_dispute(merchant_id: int, dispute_id: int):
    if not merchant_or_admin(merchant_id):
        abort(403)
    from ..models import Dispute, utcnow
    d = db.session.get(Dispute, dispute_id)
    if d is None or d.merchant_id != merchant_id:
        abort(404)
    outcome = request.form.get("outcome")
    if outcome not in ("resolved", "dismissed"):
        abort(400)
    if d.status != "open":
        flash("This dispute is already closed.", "info")
        return redirect(url_for("dashboard.disputes_page", merchant_id=merchant_id))
    d.status = outcome
    d.resolution_note = (request.form.get("note") or "").strip()[:500] or None
    d.resolved_at = utcnow()
    db.session.commit()
    from ..services.audit import log_event
    log_event("dispute." + outcome, merchant_id=merchant_id,
              detail={"dispute": d.public_id, "note": d.resolution_note})
    db.session.commit()
    flash(f"Dispute {d.public_id} marked {outcome}.", "success")
    return redirect(url_for("dashboard.disputes_page", merchant_id=merchant_id))


# ── Global payment search + support detail ───────────────────────────────────
# The audit persona test: an admin gets a call saying "the customer paid but I
# don't see the money" and must answer it in five minutes. Before this the only
# way in was the merchant's own dashboard — you had to already KNOW which
# merchant it was. These two read-only screens search every charge on the
# platform and then show, on ONE page, everything that decides where the money
# went: the charge row, its journal entries, its webhook deliveries, the link
# it came from and the refund payout if there is one.

_PAYMENT_STATUSES = ("pending", "authorized", "succeeded", "failed", "refunded")


def _phone_search_key(q: str) -> str:
    """Last 9 digits of a typed number — the app's canonical phone key.

    Same convention as rails._phone_key, so 0700…, 256700… and spaced/dashed
    forms all resolve to the one stored number.
    """
    import re as _re
    digits = _re.sub(r"\D", "", q or "")
    return digits[-9:] if len(digits) >= 9 else digits


@bp.get("/admin/payments")
@login_required
@admin_required
def admin_payments():
    """Search every charge on the platform (all merchants)."""
    from sqlalchemy import or_
    from ..models import TxnStatus

    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    # Support usually cares about real money, so an empty search shows the live
    # ledger only. A deliberate search spans both modes (a sandbox charge is
    # exactly the kind of thing an integrator calls about) unless narrowed.
    mode = request.args.get("mode")
    if mode is None:
        mode = "" if q else "live"
    mode = mode if mode in ("live", "test") else ""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    query = db.session.query(Transaction, Merchant).join(
        Merchant, Transaction.merchant_id == Merchant.id
    )
    if q:
        like = f"%{q}%"
        conds = [
            Transaction.public_id.ilike(like),
            Transaction.merchant_reference.ilike(like),
            Transaction.rail_reference.ilike(like),
        ]
        key = _phone_search_key(q)
        if key:
            # Suffix match on the last-9 key: the stored number may carry a
            # country code the caller didn't read out.
            conds.append(Transaction.customer_phone.ilike(f"%{key}"))
        else:
            conds.append(Transaction.customer_phone.ilike(like))
        query = query.filter(or_(*conds))
    if status in _PAYMENT_STATUSES:
        query = query.filter(Transaction.status == TxnStatus(status))
    if mode == "live":
        query = query.filter(Transaction.is_test.is_(False))
    elif mode == "test":
        query = query.filter(Transaction.is_test.is_(True))

    pag = query.order_by(Transaction.id.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template(
        "admin_payments.html",
        pag=pag, rows=pag.items, q=q, status=status, mode=mode,
        statuses=_PAYMENT_STATUSES,
    )


@bp.get("/admin/payments/<public_id>")
@login_required
@admin_required
def admin_payment_detail(public_id: str):
    """Read-only support view of ONE charge — the page that answers the call.

    Strictly read-only: it never moves money and never mutates a row. Every
    remediation stays behind the existing (audited) merchant/admin tooling.
    """
    import json as _json
    from ..models import JournalEntry, Payout

    txn = Transaction.query.filter_by(public_id=public_id).first() or abort(404)
    merchant = db.session.get(Merchant, txn.merchant_id)

    entries = (
        db.session.query(JournalEntry, Account)
        .join(Account, JournalEntry.account_id == Account.id)
        .filter(JournalEntry.transaction_id == txn.id)
        .order_by(JournalEntry.id.asc())
        .all()
    )
    journal_total = sum(je.amount for je, _ in entries)

    deliveries = []
    for wh in (WebhookDelivery.query.filter_by(transaction_id=txn.id)
               .order_by(WebhookDelivery.id.desc()).limit(50).all()):
        try:
            env = _json.loads(wh.payload)
        except ValueError:
            env = {}
        deliveries.append({
            "row_id": wh.id,
            "event_id": env.get("id") or "—",
            "event": env.get("event") or "—",
            "status": wh.status,
            "attempts": wh.attempts,
            "code": wh.last_response_code,
            "url": wh.url,
            "created_at": wh.created_at,
        })

    link = PaymentLink.query.filter_by(transaction_id=txn.id).first()
    refund_payout = (
        db.session.get(Payout, txn.refund_payout_id)
        if txn.refund_payout_id else None
    )
    return render_template(
        "admin_payment_detail.html",
        txn=txn, merchant=merchant, entries=entries,
        journal_total=journal_total, deliveries=deliveries,
        link=link, refund_payout=refund_payout,
    )
