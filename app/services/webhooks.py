"""Webhook signing + delivery with retry/backoff.

Real PSPs typically:
- Sign with HMAC-SHA256, send the signature in a header like X-Samsoftpay-Signature.
- Include a timestamp in the signed payload to prevent replay.
- Retry with exponential backoff up to ~48 hours.
- Expect a 2xx response within ~5 seconds.
"""
import hmac
import hashlib
import json
import uuid
from datetime import timedelta

import requests

from ..extensions import db
from ..models import WebhookDelivery, utcnow


def charge_event_data(txn) -> dict:
    """The canonical `data` body for a charge webhook.

    SINGLE source of truth: the orchestrator's completion hook and the
    gift-card checkout used to hand-build this identical dict in two places,
    which had already drifted. Anything that emits a charge.* event builds its
    data here so the shape can never diverge again.
    """
    return {
        "id": txn.public_id,
        "amount": txn.amount,
        "fee": txn.fee_amount,
        "currency": txn.currency,
        "channel": txn.channel.value,
        "status": txn.status.value,
        # `reference` is the name GET /v1/charges/<id> uses; `merchant_reference`
        # is the original webhook name. BOTH are emitted, with the same value —
        # integrators already parse `merchant_reference`, so it is never removed.
        "reference": txn.merchant_reference,
        "merchant_reference": txn.merchant_reference,
        "mode": "test" if txn.is_test else "live",
        "failure_reason": txn.failure_reason,
        "completed_at": txn.completed_at.isoformat() if txn.completed_at else None,
    }


def sign_payload(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def merchant_signing_secret(merchant, is_test: bool = False) -> str:
    """The merchant's own whsec_ secret for the given mode, generated lazily.

    Outbound deliveries are signed PER MERCHANT so each merchant can verify
    them (shown in Account settings). The global WEBHOOK_SIGNING_SECRET is
    inbound-only — it authenticates rail callbacks that mark money succeeded,
    so it must never be handed to merchants.

    Mode: a test event routed to a SEPARATE sandbox endpoint (webhook_url_test
    set) is signed with `webhook_secret_test`; live events — and test events
    that fall back to the single live URL — use `webhook_secret`.
    """
    import secrets as _secrets
    if is_test and getattr(merchant, "webhook_url_test", None):
        if not getattr(merchant, "webhook_secret_test", None):
            merchant.webhook_secret_test = "whsec_" + _secrets.token_urlsafe(32)
            db.session.commit()
        return merchant.webhook_secret_test
    if not getattr(merchant, "webhook_secret", None):
        merchant.webhook_secret = "whsec_" + _secrets.token_urlsafe(32)
        db.session.commit()
    return merchant.webhook_secret


def target_for(merchant, is_test: bool):
    """Resolve (url, secret) for a delivery of the given mode.

    Live (or a test event when no sandbox URL is configured) -> the single
    webhook_url + webhook_secret. A test event WITH webhook_url_test set ->
    that URL + its own secret, so sandbox traffic never hits the live endpoint.
    Returns (None, None) when no URL is configured for that mode.
    """
    if is_test and getattr(merchant, "webhook_url_test", None):
        return merchant.webhook_url_test, merchant_signing_secret(merchant, is_test=True)
    url = getattr(merchant, "webhook_url", None)
    if not url:
        return None, None
    return url, merchant_signing_secret(merchant, is_test=False)


def _delivery_is_test(wh) -> bool:
    """Recover a delivery's mode from its signed payload's `data.mode` field —
    the mode is stamped into the envelope at enqueue, so a resend can retarget
    to the correct per-mode endpoint without a separate column."""
    try:
        return json.loads(wh.payload).get("data", {}).get("mode") == "test"
    except Exception:
        return False


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
    if not merchant:
        return False
    # Route by the event's OWN mode (stamped into data.mode by charge_event_data
    # et al.), so a sandbox event goes to the sandbox endpoint when one is set —
    # and never to the live endpoint (Backbone Q5).
    is_test = (data.get("mode") == "test")
    url, secret = target_for(merchant, is_test)
    if not url:
        return False

    # Envelope carries an event id + unix timestamp so a receiver can DEDUPE
    # (retries/duplicate deliveries share the id) and enforce a replay window
    # (the docs promise replay protection; there was nothing to check before).
    envelope = {
        "id": "evt_" + uuid.uuid4().hex[:24],
        "timestamp": int(utcnow().timestamp()),
        "event": event,
        "data": data,
    }
    payload = json.dumps(envelope, separators=(",", ":"))
    delivery = WebhookDelivery(
        merchant_id=merchant.id,
        transaction_id=transaction_id,
        url=url,
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


def resend_delivery(wh) -> None:
    """Re-queue one delivery for the sweep (self-service recovery).

    The payload and signature are reused byte-for-byte — the receiver dedupes
    on the envelope id, so a resend of an already-processed event is harmless.
    An exhausted delivery (8 attempts) gets a fresh cycle; the sweep's
    `attempts < 8` cap would otherwise ignore it forever.
    """
    wh.status = "pending"
    wh.next_attempt_at = utcnow()
    if wh.attempts >= 8:
        wh.attempts = 0
    # The #1 recovery case is "my endpoint URL was wrong" — deliver to the
    # merchant's CURRENT url for THIS delivery's mode, not the one stored at
    # enqueue time. Re-sign the (unchanged) payload bytes with that mode's
    # secret so a delivery that now targets the sandbox endpoint is signed with
    # the sandbox secret. Safe: the envelope id in the payload is unchanged, so
    # the receiver still dedupes; only the signing key follows the target.
    from ..models import Merchant
    m = db.session.get(Merchant, wh.merchant_id)
    if m is not None:
        url, secret = target_for(m, _delivery_is_test(wh))
        if url:
            wh.url = url
            wh.signature = sign_payload(wh.payload, secret)
    db.session.commit()
    # Best-effort immediate attempt, same pattern as enqueue().
    try:
        from ..tasks.webhooks_task import deliver_webhook
        deliver_webhook.delay(wh.id)
    except Exception:
        pass


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
            # safe_post PINS the validated public IP for the connection, closing
            # the DNS-rebinding window between the is_public_http_url check above
            # and the actual TCP connect (guard and request used to re-resolve
            # independently). allow_redirects=False is applied inside safe_post.
            from .url_guard import SsrfBlocked, safe_post
            resp = safe_post(
                wh.url,
                data=wh.payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Samsoftpay-Signature": wh.signature,
                },
                timeout=5,
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
        except SsrfBlocked:
            # Host re-resolved to a non-public address at delivery time — treat as
            # a hard failure, never let the worker touch an internal service.
            wh.status = "failed"
            wh.last_response_code = 0
            wh.last_response_body = "blocked: url resolved to a non-public address"
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
