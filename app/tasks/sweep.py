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
