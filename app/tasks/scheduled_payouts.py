"""Celery beat task: scheduled payouts / payroll (money-OUT autopilot)."""
from ..celery_app import celery


@celery.task(name="app.tasks.scheduled_payouts.run_scheduled_payouts")
def run_scheduled_payouts() -> None:
    try:
        from ..services.scheduled_payouts_service import run_due
        result = run_due()
        if result.get("attempted"):
            print(
                f"scheduled payouts: {result['attempted']} schedules run, "
                f"{result['succeeded']} paid, {result['failed']} failed"
            )
    except Exception as exc:
        print(f"scheduled payout run error: {exc}")
        raise   # re-raise so the failure is VISIBLE (Sentry/Celery)
