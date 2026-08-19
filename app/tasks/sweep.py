"""Celery beat task: automatic settlement sweep.

Runs every hour and moves merchant_pending -> merchant_available
for transactions older than hold_hours (default 24h).
"""
from ..celery_app import celery


@celery.task(name="app.tasks.sweep.auto_settlement_sweep")
def auto_settlement_sweep() -> None:
    from ..services.settlement import sweep_to_available
    try:
        moved = sweep_to_available(hold_hours=24)
        if moved:
            total = sum(moved.values())
            print(f"settlement sweep: moved {total} across {len(moved)} merchant(s)")
    except Exception as exc:
        print(f"settlement sweep error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.prune_old_rows")
def prune_old_rows() -> None:
    """Daily: delete delivered webhook rows and aged idempotency keys.

    Both tables grew unbounded (no retention anywhere). 30 days keeps plenty
    of debugging/replay history; the created_at / (status, next_attempt_at)
    indexes shipped in b5c6d7e8f9a0 make these deletes cheap.
    """
    from datetime import timedelta
    from ..extensions import db
    from ..models import IdempotencyKey, WebhookDelivery, utcnow

    cutoff = utcnow() - timedelta(days=30)
    try:
        sent = (db.session.query(WebhookDelivery)
                .filter(WebhookDelivery.status == "sent",
                        WebhookDelivery.created_at <= cutoff)
                .delete(synchronize_session=False))
        keys = (db.session.query(IdempotencyKey)
                .filter(IdempotencyKey.created_at <= cutoff)
                .delete(synchronize_session=False))
        db.session.commit()
        if sent or keys:
            print(f"prune: {sent} sent webhooks, {keys} idempotency keys")
    except Exception as exc:
        db.session.rollback()
        print(f"prune error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.resolve_stale_transactions")
def resolve_stale_transactions() -> None:
    """Hourly: resolve PENDING/AUTHORIZED charges older than an hour from
    MTN's own answer (succeed/fail/expire; skip on network error).

    This is the safety net the pollers now rely on: they no longer terminally
    fail a charge/payout at their 90s timeout (MTN can complete at 91s — that
    produced "money taken, no credit" and refund-while-delivered). Something
    must therefore eventually resolve stragglers, and rails_mtn_real.py always
    assumed a beat task did — but none was ever scheduled until now.
    """
    from ..services.sweep import sweep_stale_transactions
    try:
        result = sweep_stale_transactions(stale_minutes=60)
        if result.get("swept"):
            print(f"stale sweep: {result}")
    except Exception as exc:
        print(f"stale sweep error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.resolve_stale_payouts")
def resolve_stale_payouts() -> None:
    """Hourly: resolve payouts stuck AUTHORIZED from MTN's own transfer status.

    The OUTBOUND mirror of resolve_stale_transactions. Payouts previously had NO
    automated straggler net — if the 90s poller was lost, the payout sat
    AUTHORIZED until a human ran `flask stranded-payouts`. For the one flow where
    "money may have left but we don't know" is the worst case, that asymmetry was
    the gap. Skips on network-unknown (never refunds on an unknown outcome).
    """
    from ..services.sweep import sweep_stale_payouts
    try:
        result = sweep_stale_payouts(stale_minutes=60)
        if result.get("swept"):
            print(f"stale payout sweep: {result}")
    except Exception as exc:
        print(f"stale payout sweep error: {exc}")
        raise
