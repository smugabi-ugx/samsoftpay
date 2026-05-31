"""Visa / Mastercard card processing via Flutterwave.

Flutterwave is licensed in Uganda and processes Visa/MC directly.
When you eventually get direct Visa PSP certification, swap this adapter —
the rest of the codebase stays identical.

Set in .env:
    FLUTTERWAVE_SECRET_KEY=FLW-XXXXXXX   (from dashboard.flutterwave.com)
    BASE_URL=https://yourdomain.com       (for 3DS callback URL)

Without FLUTTERWAVE_SECRET_KEY, falls back to mock for local dev.

Flutterwave docs: https://developer.flutterwave.com/docs/collecting-payments/card
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import requests as _req
from flask import current_app


@dataclass
class CardChargeResult:
    accepted: bool
    rail_reference: str | None = None
    redirect_url: str | None = None   # 3DS redirect — send customer here
    requires_redirect: bool = False
    reason: str | None = None


def charge_card(
    *,
    txn_public_id: str,
    amount_ugx: int,
    currency: str,
    card_number: str,
    cvv: str,
    expiry_month: str,
    expiry_year: str,
    cardholder_name: str,
    customer_email: str = "",
    customer_phone: str = "",
) -> CardChargeResult:
    secret = current_app.config.get("FLUTTERWAVE_SECRET_KEY", "")

    if not secret:
        # ── Mock ─────────────────────────────────────────────────────
        import random, threading
        ref = f"flw_mock_{uuid.uuid4().hex[:12]}"
        app = current_app._get_current_object()
        # We don't have txn_id here — caller must link rail_reference after
        return CardChargeResult(
            accepted=True,
            rail_reference=ref,
            requires_redirect=False,
        )

    # ── Real Flutterwave ──────────────────────────────────────────────
    payload = {
        "card_number":    card_number.replace(" ", ""),
        "cvv":            cvv,
        "expiry_month":   expiry_month.zfill(2),
        "expiry_year":    expiry_year[-2:],     # 2-digit year
        "currency":       currency,
        "amount":         str(amount_ugx),
        "fullname":       cardholder_name,
        "email":          customer_email or "noreply@samsoftpay.com",
        "phone_number":   customer_phone or "256700000000",
        "tx_ref":         f"ssp_{txn_public_id}",
        "redirect_url":   (
            current_app.config.get("BASE_URL", "http://localhost:5000")
            + f"/v1/card/flw-callback/{txn_public_id}"
        ),
        "authorization": {
            "mode": "pin"   # start with PIN; Flutterwave upgrades to 3DS if needed
        },
        "meta": {"txn_public_id": txn_public_id},
    }

    try:
        resp = _req.post(
            "https://api.flutterwave.com/v3/charges?type=card",
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=15,
        )
        data = resp.json()
        meta = (data.get("data") or {}).get("meta") or {}
        auth = meta.get("authorization") or {}

        if data.get("status") == "success":
            d = data["data"]
            # Check if 3DS redirect needed
            if auth.get("mode") in ("redirect", "3dsecure"):
                return CardChargeResult(
                    accepted=True,
                    rail_reference=d.get("flw_ref"),
                    redirect_url=auth.get("redirect"),
                    requires_redirect=True,
                )
            # Direct charge succeeded
            return CardChargeResult(
                accepted=True,
                rail_reference=d.get("flw_ref"),
                requires_redirect=False,
            )

        return CardChargeResult(
            accepted=False,
            reason=data.get("message", "card_declined"),
        )
    except Exception as exc:
        return CardChargeResult(accepted=False, reason=str(exc))


def verify_flw_transaction(flw_id: str) -> bool:
    """Verify a Flutterwave transaction by ID after 3DS redirect. Returns True if charged."""
    secret = current_app.config.get("FLUTTERWAVE_SECRET_KEY", "")
    if not secret:
        return True   # mock always succeeds
    try:
        resp = _req.get(
            f"https://api.flutterwave.com/v3/transactions/{flw_id}/verify",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=10,
        )
        data = resp.json()
        return (data.get("data") or {}).get("status") == "successful"
    except Exception:
        return False
