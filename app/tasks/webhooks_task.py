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


@celery.task(bind=True, name="app.tasks.webhooks_task.deliver_webhook")
def deliver_webhook(self, delivery_id: int) -> None:
    """Attempt delivery of a single WebhookDelivery record — exactly one driver.

    Two schedulers used to race for the same row: the task scheduled its own
    Celery retry AND left next_attempt_at set, so the 30s sweep re-enqueued
    the same delivery when the backoff elapsed — merchants received
    duplicates. Now: an atomic CLAIM on next_attempt_at decides the single
    winner (the loser's UPDATE matches 0 rows and it walks away), and ALL
    retries flow through the sweep — no task self-retry.
    """
    import requests
    from datetime import timedelta
    from ..models import WebhookDelivery, utcnow
    from ..services.webhooks import _backoff

    now = utcnow()
    claimed = (
        db.session.query(WebhookDelivery)
        .filter(WebhookDelivery.id == delivery_id,
                WebhookDelivery.status.in_(["pending", "failed"]),
                WebhookDelivery.next_attempt_at <= now + timedelta(seconds=5))
        .update({"next_attempt_at": now + timedelta(minutes=6)},
                synchronize_session=False)
    )
    db.session.commit()
    if not claimed:
        return   # another driver owns this attempt (or it's already sent)

    delivery = db.session.get(WebhookDelivery, delivery_id)
    delivery.attempts += 1
    # SSRF re-check at delivery (DNS rebinding) — see url_guard.
    from ..services.url_guard import is_public_http_url
    if not is_public_http_url(delivery.url):
        delivery.status = "failed"
        delivery.last_response_code = 0
        db.session.commit()
        return
    try:
        resp = requests.post(
            delivery.url,
            data=delivery.payload,
            headers={
                "Content-Type": "application/json",
                "X-Samsoftpay-Signature": delivery.signature,
            },
            timeout=5,
            allow_redirects=False,
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
            delivery.status = "pending"
            delivery.next_attempt_at = now + _backoff(delivery.attempts)
            db.session.commit()   # the sweep picks it up when the backoff elapses
        else:
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
