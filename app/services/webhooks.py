"""Webhook signing + delivery with retry/backoff.

Real PSPs typically:
- Sign with HMAC-SHA256, send the signature in a header like X-Samsoftpay-Signature.
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


def merchant_signing_secret(merchant) -> str:
    """The merchant's own whsec_ secret, generated lazily if missing.

    Outbound deliveries are signed PER MERCHANT so each merchant can verify
    them (shown in Account settings). The global WEBHOOK_SIGNING_SECRET is
    inbound-only — it authenticates rail callbacks that mark money succeeded,
    so it must never be handed to merchants.
    """
    if not getattr(merchant, "webhook_secret", None):
        import secrets as _secrets
        merchant.webhook_secret = "whsec_" + _secrets.token_urlsafe(32)
        db.session.commit()
    return merchant.webhook_secret


def verify_signature(payload: str, signature: str, secret: str) -> bool:
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)


def enqueue(merchant, event: str, data: dict, *, transaction_id: int | None = None) -> bool:
    """Queue one signed webhook for a merchant. Returns True if it was queued.

    The single place an outbound event is created, so every event — charge or
    vending — is signed the same way and picked up by the same delivery sweep.
    A merchant with no webhook_url configured is simply skipped.

    Canonical JSON (no spaces) because the signature is computed over the exact
    bytes we send; re-serialising differently on the receiving side would break
    verification.
    """
    import json

    from flask import current_app

    if not merchant or not getattr(merchant, "webhook_url", None):
        return False

    payload = json.dumps({"event": event, "data": data}, separators=(",", ":"))
    secret = merchant_signing_secret(merchant)
    delivery = WebhookDelivery(
        merchant_id=merchant.id,
        transaction_id=transaction_id,
        url=merchant.webhook_url,
        payload=payload,
        signature=sign_payload(payload, secret),
        next_attempt_at=utcnow(),
    )
    db.session.add(delivery)
    db.session.commit()
    # Fire the first attempt NOW instead of waiting up to 30s for the beat
    # sweep. Best-effort: a broker outage must never fail the money operation
    # that produced this event — the sweep remains the guaranteed path.
    try:
        from ..tasks.webhooks_task import deliver_webhook
        deliver_webhook.delay(delivery.id)
    except Exception:
        pass
    return True


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
    from .url_guard import is_public_http_url
    sent = 0
    for wh in pending:
        wh.attempts += 1
        # Re-validate at delivery time too: a hostname saved as public can
        # re-resolve to a private IP (DNS rebinding). Fail the delivery rather
        # than let the worker hit an internal service.
        if not is_public_http_url(wh.url):
            wh.status = "failed"
            wh.last_response_code = 0
            continue
        try:
            resp = requests.post(
                wh.url,
                data=wh.payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Samsoftpay-Signature": wh.signature,
                },
                timeout=5,
                allow_redirects=False,   # a 302 to an internal URL must not be followed
            )
            wh.last_response_code = resp.status_code
            if 200 <= resp.status_code < 300:
                wh.status = "sent"
                wh.last_response_body = None   # success — don't retain merchant's body
                sent += 1
            else:
                wh.status = "failed"
                # Keep only a short snippet for debugging a failing endpoint.
                wh.last_response_body = (resp.text or "")[:200]
                wh.next_attempt_at = now + _backoff(wh.attempts)
        except requests.RequestException as exc:
            wh.status = "failed"
            wh.last_response_body = str(exc)[:200]
            wh.next_attempt_at = now + _backoff(wh.attempts)
    db.session.commit()
    return sent


def _backoff(attempt: int) -> timedelta:
    # 1m, 5m, 30m, 2h, 6h, 12h, 24h, 48h
    schedule = [60, 300, 1800, 7200, 21600, 43200, 86400, 172800]
    idx = min(attempt - 1, len(schedule) - 1)
    return timedelta(seconds=schedule[idx])
