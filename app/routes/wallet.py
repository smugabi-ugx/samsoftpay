"""Wallet — settlement accounts, withdrawal requests, and top-up."""
import uuid

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (
    Account, AccountType, Merchant, Payout, PayoutStatus,
    PaymentLink, SettlementAccount, TopUpRequest, TxnStatus, WithdrawalRequest,
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
        merchant_id=current_user.id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
    ).first()
    pending_acct = Account.query.filter_by(
        merchant_id=current_user.id, type=AccountType.MERCHANT_PENDING, is_test=False
    ).first()
    available = -avail_acct.cached_balance if avail_acct else 0
    pending   = -pending_acct.cached_balance if pending_acct else 0

    topups = (TopUpRequest.query
              .filter_by(merchant_id=current_user.id)
              .order_by(TopUpRequest.created_at.desc())
              .limit(10).all())

    # First-class settlements (live ledger): every release of pending ->
    # available, newest first — the "you can set a watch by it" table.
    from ..models import Settlement
    settlements = (Settlement.query
                   .filter_by(merchant_id=current_user.id, is_test=False)
                   .order_by(Settlement.id.desc())
                   .limit(15).all())

    # NO SILENT HOLDS (Flutterwave counterexample: a merchant who discovers a
    # frozen balance without warning never leaves money in again). If this
    # merchant's payouts are paused, say so HERE, with the reason, before they
    # hit the error on submit.
    payouts_paused_reason = None
    from ..models import ReconException
    from ..services import platform_flags
    if platform_flags.payouts_frozen():
        payouts_paused_reason = (
            "Payouts are temporarily paused platform-wide for a safety review. "
            "Money in is unaffected and your balance is safe.")
    else:
        n_open = ReconException.query.filter_by(
            merchant_id=current_user.id, status="open", severity="critical").count()
        if n_open:
            payouts_paused_reason = (
                f"Payouts on your account are paused while {n_open} payment "
                f"record{'s are' if n_open != 1 else ' is'} reconciled with MTN. "
                "Support has been notified; your balance is safe and money in "
                "is unaffected.")

    return render_template("wallet.html",
        accounts=accounts, withdrawals=withdrawals,
        available=available, pending=pending,
        withdrawal_fee=_WITHDRAWAL_FEE,
        topups=topups,
        settlements=settlements,
        payouts_paused_reason=payouts_paused_reason,
    )


