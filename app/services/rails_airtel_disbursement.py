"""Real Airtel Money Disbursement (payout) adapter - SCAFFOLD.

The outbound mirror of rails_airtel_real (money OUT). Same safety contract as the
MTN disbursement adapter: durable-before-wire, AmbiguousRailError on an ambiguous
network failure, a process-local token cache under a threading.Lock, and a
circuit breaker.

SCAFFOLD - NOT YET CERTIFIED AGAINST AIRTEL. Airtel's disbursement API requires
the operator's PIN to be RSA-encrypted with Airtel's published public key, and
(in some regions / API v2) an x-signature + x-key pair derived from an AES key
that is itself RSA-encrypted. That crypto is NOT implemented here - it needs
Airtel's actual public key and sandbox to build and verify. Until then this
adapter is gated behind AIRTEL_USE_REAL (default OFF); with it off, Airtel
payouts stay REJECTED in production exactly as before (guardrail 13). Do not set
AIRTEL_USE_REAL=1 until this is finished and tested on Airtel's sandbox.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from flask import current_app

from ..extensions import db
from ..models import Channel, Payout, RailEvent
from .circuit_breaker import CircuitBreaker
from .rails_mtn_disbursement import AmbiguousRailError, InitiatePayoutResult


_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0))
_session.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))

airtel_disbursement_breaker = CircuitBreaker("airtel-disbursement", fail_threshold=5, reset_timeout=30.0)


class AirtelNotCertifiedError(RuntimeError):
    """Raised when the real Airtel disbursement path is reached without the
    crypto (encrypted PIN) it requires. A loud, explicit refusal beats sending a
    malformed transfer or - worse - guessing at the signing."""


def _airtel_msisdn(phone: str | None) -> str:
    import re
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 9 else digits


@dataclass
class _Token:
    value: str
    expires_at: datetime


_token_lock = threading.Lock()
_cached_token: Optional[_Token] = None


def _get_token(*, client_id: str, client_secret: str, base_url: str) -> str:
    global _cached_token
    with _token_lock:
        now = datetime.now(timezone.utc)
        if _cached_token and _cached_token.expires_at > now + timedelta(minutes=5):
            return _cached_token.value
        resp = _session.post(
            f"{base_url}/auth/oauth2/token",
            json={"client_id": client_id, "client_secret": client_secret,
                  "grant_type": "client_credentials"},
            headers={"Content-Type": "application/json", "Accept": "*/*"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        expires_in = int(data.get("expires_in", 3600))
        _cached_token = _Token(value=data["access_token"],
                               expires_at=now + timedelta(seconds=expires_in))
        return _cached_token.value


class RealAirtelDisbursementAdapter:
    """Outbound Airtel Money payments. SCAFFOLD - see module docstring."""

    channel = Channel.AIRTEL_MONEY

    def __init__(self):
        cfg = current_app.config
        self.client_id = cfg.get("AIRTEL_CLIENT_ID", "")
        self.client_secret = cfg.get("AIRTEL_CLIENT_SECRET", "")
        self.base_url = cfg.get("AIRTEL_BASE_URL", "")
        self.country = cfg.get("AIRTEL_COUNTRY", "UG")
        self.currency = cfg.get("AIRTEL_CURRENCY", "UGX")
        # The PIN must be RSA-encrypted with Airtel's public key. We expect the
        # operator to provide the already-encrypted value (AIRTEL_DISBURSEMENT_PIN)
        # rather than hold the plaintext PIN + Airtel's key in the app. If it is
        # absent, the real path refuses loudly instead of sending garbage.
        self.encrypted_pin = cfg.get("AIRTEL_DISBURSEMENT_PIN", "")

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "X-Country": self.country,
            "X-Currency": self.currency,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    def _amount(self, minor: int):
        if self.currency in ("EUR", "USD"):
            return round(minor / 100, 2)
        return int(minor)

    def resolve_account_holder(self, phone: str) -> dict:
        """Airtel KYC lookup (GET /standard/v1/users/{msisdn}). Returns
        {"active": bool|None, "registered_name": str|None}. Never claims a wallet
        is dead on a network error (unknown != inactive), mirroring MTN."""
        msisdn = _airtel_msisdn(phone)
        try:
            token = _get_token(client_id=self.client_id, client_secret=self.client_secret,
                               base_url=self.base_url)
            r = _session.get(f"{self.base_url}/standard/v1/users/{msisdn}",
                             headers=self._headers(token), timeout=15)
            if r.status_code == 200:
                body = r.json() if r.content else {}
                data = body.get("data", {}) or {}
                name = (data.get("first_name") or "") + " " + (data.get("last_name") or "")
                return {"active": bool(data.get("is_barred") is False) if "is_barred" in data else None,
                        "registered_name": name.strip() or None}
            if r.status_code == 404:
                return {"active": False, "registered_name": None}
        except requests.RequestException:
            pass
        return {"active": None, "registered_name": None}

    def initiate(self, payout: Payout) -> InitiatePayoutResult:
        if not self.encrypted_pin:
            # Loud refusal BEFORE any wire call and (in create_payout's ordering)
            # before any money moves - the caller treats this as unavailable.
            raise AirtelNotCertifiedError(
                "Airtel disbursement not configured: AIRTEL_DISBURSEMENT_PIN "
                "(RSA-encrypted) is required and the signing is unverified.")

        reference_id = str(uuid.uuid4())
        token = _get_token(client_id=self.client_id, client_secret=self.client_secret,
                           base_url=self.base_url)
        msisdn = _airtel_msisdn(payout.recipient_phone)
        body = {
            "payee": {"msisdn": msisdn, "wallet_type": "NORMAL"},
            "reference": payout.reference or payout.public_id,
            "pin": self.encrypted_pin,
            "transaction": {"amount": self._amount(payout.amount), "id": payout.public_id},
        }

        # DURABLE BEFORE THE WIRE (guardrail 21): the payout row + earmark +
        # reference are committed by create_payout's ordering before this call;
        # stamp the reference so an ambiguous failure is reconciliable.
        payout.rail_reference = reference_id
        db.session.commit()

        try:
            resp = _session.post(
                f"{self.base_url}/standard/v1/disbursements/",
                headers=self._headers(token), json=body, timeout=20,
            )
        except requests.RequestException as exc:
            if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                airtel_disbursement_breaker.record_failure()
            raise AmbiguousRailError(reference_id, str(exc)) from exc
        airtel_disbursement_breaker.record_success()

        db.session.add(RailEvent(
            rail=Channel.AIRTEL_MONEY, rail_reference=reference_id,
            event_type="payout_initiated", amount=payout.amount, currency=payout.currency,
            raw_payload=json.dumps({"status_code": resp.status_code, "request":
                                    {**body, "pin": "***"}, "response": resp.text[:1000]}),
        ))

        accepted = resp.status_code in (200, 202)
        if accepted:
            try:
                accepted = bool((resp.json().get("status", {}) or {}).get("success", True))
            except ValueError:
                pass
        if not accepted:
            return InitiatePayoutResult(rail_reference=reference_id, accepted=False,
                                        reason=f"airtel_rejected_{resp.status_code}: {resp.text[:200]}")

        try:
            from ..tasks.polling import poll_airtel_disbursement
            poll_airtel_disbursement.apply_async(args=[payout.id, reference_id], countdown=5)
        except Exception as exc:
            current_app.logger.warning(
                "could not queue Airtel payout poller for %s (%s); "
                "relying on inbound callback + sweep", payout.id, exc)
        return InitiatePayoutResult(rail_reference=reference_id, accepted=True)
