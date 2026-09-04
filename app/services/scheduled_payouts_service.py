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


# Valid interval keys. daily/weekly advance by a fixed delta; monthly advances by
# a real CALENDAR month (see _next_run) — not a fixed 30 days — so "pay on the
# 1st" stays on the 1st every month instead of drifting earlier each cycle.
_INTERVALS = {
    "daily":  timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": None,   # calendar-based, handled in _next_run
}


def _add_calendar_month(dt: datetime) -> datetime:
    """One calendar month later, preserving the day-of-month and clamping to the
    target month's length (e.g. Jan 31 -> Feb 28/29, Dec 15 -> Jan 15 next year)."""
    import calendar
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _next_run(from_dt: datetime, interval: str) -> datetime:
    if interval == "monthly":
        return _add_calendar_month(from_dt)
    delta = _INTERVALS.get(interval)
    return from_dt + (delta if delta is not None else timedelta(days=30))


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
    start_at: datetime | None = None,
) -> ScheduledPayout:
    """Validate and persist a schedule. All validation runs BEFORE any write, so a
    rejected config moves no money and writes no rows (guardrail 13).

    start_at (optional): when the FIRST run should happen. A future start_at makes
    the schedule dormant until then (next_run_at = start_at); omitted or in the
    past means immediately due, as before. There is no separate column — the first
    run IS next_run_at, which the engine already keys on."""
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
    first_run = now   # immediately due; the next beat tick fires it
    if start_at is not None:
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        # A future start defers the first run; a past start is treated as "now".
        first_run = start_at if start_at > now else now
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
        next_run_at=first_run,
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
                print(f"scheduled payout {sp.public_id} recipient crashed: {exc}")

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