@bp.get("/dashboard/wallet/statement.csv")
@login_required
@verified_required
def statement_csv():
    """Merchant statement from the JOURNAL — the same source of truth as
    /v1/balance (MTN *165# lesson: a balance the merchant can audit themselves
    is a balance they'll leave money in). Live ledger only; one line per
    journal entry on the merchant's pending/available accounts.

    ?month=YYYY-MM limits to a calendar month (default: everything).
    """
    import csv
    import io as _io
    from datetime import datetime, timedelta
    from ..models import JournalEntry

    q = (db.session.query(JournalEntry, Account)
         .join(Account, JournalEntry.account_id == Account.id)
         .filter(Account.merchant_id == current_user.id,
                 Account.is_test.is_(False),
                 Account.type.in_([AccountType.MERCHANT_PENDING,
                                   AccountType.MERCHANT_AVAILABLE])))
    month = request.args.get("month")
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError:
            abort(400)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        q = q.filter(JournalEntry.created_at >= start,
                     JournalEntry.created_at < end)
    rows = q.order_by(JournalEntry.id).all()

    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "entry_id", "account", "memo", "amount", "currency"])
    for je, acct in rows:
        # Journal stores merchant money credit-normal (negative) — flip so the
        # statement reads like a bank statement: positive = money to you.
        w.writerow([
            je.created_at.isoformat() if je.created_at else "",
            je.id,
            "available" if acct.type == AccountType.MERCHANT_AVAILABLE else "pending",
            (je.memo or "").replace("\n", " "),
            -je.amount,
            acct.currency,
        ])
    from flask import Response
    fname = f"samsoftpay-statement-{current_user.handle or current_user.id}"
    if month:
        fname += f"-{month}"
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}.csv"})


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
        merchant_id=current_user.id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
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
    """Settle any due (past-hold) pending funds to available, now.

    This used to call sweep_stale_transactions(stale_minutes=0) — the stale
    charge EXPIRER, with no merchant filter — so one merchant tapping their
    own "sweep" button terminally FAILED every in-flight transaction on the
    whole platform (and made serial MTN HTTP calls inside the request).
    Found independently by three agents of the engineering audit.

    sweep_to_available is the real settlement operation: idempotent
    (settled_at guard), respects the 24h hold, commits per merchant.
    """
    from ..services.settlement import sweep_to_available
    from ..models import Account, AccountType

    def _live_available():
        row = Account.query.filter_by(merchant_id=current_user.id,
                                      type=AccountType.MERCHANT_AVAILABLE,
                                      is_test=False).first()
        return -row.cached_balance if row else 0

    # The flash used to report sandbox settlements as if real money settled
    # ("Settled UGX 29,550" while live available stayed 0). Measure the LIVE
    # delta directly; mention test-mode movement separately.
    live_before = _live_available()
    moved = sweep_to_available(hold_hours=24)
    my_moved = moved.get(current_user.id, 0) if isinstance(moved, dict) else 0
    live_delta = _live_available() - live_before
    test_delta = max(0, my_moved - live_delta)
    if live_delta:
        msg = f"Settled UGX {live_delta:,} from pending to available."
        if test_delta:
            msg += f" (+UGX {test_delta:,} in test mode)"
        flash(msg, "success")
    elif test_delta:
        flash(f"Settled UGX {test_delta:,} in TEST mode only — no live funds were due.", "info")
    else:
        flash("Nothing due yet — funds settle automatically 24h after a payment succeeds.", "info")
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
    # Name lookup for the tables — the template used to show raw merchant ids,
    # which forced the admin to cross-reference by hand before approving money.
    ids = {w.merchant_id for w in pending_wrs} | {w.merchant_id for w in all_wrs} | {a.merchant_id for a in unverified}
    merchant_names = {m.id: m.name for m in Merchant.query.filter(Merchant.id.in_(ids)).all()} if ids else {}
    return render_template("admin_withdrawals.html",
        pending_wrs=pending_wrs, all_wrs=all_wrs, unverified=unverified,
        merchant_names=merchant_names)


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
    # Lock + re-check: two concurrent approvals (double-click, two admin tabs)
    # both saw "pending" and both created a real payout — money out twice.
    db.session.refresh(wr, with_for_update=True)
    if wr.status != "pending":
        db.session.commit()
        flash("This withdrawal is no longer pending.", "error")
        return redirect(url_for("wallet.admin_withdrawals"))
    # Claim it before touching the rail (create_payout commits internally,
    # which releases our lock — the claim must be visible first).
    wr.status = "processing"
    db.session.commit()

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
    # Row-lock + only-if-pending (mirrors the approve guard): an approved
    # request whose payout may already have DELIVERED must never flip to
    # 'rejected' — the record would deny money that actually left.
    db.session.refresh(wr, with_for_update=True)
    if wr.status != "pending":
        db.session.commit()   # release the lock
        flash(f"Request is already '{wr.status}' — nothing to reject.", "warning")
        return redirect(url_for("wallet.admin_withdrawals"))
    wr.status      = "rejected"
    wr.admin_notes = request.form.get("reason", "Rejected by admin").strip()
    wr.processed_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Withdrawal rejected.", "info")
    return redirect(url_for("wallet.admin_withdrawals"))


# ── Wallet Top-Up ──────────────────────────────────────────────────────────────

@bp.post("/dashboard/wallet/topup/momo")
@login_required
@verified_required
def topup_momo():
    """Create a PaymentLink for MoMo top-up — merchant scans QR and pays."""
    try:
        amount = int(request.form.get("topup_amount", 0))
    except ValueError:
        flash("Invalid amount.", "error")
        return redirect(url_for("wallet.wallet_home"))
    if amount < 1000:
        flash("Minimum MoMo top-up is UGX 1,000.", "error")
        return redirect(url_for("wallet.wallet_home"))

    merchant = db.session.get(Merchant, current_user.id)

    # Until a REAL MTN rail is active in production, a live top-up link has no
    # mobile-money option at all (guardrail 14 hides mock rails on live links)
    # — the page showed only Crypto. The fix is NOT to unhide the mock on a
    # live link: a fake "payment" would then credit real withdrawable balance.
    # Instead the link is created in SANDBOX mode, the flow is fully testable,
    # and the credit lands on the sandbox ledger (see _settle_topup). The
    # moment MOMO_USE_REAL goes live, top-ups become live automatically.
    from ..services.rails import is_simulated
    from ..models import Channel as _Ch
    mtn_is_mock = is_simulated(_Ch.MTN_MOMO)   # g.api_mode unset here = live view
    if mtn_is_mock:
        flash("MTN is running in sandbox until production launch — this top-up "
              "tests the flow and credits your SANDBOX balance, not real money.", "info")

    link = PaymentLink(
        public_id           = f"lnk_{uuid.uuid4().hex[:16]}",
        merchant_id         = merchant.id,
        amount              = amount,
        currency            = "UGX",
        description         = f"Wallet top-up — {merchant.name}",
        reference           = f"TOPUP-{merchant.id}",
        allow_multiple_uses = False,
        is_active           = True,
        is_test             = mtn_is_mock,
    )
    db.session.add(link)
    db.session.flush()

    from ..models import TopUpRequest
    topup = TopUpRequest(
        public_id       = f"tu_{uuid.uuid4().hex[:16]}",
        merchant_id     = merchant.id,
        method          = "momo",
        amount          = amount,
        status          = "pending",
        payment_link_id = link.id,
    )
    db.session.add(topup)
    db.session.commit()
    return redirect(url_for("wallet.topup_qr_page", topup_public_id=topup.public_id))


