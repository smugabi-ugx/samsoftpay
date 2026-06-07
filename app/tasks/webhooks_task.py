"""Celery tasks: webhook delivery.

Two tasks:
- deliver_webhook(delivery_id)  — deliver one webhook, called immediately when queued
- sweep_pending_webhooks()      — beat task, picks up any missed/retry-due webhooks
"""
from datetime import datetime, timezone
from ..celery_app import celery
from ..extensions import db


@celery.task(bind=True, max_retries=8, name="app.tasks.webhooks_task.deliver_webhook")
def deliver_webhook(self, delivery_id: int) -> None:
    """Attempt delivery of a single WebhookDelivery record."""
    import requests
    from ..models import WebhookDelivery
    from ..services.webhooks import sign_payload

    delivery = db.session.get(WebhookDelivery, delivery_id)
    if not delivery or delivery.status == "delivered":
        return

    backoff = [60, 300, 1800, 7200, 21600, 43200, 86400, 172800]

    try:
        sig = sign_payload(delivery.payload.encode(), delivery.signing_secret)
        resp = requests.post(
            delivery.url,
            data=delivery.payload,
            headers={
                "Content-Type": "application/json",
                "X-Samsoftpay-Signature": sig,
            },
            timeout=5,
        )
        if resp.status_code < 300:
            delivery.status = "delivered"
            delivery.delivered_at = datetime.now(timezone.utc)
        else:
            raise ValueError(f"HTTP {resp.status_code}")

    except Exception as exc:
        attempt = delivery.attempt_count or 0
        delivery.attempt_count = attempt + 1
        delivery.last_error = str(exc)[:500]

        if attempt < 8:
            delay = backoff[min(attempt, len(backoff) - 1)]
            delivery.status = "pending"
            delivery.next_attempt_at = datetime.now(timezone.utc).timestamp() + delay
            db.session.commit()
            raise self.retry(exc=exc, countdown=delay)
        else:
            delivery.status = "failed"

    db.session.commit()


@celery.task(name="app.tasks.webhooks_task.sweep_pending_webhooks")
def sweep_pending_webhooks() -> None:
    """Beat task: find overdue pending webhooks and re-queue them."""
    from ..models import WebhookDelivery

    now_ts = datetime.now(timezone.utc).timestamp()
    due = db.session.execute(
        db.select(WebhookDelivery)
        .where(WebhookDelivery.status == "pending")
        .where(WebhookDelivery.next_attempt_at <= now_ts)
        .limit(50)
    ).scalars().all()

    for d in due:
        deliver_webhook.delay(d.id)
