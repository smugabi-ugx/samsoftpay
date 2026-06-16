"""Real MTN MoMo Disbursement sandbox adapter.

Mirrors the Collections adapter but for outbound payments.

Endpoint: POST /disbursement/v1_0/transfer
Token:    POST /disbursement/token/   (different path from collection)

Flow:
1. Get OAuth token (cached per-product — disbursement has its own).
2. POST /disbursement/v1_0/transfer with X-Reference-Id, recipient MSISDN,
   amount. MTN returns 202.
3. Poll /disbursement/v1_0/transfer/{ref} for SUCCESSFUL / FAILED.
4. Call complete_payout() to post the final ledger entries.
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
from ..models import Channel, Payout, RailEvent


@dataclass
class InitiatePayoutResult:
    rail_reference: str
    accepted: bool
    reason: Optional[str] = None


# ---------- Token cache (separate from Collections!) ----------

@dataclass
class _Token:
    value: str
    expires_at: datetime


_token_lock = threading.Lock()
_cached_token: Optional[_Token] = None


def _get_token(*, subscription_key: str, api_user: str, api_key: str, base_url: str) -> str:
    global _cached_token
    with _token_lock:
        now = datetime.now(timezone.utc)
        if _cached_token and _cached_token.expires_at > now + timedelta(minutes=5):
            return _cached_token.value

        basic = base64.b64encode(f"{api_user}:{api_key}".encode()).decode()
        resp = requests.post(
            f"{base_url}/disbursement/token/",
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

class RealMTNMoMoDisbursementAdapter:
    """Outbound MTN MoMo payments (the inverse of the Collections adapter)."""

    channel = Channel.MTN_MOMO

    def __init__(self):
        cfg = current_app.config
        self.subscription_key = cfg["MOMO_DISBURSEMENT_SUBSCRIPTION_KEY"]
        self.api_user = cfg["MOMO_DISBURSEMENT_API_USER"]
        self.api_key = cfg["MOMO_DISBURSEMENT_API_KEY"]
        self.base_url = cfg["MOMO_BASE_URL"]
        self.target_env = cfg["MOMO_TARGET_ENV"]
        self.currency = cfg["MOMO_CURRENCY"]
        if not all([self.subscription_key, self.api_user, self.api_key]):
            raise RuntimeError(
                "Disbursement credentials missing. Set MOMO_DISBURSEMENT_* env vars."
            )

    def _headers(self, token: str, reference_id: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "X-Reference-Id": reference_id,
            "X-Target-Environment": self.target_env,
            "Content-Type": "application/json",
        }

    def _amount_to_string(self, minor: int) -> str:
        if self.currency in ("EUR", "USD"):
            return f"{minor / 100:.2f}"
        return str(minor)

    def initiate(self, payout: Payout) -> InitiatePayoutResult:
        reference_id = str(uuid.uuid4())
        token = _get_token(
            subscription_key=self.subscription_key,
            api_user=self.api_user,
            api_key=self.api_key,
            base_url=self.base_url,
        )

        msisdn = (payout.recipient_phone or "").lstrip("+").replace(" ", "")

        body = {
            "amount": self._amount_to_string(payout.amount),
            "currency": self.currency,
            "externalId": payout.public_id,
            "payee": {
                "partyIdType": "MSISDN",
                "partyId": msisdn,
            },
            "payerMessage": f"Payout {payout.public_id}",
            "payeeNote": payout.recipient_name or payout.public_id,
        }

        resp = requests.post(
            f"{self.base_url}/disbursement/v1_0/transfer",
            headers=self._headers(token, reference_id),
            json=body,
            timeout=20,
        )

        db.session.add(
            RailEvent(
                rail=Channel.MTN_MOMO,
                rail_reference=reference_id,
                event_type="payout_initiated",
                amount=payout.amount,
                currency=payout.currency,
                raw_payload=json.dumps({
                    "status_code": resp.status_code,
                    "request": body,
                    "response": resp.text[:1000],
                }),
            )
        )

        if resp.status_code != 202:
            return InitiatePayoutResult(
                rail_reference=reference_id,
                accepted=False,
                reason=f"momo_rejected_{resp.status_code}: {resp.text[:200]}",
            )

        # Already accepted by MTN (202). Don't fail the payout if the broker is
        # momentarily unreachable — the inbound webhook + beat sweep complete it.
        try:
            from ..tasks.polling import poll_mtn_disbursement
            poll_mtn_disbursement.apply_async(args=[payout.id, reference_id], countdown=5)
        except Exception as exc:
            current_app.logger.warning(
                "could not queue payout poller for payout %s (%s); "
                "relying on inbound webhook + sweep", payout.id, exc
            )
        return InitiatePayoutResult(rail_reference=reference_id, accepted=True)