@bp.get("/dashboard/wallet/topup/<topup_public_id>/qr")
@login_required
@verified_required
def topup_qr_page(topup_public_id: str):
    from ..models import TopUpRequest
    topup = TopUpRequest.query.filter_by(
        public_id=topup_public_id, merchant_id=current_user.id
    ).first_or_404()
    link = db.session.get(PaymentLink, topup.payment_link_id) if topup.payment_link_id else None
    checkout_url = url_for("checkout.checkout_page", public_id=link.public_id, _external=True) if link else None
    return render_template("topup_qr.html", topup=topup, link=link, checkout_url=checkout_url)


@bp.get("/dashboard/wallet/topup/<topup_public_id>/status.json")
@login_required
def topup_status_json(topup_public_id: str):
    from datetime import datetime, timezone
    from ..models import TopUpRequest, Transaction
    topup = TopUpRequest.query.filter_by(
        public_id=topup_public_id, merchant_id=current_user.id
    ).first_or_404()

    if topup.status == "pending" and topup.payment_link_id:
        # Row-lock the topup and RE-CHECK under the lock: this settles money
        # from a GET polling endpoint, and two concurrent polls both saw
        # status=='pending' and both posted the pending->available move.
        # FOR UPDATE serialises them (no-op on SQLite, same as the ledger's
        # lock_account_for_update precedent).
        db.session.refresh(topup, with_for_update=True)
        if topup.status != "pending":
            db.session.commit()   # release the lock
            return jsonify(status=topup.status, amount=topup.amount, currency=topup.currency)
        link = db.session.get(PaymentLink, topup.payment_link_id)
        if link and link.transaction_id:
            txn = db.session.get(Transaction, link.transaction_id)
            if txn and txn.status == TxnStatus.SUCCEEDED:
                _settle_topup(topup.merchant_id, txn, topup.public_id)
                topup.status         = "completed"
                topup.transaction_id = txn.id
                topup.processed_at   = datetime.now(timezone.utc)
                db.session.commit()
            elif txn and txn.status == TxnStatus.FAILED:
                topup.status      = "rejected"
                topup.admin_notes = txn.failure_reason or "Payment failed"
                db.session.commit()

    return jsonify(status=topup.status, amount=topup.amount, currency=topup.currency)


def _settle_topup(merchant_id: int, txn, ref: str) -> None:
    """Settle a completed top-up: move net amount from pending → available.

    Platform keeps the 1.5% collection fee (min UGX 200) as normal revenue.
    Merchant depositing 1,000 UGX gets 800 UGX available — 200 goes to platform.
    This is the same fee applied to all collections.
    """
    from ..services import ledger

    net_amount = txn.amount - txn.fee_amount   # e.g. 1000 - 200 = 800
    currency   = txn.currency

    # Scoped to the TRANSACTION's mode, never hardcoded live: a sandbox top-up
    # (mock MTN pre-production) must credit the sandbox ledger only. Hardcoding
    # is_test=False here would let a fake payment mint real withdrawable
    # balance — the exact hole guardrails 12/14 exist to close.
    mode = bool(txn.is_test)
    pending = ledger.get_or_create_account(
        type=AccountType.MERCHANT_PENDING,  merchant_id=merchant_id, currency=currency,
        is_test=mode)
    avail   = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id, currency=currency,
        is_test=mode)

    # Move net from pending → available. Fee stays in psp_revenue.
    ledger.post(
        [(pending, +net_amount), (avail, -net_amount)],
        currency=currency,
        memo=f"top-up settle {ref}",
    )
    # CRITICAL: mark the transaction settled. Without this the hourly
    # settlement sweep (which selects settled_at IS NULL) moved the SAME
    # pending->available amount AGAIN 24h later — every top-up double-credited.
    from ..models import utcnow as _utcnow
    txn.settled_at = _utcnow()


