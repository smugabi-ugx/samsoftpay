"""Celery beat task: subscription billing."""
from ..celery_app import celery


@celery.task(name="app.tasks.billing.process_due_subscriptions")
def process_due_subscriptions() -> None:
    try:
        from ..services.subscriptions_service import bill_due
        result = bill_due()
        if result.get("attempted"):
            print(
                f"subscriptions: {result['attempted']} attempted, "
                f"{result['succeeded']} ok, {result['failed']} failed"
            )
    except Exception as exc:
        print(f"subscription billing error: {exc}")
