"""Visa / Card rail via Flutterwave.

Set FLUTTERWAVE_SECRET_KEY in .env to go live.
Without it, falls back to the mock rail automatically.
"""
from __future__ import annotations
from dataclasses import dataclass

from flask import current_app

from ..models import Transaction


@dataclass
class CardInitiateResult:
    accepted: bool
    rail_reference: str | None = None
    redirect_url: str | None = None  # 3DS redirect if required
    reason: str | None = None


def initiate_card_charge(txn: Transaction, card_number: str = "") -> CardInitiateResult:
    """Charge a card via Flutterwave. Falls back to mock when key not set."""
    secret = current_app.config.get("FLUTTERWAVE_SECRET_KEY", "")
    if not secret:
        # Mock — simulate success after delay (same as existing mock rail)
        import uuid, random, threading
        ref = f"flw_mock_{uuid.uuid4().hex[:12]}"
        app = current_app._get_current_object()
        txn_id = txn.id
        def _fire():
            from .orchestrator import complete_transaction
            with app.app_context():
                complete_transaction(txn_id, success=random.random() < 0.85,
                                     rail_reference=ref)
        threading.Timer(app.config["RAIL_CALLBACK_DELAY_SECONDS"], _fire).start()
        return CardInitiateResult(accepted=True, rail_reference=ref)

    # ── Real Flutterwave integration ──────────────────────────────
    import requests as _req
    payload = {
        "tx_ref": f"ssp_{txn.public_id}",
        "amount": str(txn.amount / 100),   # Flutterwave uses major units
        "currency": txn.currency,
        "card_number": card_number,
        "redirect_url": current_app.config.get("BASE_URL", "") + "/v1/flw/callback",
        "meta": {"merchant_id": txn.merchant_id, "txn_public_id": txn.public_id},
    }
    try:
        resp = _req.post(
            "https://api.flutterwave.com/v3/charges?type=card",
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == "success":
            charge_data = data.get("data", {})
            return CardInitiateResult(
                accepted=True,
                rail_reference=charge_data.get("flw_ref"),
                redirect_url=charge_data.get("meta", {}).get("authorization", {}).get("redirect"),
            )
        return CardInitiateResult(accepted=False, reason=data.get("message", "card_declined"))
    except Exception as exc:
        return CardInitiateResult(accepted=False, reason=str(exc))