def _credit_wallet(merchant_id: int, amount: int, ref: str) -> None:
    """Used ONLY for bank/admin top-up approvals where no prior charge was made."""
    from ..services import ledger
    avail = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id, currency="UGX",
        is_test=False,
    )
    psp_float = ledger.get_or_create_account(
        type=AccountType.PSP_FLOAT, merchant_id=None, currency="UGX", is_test=False,
    )
    ledger.post([(psp_float, +amount), (avail, -amount)],
                currency="UGX", memo=f"bank/admin top-up {ref}")


@bp.post("/dashboard/wallet/topup/bank")
@login_required
@verified_required
def topup_bank():
    try:
        amount = int(request.form.get("bank_amount", 0))
    except ValueError:
        flash("Invalid amount.", "error")
        return redirect(url_for("wallet.wallet_home"))
    if amount < 10000:
        flash("Minimum bank top-up is UGX 10,000.", "error")
        return redirect(url_for("wallet.wallet_home"))

    from ..models import TopUpRequest
    topup = TopUpRequest(
        public_id   = f"tu_{uuid.uuid4().hex[:16]}",
        merchant_id = current_user.id,
        method      = "bank",
        amount      = amount,
        status      = "pending",
        bank_name   = request.form.get("bank_name", "").strip() or None,
        reference   = request.form.get("bank_ref", "").strip() or None,
    )
    db.session.add(topup)
    db.session.commit()
    flash("Bank top-up submitted. We'll verify your reference and credit your wallet within 1 business day.", "success")
    return redirect(url_for("wallet.wallet_home"))


@bp.get("/admin/topups")
@login_required
@admin_required
def admin_topups():
    from ..models import TopUpRequest
    pending   = TopUpRequest.query.filter_by(status="pending").order_by(TopUpRequest.created_at.asc()).all()
    all_items = TopUpRequest.query.order_by(TopUpRequest.created_at.desc()).limit(50).all()
    return render_template("admin_topups.html", pending=pending, all_items=all_items)


@bp.post("/admin/topups/<int:req_id>/approve")
@login_required
@admin_required
def admin_approve_topup(req_id: int):
    from datetime import datetime, timezone
    from ..models import TopUpRequest
    topup = db.session.get(TopUpRequest, req_id) or abort(404)
    # Lock + re-check: two concurrent approvals both saw "pending" and both
    # credited the wallet — real balance minted twice.
    db.session.refresh(topup, with_for_update=True)
    if topup.status != "pending":
        db.session.commit()
        flash("Already processed.", "error")
        return redirect(url_for("wallet.admin_topups"))
    _credit_wallet(topup.merchant_id, topup.amount, topup.public_id)
    topup.status       = "completed"
    topup.admin_notes  = f"Approved by {current_user.email}"
    topup.processed_at = datetime.now(timezone.utc)
    db.session.commit()
    flash(f"UGX {topup.amount:,} credited to merchant #{topup.merchant_id}.", "success")
    return redirect(url_for("wallet.admin_topups"))


@bp.post("/admin/topups/<int:req_id>/reject")
@login_required
@admin_required
def admin_reject_topup(req_id: int):
    from datetime import datetime, timezone
    from ..models import TopUpRequest
    topup = db.session.get(TopUpRequest, req_id) or abort(404)
    # Same only-if-pending guard: a completed top-up already credited the
    # wallet — flipping it to 'rejected' left the money with a lying record.
    db.session.refresh(topup, with_for_update=True)
    if topup.status != "pending":
        db.session.commit()
        flash(f"Top-up is already '{topup.status}' — nothing to reject.", "warning")
        return redirect(url_for("wallet.admin_topups"))
    topup.status       = "rejected"
    topup.admin_notes  = request.form.get("reason", "Rejected").strip()
    topup.processed_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Top-up request rejected.", "info")
    return redirect(url_for("wallet.admin_topups"))
