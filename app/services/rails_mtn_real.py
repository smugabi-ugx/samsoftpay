"""Real MTN MoMo Collections sandbox adapter.

Implements the same RailAdapter interface as the mock, but actually talks to
https://sandbox.momodeveloper.mtn.com.

Flow:
1. Get an OAuth token (cached until ~5 min before expiry).
2. POST /collection/v1_0/requesttopay with a unique X-Reference-Id.
   MTN returns 202 immediately. Transaction is now `authorized` (pending at MTN).
3. We do NOT block. A background poller checks status every few seconds for up
   to 90s. When MTN reports SUCCESSFUL or FAILED, we call complete_transaction.
   (Callbacks via providerCallbackHost are also supported via /inbound/mtn_momo
   but are flaky in sandbox, so polling is the safety net.)

Sandbox test MSISDNs (per MTN docs):
- Use phone numbers like 46733123450 onward — they trigger deterministic outcomes.
- Some MSISDN suffixes simulate failures; the exact mapping is in MTN's docs.
- In sandbox, "currency" must be "EUR" — UGX is production-only.
"""
from __future__ import annotations

import base64
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from flask import current_app

from ..extensions import db
from ..models import Channel, RailEvent, Transaction
from .rails import InitiateResult, RailAdapter
from .rails_mtn_disbursement import AmbiguousRailError


# ---------- Token cache (process-local) ----------

@dataclass
class _Token:
    value: str
    expires_at: datetime


_token_lock = threading.Lock()
_cached_token: Optional[_Token] = None


def _get_token(*, subscription_key: str, api_user: str, api_key: str, base_url: str) -> str:
    """Fetch + cache an OAuth token for the Collections product."""
    global _cached_token
    with _token_lock:
        now = datetime.now(timezone.utc)
        if _cached_token and _cached_token.expires_at > now + timedelta(minutes=5):
            return _cached_token.value

        basic = base64.b64encode(f"{api_user}:{api_key}".encode()).decode()
        resp = requests.post(
            f"{base_url}/collection/token/",
            headers={
                "Authorization": f"Basic {basic}",
                "Ocp-Apim-Subscription-Key": subscription_key,
            },
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

class RealMTNMoMoAdapter(RailAdapter):
    channel = Channel.MTN_MOMO

    def __init__(self):
        cfg = current_app.config
        self.subscription_key = cfg["MOMO_SUBSCRIPTION_KEY"]
        self.api_user = cfg["MOMO_API_USER"]
        self.api_key = cfg["MOMO_API_KEY"]
        self.base_url = cfg["MOMO_BASE_URL"]
        self.target_env = cfg["MOMO_TARGET_ENV"]
        self.currency = cfg["MOMO_CURRENCY"]
        # OPT-IN callback: set MOMO_CALLBACK_URL (e.g.
        # https://api.samsoftpay.com/inbound/mtn/callback) ONLY once MTN has
        # provisioned the API user with a matching providerCallbackHost — MTN
        # rejects requesttopay when the X-Callback-Url host doesn't match the
        # provisioned one, so defaulting it on would break charges. Unset =
        # today's behavior (poller + hourly sweep own completion). The callback
        # route itself never trusts the payload — it verifies via MTN's status
        # API (see webhooks_inbound.mtn_native_callback).
        self.callback_url = cfg.get("MOMO_CALLBACK_URL") or ""
        # Sandbox uses EUR; production uses local currency (UGX in Uganda).
        # Amounts to MTN must be in MAJOR units as a string ("100.00"), unlike
        # our internal storage (minor units as int).

    def _headers(self, token: str, reference_id: str) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "X-Reference-Id": reference_id,
            "X-Target-Environment": self.target_env,
            "Content-Type": "application/json",
        }
        if self.callback_url:
            headers["X-Callback-Url"] = self.callback_url
        return headers

    def _amount_to_string(self, minor: int) -> str:
        """Convert internal minor units to MoMo's string-decimal major units.

        In sandbox we use EUR with 2 decimals. UGX (production) has 0 decimals.
        """
        if self.currency in ("EUR", "USD"):
            return f"{minor / 100:.2f}"
        # UGX, RWF, etc. — no minor unit
        return str(minor)

    def initiate(self, txn: Transaction) -> InitiateResult:
        reference_id = str(uuid.uuid4())
        token = _get_token(
            subscription_key=self.subscription_key,
            api_user=self.api_user,
            api_key=self.api_key,
            base_url=self.base_url,
        )

        # MSISDN must carry the country code in production (a raw "0772..."
        # fails with PAYER_NOT_FOUND) and the notes reject special characters
        # (an apostrophe 400s the whole charge) — see services/msisdn.py.
        from .msisdn import normalize_msisdn, sanitize_note
        msisdn = normalize_msisdn(txn.customer_phone)

        body = {
            "amount": self._amount_to_string(txn.amount),
            "currency": self.currency,
            "externalId": txn.public_id,
            "payer": {
                "partyIdType": "MSISDN",
                "partyId": msisdn,
            },
            "payerMessage": sanitize_note(f"Charge {txn.public_id}"),
            "payeeNote": sanitize_note(txn.merchant_reference or txn.public_id),
        }

        # DURABLE BEFORE THE WIRE: commit the reference onto the transaction
        # BEFORE the outbound call. A timeout after MTN accepts requesttopay
        # used to roll back the flushed txn — the customer still got the
        # prompt, approved, MTN collected, and the callback found no
        # transaction: money in our float invisible to the ledger. With the
        # reference committed first, an ambiguous failure is ordinary
        # reconciliation work for the sweep instead of silent money loss.
        txn.rail_reference = reference_id
        db.session.commit()

        try:
            resp = requests.post(
                f"{self.base_url}/collection/v1_0/requesttopay",
                headers=self._headers(token, reference_id),
                json=body,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise AmbiguousRailError(reference_id, str(exc)) from exc

        db.session.add(
            RailEvent(
                rail=Channel.MTN_MOMO,
                rail_reference=reference_id,
                event_type="initiated",
                amount=txn.amount,
                currency=txn.currency,
                raw_payload=json.dumps({
                    "status_code": resp.status_code,
                    "request": body,
                    "response": resp.text[:1000],
                }),
            )
        )

        if resp.status_code != 202:
            # 4xx / 5xx — MTN rejected the request synchronously.
            return InitiateResult(
                rail_reference=reference_id,
                accepted=False,
                reason=f"momo_rejected_{resp.status_code}: {resp.text[:200]}",
            )

        # 202 Accepted — MTN took the request. Queue a persistent Celery poller.
        # The deposit is already accepted by MTN; the inbound webhook is the primary
        # completion path and the beat sweep re-polls stragglers. So if the broker is
        # momentarily unreachable, DO NOT fail the charge — just log and rely on those.
        try:
            from ..tasks.polling import poll_mtn_collection
            poll_mtn_collection.apply_async(args=[txn.id, reference_id], countdown=5)
        except Exception as exc:  # broker down / Redis blip
            current_app.logger.warning(
                "could not queue MoMo poller for txn %s (%s); "
                "relying on inbound webhook + sweep", txn.id, exc
            )
        return InitiateResult(rail_reference=reference_id, accepted=True)
