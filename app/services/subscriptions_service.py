"""Subscription billing service.

Lifecycle:
  create_plan()         → SubscriptionPlan (merchant defines price + interval)
  subscribe()           → Subscription (customer enrolls; fires first charge immediately)
  bill_due()            → charges all active subscriptions whose next_billing_at <= now
  cancel_subscription() → sets status = cancelled
  pause_subscription()  → sets status = paused (skips billing until resumed)
  resume_subscription() → sets status = active, resets next_billing_at to now + interval
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..extensions import db
from ..models import (
    Channel,
    Merchant,
    Subscription,
    SubscriptionPlan,
    TxnStatus,
)


# ── interval helpers ──────────────────────────────────────────────────────────

_INTERVALS = {
    "weekly":  timedelta(weeks=1),
    "monthly": timedelta(days=30),
    "yearly":  timedelta(days=365),
}


def _next_billing(from_dt: datetime, interval: str) -> datetime:
    delta = _INTERVALS.get(interval, timedelta(days=30))
    return from_dt + delta


# ── dunning ────────────────────────────────────────────────────────────────
# A failed charge no longer churns the plan on the first miss. It goes past_due
# and is retried on this backoff; only after the last retry fails does it become
# 'failed'. Days chosen for Uganda's irregular wallet top-ups.
_DUNNING_BACKOFF_DAYS = [1, 2, 3, 5]
_MAX_DUNNING_RETRIES = len(_DUNNING_BACKOFF_DAYS)   # 4 retries over ~11 days


def _mark_charge_failed(sub: Subscription, reason: str, now: datetime) -> None:
    """Advance the dunning state machine after a failed subscription charge.

    First failures -> past_due with a scheduled next_retry_at. After the retries
    are exhausted -> failed (the plan finally churns). Commits.
    """
    sub.retry_count = (sub.retry_count or 0) + 1
    sub.failure_reason = reason
    if sub.retry_count > _MAX_DUNNING_RETRIES:
        sub.status = "failed"
        sub.next_retry_at = None
    else:
        sub.status = "past_due"
        days = _DUNNING_BACKOFF_DAYS[min(sub.retry_count - 1, len(_DUNNING_BACKOFF_DAYS) - 1)]
        sub.next_retry_at = now + timedelta(days=days)
    db.session.commit()


def _mark_charge_succeeded(sub: Subscription) -> None:
    """A charge (regular or dunning retry) went through — the plan is healthy."""
    sub.status = "active"
    sub.retry_count = 0
    sub.next_retry_at = None
    sub.failure_reason = None
    db.session.commit()


# ── plan management ───────────────────────────────────────────────────────────

def create_plan(
    *,
    merchant_id: int,
    name: str,
    description: str | None,
    amount: int,
    currency: str = "UGX",
    interval: str = "monthly",
    channel: Channel = Channel.MTN_MOMO,
) -> SubscriptionPlan:
    if interval not in _INTERVALS:
        raise ValueError(f"interval must be one of {list(_INTERVALS)}")
    plan = SubscriptionPlan(
        public_id=f"plan_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant_id,
        name=name,
        description=description,
        amount=amount,
        currency=currency,
        interval=interval,
        channel=channel,
    )
    db.session.add(plan)
    db.session.commit()
    return plan


# ── subscriber enrollment ─────────────────────────────────────────────────────

def subscribe(
    *,
    plan: SubscriptionPlan,
    customer_phone: str,
    customer_email: str | None = None,
) -> Subscription:
    now = datetime.now(timezone.utc)
    sub = Subscription(
        public_id=f"sub_{uuid.uuid4().hex[:16]}",
        merchant_id=plan.merchant_id,
        plan_id=plan.id,
        customer_phone=customer_phone,
        customer_email=customer_email,
        status="active",
        current_period_start=now,
        next_billing_at=now,    # bill immediately on first enrollment
    )
    db.session.add(sub)
    db.session.commit()
    return sub


# ── billing loop ──────────────────────────────────────────────────────────────

def bill_due(app=None) -> dict:
    """Charge all active subscriptions whose next_billing_at is in the past.

    Returns a summary dict with counts.
    Called by the worker every 60 seconds and by the CLI `bill-subscriptions`.
    """
    from .orchestrator import OrchestratorError, create_charge
    from flask import g

    now = datetime.now(timezone.utc)
    # Two kinds of work: a normal cycle (active + next_billing_at due) and a
    # dunning retry (past_due + next_retry_at due). Both are charged the same way.
    due = (
        Subscription.query
        .filter(
            db.or_(
                db.and_(Subscription.status == "active",
                        Subscription.next_billing_at <= now),
                db.and_(Subscription.status == "past_due",
                        Subscription.next_retry_at.isnot(None),
                        Subscription.next_retry_at <= now),
            )
        )
        .all()
    )

    attempted = succeeded = failed = 0
    for sub in due:
        plan = db.session.get(SubscriptionPlan, sub.plan_id)
        if not plan or not plan.is_active:
            sub.status = "cancelled"
            db.session.commit()
            continue

        merchant = db.session.get(Merchant, sub.merchant_id)
        if not merchant or not merchant.is_active:
            continue

        # ATOMIC per-row claim so overlapping beat runs (60s beat, 2 workers,
        # multi-second MTN charges) can't double-charge one subscriber. A retry
        # is claimed by clearing next_retry_at; a normal cycle by advancing
        # next_billing_at. The WHERE re-checks the due condition, so exactly one
        # run wins each row and the loser skips it.
        is_retry = sub.status == "past_due"
        if is_retry:
            claimed = (
                db.session.query(Subscription)
                .filter(Subscription.id == sub.id,
                        Subscription.status == "past_due",
                        Subscription.next_retry_at <= now)
                .update({"next_retry_at": None}, synchronize_session=False)
            )
        else:
            claimed = (
                db.session.query(Subscription)
                .filter(Subscription.id == sub.id,
                        Subscription.status == "active",
                        Subscription.next_billing_at <= now)
                .update({"current_period_start": now,
                         "next_billing_at": _next_billing(now, plan.interval)},
                        synchronize_session=False)
            )
        db.session.commit()
        if not claimed:
            continue   # a concurrent run already owns this subscription's cycle
        db.session.refresh(sub)   # the UPDATE bypassed the identity map
        attempted += 1

        # Set api_mode to live for the charge (subscriptions are always live)
        try:
            g.api_mode = "live"
        except RuntimeError:
            pass   # may be called outside request context

        try:
            txn = create_charge(
                merchant=merchant,
                amount=plan.amount,
                currency=plan.currency,
                channel=plan.channel,
                customer_phone=sub.customer_phone,
                customer_email=sub.customer_email,
                merchant_reference=f"sub_{sub.public_id}",
            )
            if txn.status == TxnStatus.FAILED:
                # Synchronous rejection (e.g. fee>=amount, blocked channel).
                # DUNNING: don't churn on the first miss — go past_due and retry.
                _mark_charge_failed(sub, txn.failure_reason or "charge_failed", now)
                failed += 1
            elif txn.status == TxnStatus.SUCCEEDED:
                _mark_charge_succeeded(sub)
                succeeded += 1
            else:
                # AUTHORIZED / PENDING — the MoMo prompt was accepted but the
                # outcome is still in flight. Do NOT touch the dunning state
                # here (treating in-flight as success used to prematurely clear
                # a subscription that then failed on the callback). The async
                # completion hook `orchestrator._maybe_update_subscription`
                # resolves it when the charge actually succeeds or fails.
                pass
        except OrchestratorError as exc:
            _mark_charge_failed(sub, str(exc), now)
            failed += 1
        except Exception as exc:
            # A non-Orchestrator crash (network timeout, DB blip) used to
            # abort the WHOLE batch mid-run — every subscription after this
            # one silently skipped its cycle. The cycle was already claimed
            # above, so continuing is safe (no double-bill); this one just
            # misses this attempt and stays where it is for the next tick.
            db.session.rollback()
            # Was a bare print() — a crashed cycle billed nobody and paged no one.
            # Log at ERROR and alert (deduped; send_alert never raises).
            from flask import current_app
            from .alerts import send_alert
            current_app.logger.exception("subscription %s billing crashed", sub.public_id)
            send_alert(
                "Subscription billing crashed",
                f"Subscription {sub.public_id} billing crashed: {exc}. "
                f"The batch continued; this cycle was NOT billed.",
                severity="warning", key=f"sub-billing-crash-{sub.public_id}")
            failed += 1

    return {"attempted": attempted, "succeeded": succeeded, "failed": failed}


# ── lifecycle operations ───────────────────────────────────────────────────────

def cancel_subscription(sub_id: int) -> None:
    sub = db.session.get(Subscription, sub_id)
    if sub:
        sub.status = "cancelled"
        sub.cancelled_at = datetime.now(timezone.utc)
        db.session.commit()


def pause_subscription(sub_id: int) -> None:
    sub = db.session.get(Subscription, sub_id)
    if sub and sub.status == "active":
        sub.status = "paused"
        db.session.commit()


def resume_subscription(sub_id: int) -> None:
    sub = db.session.get(Subscription, sub_id)
    # Resumable from paused OR past_due (a merchant/customer fixing a dunning
    # situation) — clear the dunning counters so it starts clean.
    if sub and sub.status in ("paused", "past_due"):
        sub.status = "active"
        sub.next_billing_at = datetime.now(timezone.utc)  # bill on next worker tick
        sub.failure_reason = None
        sub.retry_count = 0
        sub.next_retry_at = None
        db.session.commit()
