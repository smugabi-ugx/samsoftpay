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
