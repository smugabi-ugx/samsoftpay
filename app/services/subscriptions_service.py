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
    due = (
        Subscription.query
        .filter(
            Subscription.status == "active",
            Subscription.next_billing_at <= now,
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

        attempted += 1
        # Advance period before firing so we don't double-bill on retries
        sub.current_period_start = now
        sub.next_billing_at = _next_billing(now, plan.interval)
        db.session.commit()

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
            if txn.status in (TxnStatus.FAILED,):
                sub.failure_reason = txn.failure_reason or "charge_failed"
                sub.status = "failed"
                db.session.commit()
                failed += 1
            else:
                succeeded += 1
        except OrchestratorError as exc:
            sub.failure_reason = str(exc)
            sub.status = "failed"
            db.session.commit()
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
    if sub and sub.status == "paused":
        sub.status = "active"
        sub.next_billing_at = datetime.now(timezone.utc)  # bill on next worker tick
        sub.failure_reason = None
        db.session.commit()
