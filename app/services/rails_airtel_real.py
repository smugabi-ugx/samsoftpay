"""Real Airtel Money Collections adapter - SCAFFOLD.

Implements the same RailAdapter interface as the mock (initiate -> InitiateResult)
and carries the SAME safety properties the MTN adapter earned the hard way:
  - a process-local OAuth token cache guarded by a threading.Lock (guardrail 1:
    `import threading` must never be removed);
  - the rail reference is committed onto the txn BEFORE the outbound call
    (durable-before-wire), so an ambiguous network failure is reconciliation
    work, not silent money loss (guardrail 21);
  - a requests exception during the POST raises AmbiguousRailError (the request
    may have reached Airtel) so create_charge parks the charge AUTHORIZED;
  - a circuit breaker fails fast when Airtel stops answering, so a slow rail
    can't starve the gunicorn thread pool.

SCAFFOLD - NOT YET CERTIFIED AGAINST AIRTEL. This follows Airtel Africa's
published OpenAPI shape (auth/oauth2/token, /merchant/v1/payments/,
/standard/v1/payments/{id}). The exact field names, the country/currency
headers, the amount format, and (for some regions) request signing/encryption
must be confirmed against Airtel's sandbox before going live. That is why the
rail is gated behind AIRTEL_USE_REAL (default OFF) AND the presence of
credentials: until an operator deliberately sets AIRTEL_USE_REAL=1 with real
keys, get_adapter() keeps returning the mock, so Airtel stays refused in
production exactly as before (guardrail 14). DO NOT set AIRTEL_USE_REAL=1 in
production until this has been tested end-to-end on Airtel's sandbox.

Airtel MSISDN note: Airtel Africa expects the NATIONAL number (no country code,
e.g. "751234567" for Uganda) together with an X-Country header - unlike MTN,
which wants the full country-coded MSISDN. See _airtel_msisdn below.
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
from ..models import Channel, RailEvent, Transaction
from .circuit_breaker import CircuitBreaker, RailUnavailableError
from .rails import InitiateResult, RailAdapter
from .rails_mtn_disbursement import AmbiguousRailError


# One shared keep-alive pool for all Airtel calls (token + payment + status).
_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0))
_session.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))

# Fail fast when the Airtel collections rail stops answering (mirrors MTN).
airtel_collection_breaker = CircuitBreaker("airtel-collection", fail_threshold=5, reset_timeout=30.0)


def _airtel_msisdn(phone: str | None) -> str:
    """Airtel Africa wants the NATIONAL subscriber number (no country code),
    paired with an X-Country header. Reduce any input form (0751, +256751,
    256751) to its last 9 digits."""
    import re
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 9 else digits


# ---------- Token cache (process-local, Airtel-only) ----------

@dataclass
class _Token:
    value: str
    expires_at: datetime


_token_lock = threading.Lock()
_cached_token: Optional[_Token] = None


def _get_token(*, client_id: str, client_secret: str, base_url: str) -> str:
    """Fetch + cache an Airtel OAuth2 client-credentials token."""
    global _cached_token
    with _token_lock:
        now = datetime.now(timezone.utc)
        if _cached_token and _cached_token.expires_at > now + timedelta(minutes=5):
            return _cached_token.value
        resp = _session.post(
            f"{base_url}/auth/oauth2/token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/json", "Accept": "*/*"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        expires_in = int(data.get("expires_in", 3600))
        _cached_token = _Token(
            value=data["access_token"],
            expires_at=now + timedelta(seconds=expires_in),
        )
        return _cached_token.value


# ---------- Adapter ----------

class RealAirtelMoneyAdapter(RailAdapter):
    channel = Channel.AIRTEL_MONEY

    def __init__(self):
        cfg = current_app.config
        self.client_id = cfg.get("AIRTEL_CLIENT_ID", "")
        self.client_secret = cfg.get("AIRTEL_CLIENT_SECRET", "")
        self.base_url = cfg.get("AIRTEL_BASE_URL", "")
        self.country = cfg.get("AIRTEL_COUNTRY", "UG")
        self.currency = cfg.get("AIRTEL_CURRENCY", "UGX")
        self.callback_url = cfg.get("AIRTEL_CALLBACK_URL", "") or ""

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "X-Country": self.country,
            "X-Currency": self.currency,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    def _amount(self, minor: int):
        """Airtel expects a numeric amount in MAJOR units. UGX has no minor unit,
        so amount == stored value; EUR/USD sandbox currencies divide by 100."""
        if self.currency in ("EUR", "USD"):
            return round(minor / 100, 2)
        return int(minor)

    def initiate(self, txn: Transaction) -> InitiateResult:
        # Fail fast if the rail has clearly stopped answering - nothing is sent
        # here, so create_charge treats this as a clean, retryable rejection.
        if not airtel_collection_breaker.allow():
            raise RailUnavailableError(
                "Airtel collections rail temporarily unavailable (circuit open) - retry shortly")

        reference_id = str(uuid.uuid4())
        try:
            token = _get_token(
                client_id=self.client_id,
                client_secret=self.client_secret,
                base_url=self.base_url,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            airtel_collection_breaker.record_failure()
            raise RailUnavailableError(f"Airtel token endpoint unreachable: {exc}") from exc

        msisdn = _airtel_msisdn(txn.customer_phone)
        body = {
            "reference": txn.merchant_reference or txn.public_id,
            "subscriber": {
                "country": self.country,
                "currency": self.currency,
                "msisdn": msisdn,
            },
            "transaction": {
                "amount": self._amount(txn.amount),
                "country": self.country,
                "currency": self.currency,
                "id": txn.public_id,
            },
        }

        # DURABLE BEFORE THE WIRE (guardrail 21): commit our client-generated
        # reference onto the txn BEFORE the POST, so a timeout after Airtel
        # accepts the request is reconciliation work, not a lost record.
        txn.rail_reference = reference_id
        db.session.commit()

        try:
            resp = _session.post(
                f"{self.base_url}/merchant/v1/payments/",
                headers=self._headers(token),
                json=body,
                timeout=20,
            )
        except requests.RequestException as exc:
            # The request MAY have reached Airtel - park AUTHORIZED and let
            # reconciliation resolve it from Airtel's own status.
            if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                airtel_collection_breaker.record_failure()
            raise AmbiguousRailError(reference_id, str(exc)) from exc
        airtel_collection_breaker.record_success()

        db.session.add(RailEvent(
            rail=Channel.AIRTEL_MONEY,
            rail_reference=reference_id,
            event_type="initiated",
            amount=txn.amount,
            currency=txn.currency,
            raw_payload=json.dumps({
                "status_code": resp.status_code,
                "request": body,
                "response": resp.text[:1000],
            }),
        ))

        # Airtel returns 200 with a status envelope; a non-2xx or an explicit
        # unsuccessful status is a synchronous rejection.
        accepted = resp.status_code in (200, 202)
        if accepted:
            try:
                ok = (resp.json().get("status", {}) or {}).get("success", True)
                accepted = bool(ok)
            except ValueError:
                pass
        if not accepted:
            return InitiateResult(
                rail_reference=reference_id, accepted=False,
                reason=f"airtel_rejected_{resp.status_code}: {resp.text[:200]}",
            )

        # Accepted - queue a persistent poller for the final status. As with MTN,
        # a momentarily-unreachable broker must NOT fail an accepted charge; the
        # inbound callback + hourly sweep are the guaranteed completion paths.
        try:
            from ..tasks.polling import poll_airtel_collection
            poll_airtel_collection.apply_async(args=[txn.id, reference_id], countdown=5)
        except Exception as exc:  # broker down, or poller not wired yet (scaffold)
            current_app.logger.warning(
                "could not queue Airtel poller for txn %s (%s); "
                "relying on inbound callback + sweep", txn.id, exc)
        return InitiateResult(rail_reference=reference_id, accepted=True)
