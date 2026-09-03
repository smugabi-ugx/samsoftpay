"""Scheduled payouts / payroll — recurring mobile-money disbursements.

The MIRROR of subscription billing (money IN) pointed at the existing payout rail
(money OUT). A merchant sets a ScheduledPayout (one number or a salary list,
daily/weekly/monthly) and forgets it; run_due() fires every due schedule.

Adds ZERO new money primitives — every disbursement goes through the existing
payouts.create_payout(), which resolves the rail before money moves (guardrail
13), row-locks the balance (5), keeps the ledger zero-sum (6), obeys the payout
kill switch (13), parks ambiguous failures AUTHORIZED (21), and draws the ledger
selected by g.api_mode (12).

What THIS engine owns is exactly-once-per-cycle: the atomic claim in run_due()
advances next_run_at BEFORE paying, so a double beat tick / worker restart can
never pay one cycle twice. A cycle the merchant can't fully fund PAUSES and
creates ZERO payouts — never a partial payroll.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from flask import g

from ..extensions import db
from ..models import AccountType, Channel, Merchant, PayoutStatus, ScheduledPayout
from . import ledger
from .fees import calculate_payout_fee
from .payouts import PayoutError, create_payout
from .platform_flags import payouts_frozen


class ScheduledPayoutError(Exception):
    """A permanent client rejection of a schedule config (bad interval, cap
    breach, empty recipients). Raised with ZERO writes (guardrail 13 shape)."""
    pass


_INTERVALS = {
    "daily":   timedelta(days=1),
    "weekly":  timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def _next_run(from_dt: datetime, interval: str) -> datetime:
    return from_dt + _INTERVALS.get(interval, timedelta(days=30))


def create_scheduled_payout(
    *,
    merchant: Merchant,
    amount: int,
    currency: str = "UGX",
    channel: Channel = Channel.MTN_MOMO,
    interval: str = "monthly",
    recipients: list,
    name: str | None = None,
    max_per_recipient: int | None = None,
    is_test: bool = False,
) -> ScheduledPayout:
    """Validate and persist a schedule. All validation runs BEFORE any write, so a
    rejected config moves no money and writes no rows (guardrail 13)."""
    if interval not in _INTERVALS:
        raise ScheduledPayoutError(f"interval must be one of {list(_INTERVALS)}")
    if amount <= 0:
        raise ScheduledPayoutError("amount must be positive")
    if not recipients:
        raise ScheduledPayoutError("at least one recipient is required")
    for r in recipients:
        if not r.get("phone"):
            raise ScheduledPayoutError("every recipient needs a phone")
    if max_per_recipient is not None and amount > max_per_recipient:
        raise ScheduledPayoutError(
            f"amount {amount} exceeds the per-recipient cap of {max_per_recipient}")

    now = datetime.now(timezone.utc)
    sp = ScheduledPayout(
        public_id=f"spo_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        name=name,
        amount=amount,
        currency=currency,
        channel=channel,
        interval=interval,
        recipients=json.dumps(
            [{"phone": r["phone"], "name": r.get("name")} for r in recipients]),
        max_per_recipient=max_per_recipient,
        status="active",
        is_test=is_test,
        next_run_at=now,   # immediately due; the next beat tick fires it
    )
    db.session.add(sp)
    db.session.commit()
    return sp


def run_due(app=None) -> dict:
    """Fire every ACTIVE schedule whose next_run_at is in the past.

    Returns {"attempted", "succeeded", "failed"}. Never partial-pays a cycle: a
    schedule that can't be fully funded pauses with zero payouts.
    """
    now = datetime.now(timezone.utc)
    due = (
        ScheduledPayout.query
        .filter(ScheduledPayout.status == "active",
                ScheduledPayout.next_run_at <= now)
        .all()
    )

    attempted = succeeded = failed = 0
    for sp in due:
        merchant = db.session.get(Merchant, sp.merchant_id)
        if not merchant or not merchant.is_active:
            continue

        # (a) KILL SWITCH — live schedules only, checked BEFORE any claim so the
        # schedule stays DUE and runs once the freeze lifts (guardrail 13).
        if not sp.is_test and payouts_frozen():
            continue

        try:
            recips = json.loads(sp.recipients) or []
        except (TypeError, ValueError):
            recips = []
        if not recips:
            continue

        # Per-recipient fee is 1.5% of the per-recipient amount (min 200, cap 5,000).
        fee = calculate_payout_fee(amount=sp.amount, currency=sp.currency)
        run_total = len(recips) * (sp.amount + fee)

        # (b) AFFORDABILITY PRE-CHECK UNDER A ROW LOCK (guardrail 5). The whole run
        # is refused atomically — never a partial payroll.
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant.id,
            currency=sp.currency, is_test=sp.is_test)
        ledger.lock_account_for_update(avail)
        available_positive = -avail.cached_balance   # stored as a negative credit
        if available_positive < run_total:
            sp.status = "paused"
            sp.failure_reason = (
                f"insufficient balance: have {available_positive}, need "
                f"{run_total} for {len(recips)} recipient(s)")[:255]
            db.session.commit()
            continue

        # (c) ATOMIC CLAIM — advance next_run_at BEFORE paying. A double beat fire
        # / second worker re-running this schedule matches zero rows and skips.
        claimed = (
            db.session.query(ScheduledPayout)
            .filter(ScheduledPayout.id == sp.id,
                    ScheduledPayout.status == "active",
                    ScheduledPayout.next_run_at <= now)
            .update({"last_run_at": now,
                     "next_run_at": _next_run(now, sp.interval)},
                    synchronize_session=False)
        )
        db.session.commit()
        if not claimed:
            continue
        db.session.refresh(sp)
        attempted += 1

        # (d) Pay each recipient. g.api_mode MUST match sp.is_test so create_payout
        # draws the SAME ledger the pre-check locked (guardrail 12).
        try:
            g.api_mode = "test" if sp.is_test else "live"
        except RuntimeError:
            pass

        run_ok = run_fail = 0
        last_err = None
        for r in recips:
            try:
                payout = create_payout(
                    merchant=merchant,
                    amount=sp.amount,
                    currency=sp.currency,
                    recipient_phone=r["phone"],
                    recipient_name=r.get("name"),
                    channel=sp.channel,
                    reference=(sp.public_id if sp.public_id else None),
                )
                if payout.status == PayoutStatus.FAILED:
                    run_fail += 1
                    last_err = payout.failure_reason
                else:
                    run_ok += 1
            except PayoutError as exc:
                run_fail += 1
                last_err = str(exc)
            except Exception as exc:
                db.session.rollback()
                run_fail += 1
                last_err = str(exc)
                # A crashed recipient used to be a bare print() to worker stdout —
                # money silently under-delivered with no on-call signal. Log at
                # ERROR and page (deduped; send_alert never raises).
                from flask import current_app
                from .alerts import send_alert
                current_app.logger.exception(
                    "scheduled payout %s recipient crashed", sp.public_id)
                send_alert(
                    "Scheduled payout recipient crashed",
                    f"Scheduled payout {sp.public_id} had a recipient crash: {exc}. "
                    f"The batch continued; this recipient was NOT paid.",
                    severity="warning", key=f"sched-payout-crash-{sp.public_id}")

        sp = db.session.get(ScheduledPayout, sp.id)
        if sp is not None:
            sp.failure_reason = (last_err or None) if run_fail else None
            db.session.commit()
        succeeded += run_ok
        failed += run_fail

    return {"attempted": attempted, "succeeded": succeeded, "failed": failed}


def pause_scheduled_payout(sp_id: int) -> None:
    sp = db.session.get(ScheduledPayout, sp_id)
    if sp and sp.status == "active":
        sp.status = "paused"
        db.session.commit()


def resume_scheduled_payout(sp_id: int) -> None:
    sp = db.session.get(ScheduledPayout, sp_id)
    if sp and sp.status == "paused":
        sp.status = "active"
        sp.next_run_at = datetime.now(timezone.utc)
        sp.failure_reason = None
        db.session.commit()


def cancel_scheduled_payout(sp_id: int) -> None:
    sp = db.session.get(ScheduledPayout, sp_id)
    if sp:
        sp.status = "cancelled"
        db.session.commit()
