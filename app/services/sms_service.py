"""SMS delivery — Africa's Talking (the standard SMS gateway for Uganda).

Best-effort and NEVER raises: an SMS failure must never affect a payment we have
already taken. Without an API key it logs and no-ops (so receipts degrade
gracefully until SMS is wired, exactly like email's OTP fallback).

Configure via env (on the WEB service, and the WORKER if it sends):
    AT_API_KEY        Africa's Talking API key (required to actually send)
    AT_USERNAME       app username ('sandbox' for testing; default 'sandbox')
    SMS_SENDER_ID     optional alphanumeric sender id / short code
"""
from __future__ import annotations

import os


def sms_configured() -> bool:
    return bool(os.environ.get("AT_API_KEY"))


def _normalize(phone: str) -> str:
    """E.164 for Africa's Talking (+2567...)."""
    from .msisdn import normalize_msisdn
    digits = normalize_msisdn(phone)          # -> 2567..., strips +/spaces
    if not digits:
        return ""
    return digits if digits.startswith("+") else "+" + digits


def send_sms(to_phone: str | None, message: str) -> bool:
    """Send one SMS. Returns True if the gateway accepted it, else False.
    NEVER raises."""
    if not to_phone or not message:
        return False
    api_key = os.environ.get("AT_API_KEY")
    to = _normalize(to_phone)
    if not api_key:
        _log(f"[SMS fallback] to {to}: {message[:140]}")
        return False
    username = os.environ.get("AT_USERNAME", "sandbox")
    sender = os.environ.get("SMS_SENDER_ID")
    base = ("https://api.sandbox.africastalking.com" if username == "sandbox"
            else "https://api.africastalking.com")
    data = {"username": username, "to": to, "message": message}
    if sender:
        data["from"] = sender
    try:
        import requests
        r = requests.post(
            f"{base}/version1/messaging", data=data,
            headers={"apiKey": api_key, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=10)
        return r.status_code in (200, 201)
    except Exception as exc:      # network/other — best-effort
        _log(f"SMS send failed to {to}: {exc}")
        return False


def _log(msg: str) -> None:
    try:
        from flask import current_app
        current_app.logger.info(msg)
    except Exception:
        print(msg, flush=True)
