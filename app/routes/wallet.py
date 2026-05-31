"""Wallet — settlement accounts, withdrawal requests, and top-up."""
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    Account, AccountType, Merchant, Payout, PayoutStatus,
    SettlementAccount, WithdrawalRequest,
)
from ..utils import admin_required, verified_required

bp = Blueprint("wallet", __name__)

_WITHDRAWAL_FEE = 750   # UGX flat (same as standard payout)

# ── Settlement accounts ────────────────────────────────────────────────────────

@bp.get("/dashboard/wallet")
@login_required
@verified_required
def wallet_home():
    accounts   = SettlementAccount.query.filter_by(merchant_id=current_user.id).all()
    withdrawals = (WithdrawalRequest.query
                   .filter_by(merchant_id=current_user.id)
                   .order_by(WithdrawalRequest.created_at.desc())
                   .limit(20).all())
    avail_acct = Account.query.filter_by(
        merchant_id=current_user.id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    pending_acct = Account.query.filter_by(
        merchant_id=current_user.id, type=AccountType.MERCHANT_PENDING
    ).first()
    available = -avail_acct.cached_balance if avail_acct else 0
    pending   = -pending_acct.cached_balance if pending_acct else 0

    return render_template("wallet.html",
        accounts=accounts, withdrawals=withdrawals,
        available=available, pending=pending,
        withdrawal_fee=_WITHDRAWAL_FEE,
    )


@bp.post("/dashboard/wallet/add-account")
@login_required
@verified_required
def add_account():
    acct_type   = request.form.get("account_type", "")
    acct_number = request.form.get("account_number", "").strip()
    acct_name   = request.form.get("account_name", "").strip()
    bank_name   = request.form.get("bank_name", "").strip() or None

    if not acct_type or not acct_number or not acct_name:
        flash("All required fields must be filled.", "error")
        return redirect(url_for("wallet.wallet_home"))

    # Only one primary per merchant
    has_primary = SettlementAccount.query.filter_by(
        merchant_id=current_user.id, is_primary=True
    ).first()

    acct = SettlementAccount(
        public_id      = f"sa_{uuid.uuid4().hex[:16]}",
        merchant_id    = current_user.id,
        account_type   = acct_type,
        account_number = acct_number,
        account_name   = acct_name,
        bank_name      = bank_name,
        is_primary     = not bool(has_primary),
        is_verified    = False,
    )
    db.session.add(acct)
    db.session.commit()
    flash("Account added. It will be verified by our team within 1 business day before you can withdraw.", "info")
    return redirect(url_for("wallet.wallet_home"))


@bp.post("/dashboard/wallet/remove-account/<int:acct_id>")
@login_required
@verified_required
def remove_account(acct_id: int):
    acct = SettlementAccount.query.filter_by(
        id=acct_id, merchant_id=current_user.id
    ).first_or_404()
    if WithdrawalRequest.query.filter_by(
        settlement_account_id=acct_id, status="pending"
    ).first():
        flash("Cannot remove an account with a pending withdrawal.", "error")
        return redirect(url_for("wallet.wallet_home"))
    db.session.delete(acct)
    db.session.commit()
    flash("Account removed.", "success")
    return redirect(url_for("wallet.wallet_home"))


@bp.post("/dashboard/wallet/set-primary/<int:acct_id>")
@login_required
@verified_required
def set_primary(acct_id: int):
    acct = SettlementAccount.query.filter_by(
        id=acct_id, merchant_id=current_user.id
    ).first_or_404()
    SettlementAccount.query.filter_by(merchant_id=current_user.id).update({"is_primary": False})
    acct.is_primary = True
    db.session.commit()
    flash(f"{acct.account_name} set as primary withdrawal account.", "success")
    return redirect(url_for("wallet.wallet_home"))


# ── Withdrawal requests ────────────────────────────────────────────────────────

@bp.post("/dashboard/wallet/withdraw")
@login_required
@verified_required
def request_withdrawal():
    from datetime import datetime, timezone
    from ..services import ledger

    acct_id = request.form.get("settlement_account_id")
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        flash("Invalid amount.", "error")
        return redirect(url_for("wallet.wallet_home"))

    if amount < 5000:
        flash("Minimum withdrawal is UGX 5,000.", "error")
        return redirect(url_for("wallet.wallet_home"))

    sa = SettlementAccount.query.filter_by(
        id=acct_id, merchant_id=current_user.id, is_verified=True
    ).first()
    if not sa:
        flash("Please add and verify a withdrawal account first.", "error")
        return redirect(url_for("wallet.wallet_home"))

    avail_acct = Account.query.filter_by(
        merchant_id=current_user.id, type=AccountType.MERCHANT_AVAILABLE
    ).first()
    available = -avail_acct.cached_balance if avail_acct else 0
    total_needed = amount + _WITHDRAWAL_FEE
    if available < total_needed:
        flash(f"Insufficient available balance. You have UGX {available:,} — need UGX {total_needed:,} (amount + UGX {_WITHDRAWAL_FEE:,} fee).", "error")
        return redirect(url_for("wallet.wallet_home"))

    # Create withdrawal request — admin approves and triggers the actual payout
    wr = WithdrawalRequest(
        public_id             = f"wd_{uuid.uuid4().hex[:16]}",
        merchant_id           = current_user.id,
        settlement_account_id = sa.id,
        amount                = amount,
        fee_amount            = _WITHDRAWAL_FEE,
        status                = "pending",
    )
    db.session.add(wr)
    db.session.commit()

    flash(f"Withdrawal request for UGX {amount:,} submitted. Processing within 1 business day.", "success")
    return redirect(url_for("wallet.wallet_home"))


@bp.post("/dashboard/wallet/withdraw/<int:wr_id>/cancel")
@login_required
@verified_required
def cancel_withdrawal(wr_id: int):
    wr = WithdrawalRequest.query.filter_by(
        id=wr_id, merchant_id=current_user.id, status="pending"
    ).first_or_404()
    wr.status = "cancelled"
    db.session.commit()
    flash("Withdrawal request cancelled.", "info")
    return redirect(url_for("wallet.wallet_home"))


# ── Manual sweep (pending → available) ───────────────────────────────────────

@bp.post("/dashboard/wallet/sweep")
@login_required
@verified_required
def manual_sweep():
    """Move eligible pending transactions to available balance."""
    from ..services.sweep import sweep_stale_transactions
    result = sweep_stale_transactions(stale_minutes=0)   # sweep all settled
    succeeded = result.get("succeeded", 0)
    if succeeded:
        flash(f"Swept {succeeded} transaction(s) from pending to available.", "success")
    else:
        flash("No transactions ready to settle yet. Transactions settle after confirmation.", "info")
    return redirect(url_for("wallet.wallet_home"))


# ── Admin: verify accounts & process withdrawals ──────────────────────────────

@bp.get("/admin/withdrawals")
@login_required
@admin_required
def admin_withdrawals():
    pending_wrs = (WithdrawalRequest.query
                   .filter_by(status="pending")
                   .order_by(WithdrawalRequest.created_at.asc()).all())
    all_wrs = (WithdrawalRequest.query
               .order_by(WithdrawalRequest.created_at.desc()).limit(50).all())
    unverified = (SettlementAccount.query
                  .filter_by(is_verified=False)
                  .order_by(SettlementAccount.created_at.asc()).all())
    return render_template("admin_withdrawals.html",
        pending_wrs=pending_wrs, all_wrs=all_wrs, unverified=unverified)


@bp.post("/admin/settlement-accounts/<int:acct_id>/verify")
@login_required
@admin_required
def admin_verify_account(acct_id: int):
    from datetime import datetime, timezone
    acct = db.session.get(SettlementAccount, acct_id) or abort(404)
    acct.is_verified = True
    acct.verified_at = datetime.now(timezone.utc)
    acct.verified_by = current_user.id
    db.session.commit()
    merchant = db.session.get(Merchant, acct.merchant_id)
    flash(f"Account for {merchant.name} verified: {acct.account_number}", "success")
    return redirect(url_for("wallet.admin_withdrawals"))


@bp.post("/admin/withdrawals/<int:wr_id>/approve")
@login_required
@admin_required
def admin_approve_withdrawal(wr_id: int):
    """Approve withdrawal — creates an actual payout via the disbursement rail."""
    from datetime import datetime, timezone
    from flask import g
    from ..models import Channel
    from ..services.payouts import PayoutError, create_payout

    wr = db.session.get(WithdrawalRequest, wr_id) or abort(404)
    if wr.status != "pending":
        flash("This withdrawal is no longer pending.", "error")
        return redirect(url_for("wallet.admin_withdrawals"))

    sa = db.session.get(SettlementAccount, wr.settlement_account_id)
    merchant = db.session.get(Merchant, wr.merchant_id)

    # Map account type to disbursement channel
    channel_map = {
        "momo_mtn":    Channel.MTN_MOMO,
        "momo_airtel": Channel.AIRTEL_MONEY,
        "bank":        Channel.MTN_MOMO,   # bank via MoMo bridge for now
    }
    channel = channel_map.get(sa.account_type, Channel.MTN_MOMO)

    g.api_mode = "live"
    try:
        payout = create_payout(
            merchant=merchant,
            amount=wr.amount,
            currency=wr.currency,
            recipient_phone=sa.account_number,
            recipient_name=sa.account_name,
            channel=channel,
        )
        wr.status       = "processing"
        wr.payout_id    = payout.id
        wr.processed_at = datetime.now(timezone.utc)
        wr.admin_notes  = f"Approved by {current_user.email}. Payout: {payout.public_id}"
        db.session.commit()
        flash(f"Withdrawal approved and payout {payout.public_id} created.", "success")
    except PayoutError as exc:
        wr.status     = "rejected"
        wr.admin_notes = f"Payout failed: {exc}"
        db.session.commit()
        flash(f"Could not create payout: {exc}", "error")

    return redirect(url_for("wallet.admin_withdrawals"))


@bp.post("/admin/withdrawals/<int:wr_id>/reject")
@login_required
@admin_required
def admin_reject_withdrawal(wr_id: int):
    from datetime import datetime, timezone
    wr = db.session.get(WithdrawalRequest, wr_id) or abort(404)
    wr.status      = "rejected"
    wr.admin_notes = request.form.get("reason", "Rejected by admin").strip()
    wr.processed_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Withdrawal rejected.", "info")
    return redirect(url_for("wallet.admin_withdrawals"))
