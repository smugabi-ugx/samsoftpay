"""Celery tasks: webhook delivery.

Two tasks:
- deliver_webhook(delivery_id)  — deliver one webhook, called immediately when queued
- sweep_pending_webhooks()      — beat task, picks up any missed/retry-due webhooks

These align with the WebhookDelivery model schema:
  status: pending | sent | failed
  attempts (int), signature (precomputed HMAC), next_attempt_at (DateTime),
  last_response_code (int), last_response_body (text)
"""
from ..celery_app import celery
from ..extensions import db


@celery.task(bind=True, max_retries=8, name="app.tasks.webhooks_task.deliver_webhook")
def deliver_webhook(self, delivery_id: int) -> None:
    """Attempt delivery of a single WebhookDelivery record."""
    import requests
    from ..models import WebhookDelivery, utcnow
    from ..services.webhooks import _backoff

    delivery = db.session.get(WebhookDelivery, delivery_id)
    if not delivery or delivery.status == "sent":
        return

    now = utcnow()
    delivery.attempts += 1
    try:
        resp = requests.post(
            delivery.url,
            data=delivery.payload,
            headers={
                "Content-Type": "application/json",
                "X-Samsoftpay-Signature": delivery.signature,
            },
            timeout=5,
        )
        delivery.last_response_code = resp.status_code
        if 200 <= resp.status_code < 300:
            delivery.status = "sent"
            delivery.last_response_body = None   # success — don't retain merchant's body
            db.session.commit()
            return
        delivery.last_response_body = (resp.text or "")[:200]
        raise ValueError(f"HTTP {resp.status_code}")
    except Exception as exc:
        delivery.last_response_body = str(exc)[:200]
        if delivery.attempts < 8:
            delay = _backoff(delivery.attempts)
            delivery.status = "pending"
            delivery.next_attempt_at = now + delay
            db.session.commit()
            raise self.retry(exc=exc, countdown=int(delay.total_seconds()))
        delivery.status = "failed"
        db.session.commit()


@celery.task(name="app.tasks.webhooks_task.sweep_pending_webhooks")
def sweep_pending_webhooks() -> None:
    """Beat task: find overdue pending/failed webhooks and re-queue them."""
    from ..models import WebhookDelivery, utcnow

    now = utcnow()
    due = (
        WebhookDelivery.query.filter(
            WebhookDelivery.status.in_(["pending", "failed"]),
            WebhookDelivery.next_attempt_at <= now,
            WebhookDelivery.attempts < 8,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(50)
        .all()
    )
    for d in due:
        deliver_webhook.delay(d.id)
