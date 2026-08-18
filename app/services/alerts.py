"""Operational alerting — route a critical condition to a human.

The money tasks already log loudly (reconciliation disagreements, stuck money,
a dead beat). But a log line on Render nobody reads is not an alert. This is the
one place that turns "logged an error" into "a human's phone buzzed":

    from ..services.alerts import send_alert
    send_alert("MTN reconciliation", "3 open critical exceptions",
               severity="critical", key="mtn-recon-critical")

Channels (each independent, all best-effort):
  - Slack   — POST to SLACK_WEBHOOK_URL (an Incoming Webhook).
  - Email   — SMTP to ALERT_EMAIL (falls back to ADMIN_EMAIL), reusing the
              same MAIL_* config the OTP mailer uses.
  - Sentry  — capture_message() so a logged-but-not-raised critical still
              surfaces in Sentry (the Celery integration only catches raises).

Two hard rules, because this code runs INSIDE the task that is already failing:

  1. NEVER raise. An alerting bug must not crash the reconciliation/sweep task
     that is reporting the real problem. Every channel is wrapped; failures are
     logged and swallowed.

  2. DEDUPE. The hourly beat tasks would otherwise re-alert the same open
     condition every single hour. A short-TTL Redis key throttles repeats per
     `key`. If Redis is unavailable we FAIL OPEN (send anyway) — a duplicate
     alert is a smaller sin than a missed one.
"""
from __future__ import annotations

import os

from flask import current_app

# Module-level Redis client, lazily created and cached. Kept here (not per-call)
# so we don't reconnect on every alert.
_redis_client = None
_redis_tried = False


def _redis():
    global _redis_client, _redis_tried
    if _redis_tried:
        return _redis_client
    _redis_tried = True
    try:
        import redis
        url = current_app.config.get("REDIS_URL") or os.environ.get("REDIS_URL")
        if not url:
            return None
        _redis_client = redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
    except Exception:
        _redis_client = None
    return _redis_client


def _should_send(key: str | None, dedupe_seconds: int) -> bool:
    """True if this alert should go out now.

    Uses Redis SET NX EX as a throttle: the first call within the window sets
    the key and returns True; repeats find it already set and return False.
    Fails OPEN — if Redis is down we would rather double-alert than go silent.
    """
    if not key or dedupe_seconds <= 0:
        return True
    client = _redis()
    if client is None:
        return True   # fail open
    try:
        # nx=True → only set if absent; returns True when it set (i.e. first time).
        was_set = client.set(f"alert:dedupe:{key}", "1", nx=True, ex=dedupe_seconds)
        return bool(was_set)
    except Exception:
        return True   # fail open


def _post_slack(title: str, body: str, severity: str) -> None:
    url = current_app.config.get("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    import requests
    emoji = {"critical": ":rotating_light:", "warning": ":warning:",
             "info": ":information_source:"}.get(severity, ":bell:")
    env = "PROD" if os.environ.get("RENDER") else "dev"
    text = f"{emoji} *[{env}] {title}* ({severity})\n{body}"
    requests.post(url, json={"text": text}, timeout=5)


def _send_email(title: str, body: str, severity: str) -> None:
    to = (current_app.config.get("ALERT_EMAIL")
          or os.environ.get("ADMIN_EMAIL")
          or current_app.config.get("MAIL_FROM", ""))
    if not to or not current_app.config.get("MAIL_HOST"):
        return
    from .email_service import send_email
    env = "PROD" if os.environ.get("RENDER") else "dev"
    subject = f"[Samsoftpay {env}] {severity.upper()}: {title}"
    html = (f"<div style='font-family:Inter,system-ui,sans-serif;max-width:560px;'>"
            f"<h2 style='color:#0f172a;margin:0 0 .5rem;'>{title}</h2>"
            f"<p style='color:#64748b;margin:0 0 1rem;'>Severity: <strong>{severity}</strong>"
            f" &middot; Environment: {env}</p>"
            f"<pre style='background:#f1f5f9;padding:1rem;border-radius:8px;"
            f"white-space:pre-wrap;color:#334155;font-size:.9rem;'>{body}</pre></div>")
    plain = f"[{env}] {severity.upper()}: {title}\n\n{body}"
    send_email(to, subject, html, plain)


def _capture_sentry(title: str, body: str, severity: str) -> None:
    try:
        import sentry_sdk
    except ImportError:
        return
    level = {"critical": "error", "warning": "warning", "info": "info"}.get(severity, "error")
    sentry_sdk.capture_message(f"{title}: {body}", level=level)


def send_alert(title: str, body: str, *, severity: str = "critical",
               key: str | None = None, dedupe_seconds: int = 3600) -> bool:
    """Fan an operational alert out to every configured channel.

    Returns True if the alert was dispatched, False if it was suppressed by the
    dedupe throttle or alerting is disabled. NEVER raises — a channel that blows
    up is logged and skipped so the caller (a money task) is never disrupted.
    """
    try:
        if current_app.config.get("ALERTS_ENABLED", "1") == "0":
            return False
    except Exception:
        # No app context (shouldn't happen from a task) — be safe, don't crash.
        return False

    if not _should_send(key, dedupe_seconds):
        return False

    for name, fn in (("slack", _post_slack), ("email", _send_email),
                     ("sentry", _capture_sentry)):
        try:
            fn(title, body, severity)
        except Exception as exc:
            try:
                current_app.logger.warning("alert channel %s failed: %s", name, exc)
            except Exception:
                pass
    try:
        current_app.logger.info("ALERT[%s] %s: %s", severity, title, body)
    except Exception:
        pass
    return True


# ── Worker/beat heartbeat ────────────────────────────────────────────────────
# A dead worker or beat can't report its own death, so the liveness signal has
# to be pulled from OUTSIDE the worker: the `heartbeat` beat task writes a fresh
# timestamp to Redis every minute, and the WEB process (which is still up) reads
# it at /ops/status. Stale timestamp => the worker/beat pipeline is down.
_HEARTBEAT_KEY = "samsoftpay:ops:heartbeat"


def record_heartbeat() -> None:
    """Called from the `heartbeat` beat task. Best-effort; never raises."""
    client = _redis()
    if client is None:
        return
    try:
        from ..models import utcnow
        # Store epoch seconds as a plain string; TTL well beyond the staleness
        # threshold so a genuinely dead pipeline lets the key expire on its own.
        client.set(_HEARTBEAT_KEY, str(int(utcnow().timestamp())), ex=3600)
    except Exception:
        pass


def heartbeat_age_seconds() -> int | None:
    """Seconds since the last recorded heartbeat, or None if unknown.

    None means "can't tell" (no Redis, or never written yet) — the caller must
    treat that distinctly from a large number (definitely stale).
    """
    client = _redis()
    if client is None:
        return None
    try:
        from ..models import utcnow
        raw = client.get(_HEARTBEAT_KEY)
        if raw is None:
            return None
        last = int(raw)
        return max(0, int(utcnow().timestamp()) - last)
    except Exception:
        return None
