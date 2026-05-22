"""Webhook signing + delivery with retry/backoff.

Real PSPs typically:
- Sign with HMAC-SHA256, send the signature in a header like X-PesaDemo-Signature.
- Include a timestamp in the signed payload to prevent replay.
- Retry with exponential backoff up to ~48 hours.
- Expect a 2xx response within ~5 seconds.
"""
import hmac
import hashlib
from datetime import timedelta

import requests

from ..extensions import db
from ..models import WebhookDelivery, utcnow


def sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: str, signature: str, secret: str) -> bool:
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)


def deliver_pending_webhooks(*, limit: int = 50) -> int:
    """Send any pending webhooks whose next_attempt_at <= now. Returns count sent.

    Run this from the worker on a tick.
    """
    now = utcnow()
    pending = (
        WebhookDelivery.query.filter(
            WebhookDelivery.status.in_(["pending", "failed"]),
            WebhookDelivery.next_attempt_at <= now,
            WebhookDelivery.attempts < 8,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(limit)
        .all()
    )
    sent = 0
    for wh in pending:
        wh.attempts += 1
        try:
            resp = requests.post(
                wh.url,
                data=wh.payload,
                headers={
                    "Content-Type": "application/json",
                    "X-PesaDemo-Signature": wh.signature,
                },
                timeout=5,
            )
            wh.last_response_code = resp.status_code
            wh.last_response_body = resp.text[:1000]
            if 200 <= resp.status_code < 300:
                wh.status = "sent"
                sent += 1
            else:
                wh.status = "failed"
                wh.next_attempt_at = now + _backoff(wh.attempts)
        except requests.RequestException as exc:
            wh.status = "failed"
            wh.last_response_body = str(exc)[:1000]
            wh.next_attempt_at = now + _backoff(wh.attempts)
    db.session.commit()
    return sent


def _backoff(attempt: int) -> timedelta:
    # 1m, 5m, 30m, 2h, 6h, 12h, 24h, 48h
    schedule = [60, 300, 1800, 7200, 21600, 43200, 86400, 172800]
    idx = min(attempt - 1, len(schedule) - 1)
    return timedelta(seconds=schedule[idx])
