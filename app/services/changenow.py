"""ChangeNow crypto payment adapter.

How it works:
  1. Customer chooses "Pay with Crypto" and selects a coin (BTC, ETH, USDT, …)
  2. We call ChangeNow to create an exchange: <coin> → USDT (to our receiving wallet)
  3. ChangeNow returns a deposit address + exchange ID
  4. We show the customer the deposit address and a QR code
  5. Customer sends crypto to that address from any wallet
  6. ChangeNow converts to USDT and sends to our wallet
  7. We poll GET /v1/transactions/{id} until status = 'finished'
  8. Mark the Samsoftpay transaction as succeeded

Set in .env:
    CHANGENOW_API_KEY=your_api_key           (from changenow.io/developers)
    CHANGENOW_RECEIVING_ADDRESS=your_usdt_wallet  (e.g. BSC BEP-20 USDT address)
    CHANGENOW_RECEIVING_NETWORK=bsc          (network for receiving wallet)

Without these, falls back to mock mode for development.

API reference: https://documenter.getpostman.com/view/8180765/SVfTPnM8
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests as _req
from flask import current_app

_BASE = "https://api.changenow.io/v1"

# Coins we accept from customers (displayed in checkout)
SUPPORTED_COINS = [
    ("btc",   "Bitcoin",         "BTC"),
    ("eth",   "Ethereum",        "ETH"),
    ("usdtbsc", "USDT (BEP-20)", "USDT"),
    ("usdteth", "USDT (ERC-20)", "USDT"),
    ("bnb",   "BNB",             "BNB"),
    ("sol",   "Solana",          "SOL"),
]


@dataclass
class CryptoOrderResult:
    accepted: bool
    exchange_id: str | None = None
    deposit_address: str | None = None
    deposit_coin: str | None = None
    deposit_amount_estimate: float | None = None
    expires_at: int | None = None  # unix ts — ChangeNow locks rate for ~20 min
    reason: str | None = None
    extra: dict = field(default_factory=dict)


def get_estimate(*, from_coin: str, amount_ugx: int) -> dict:
    """Get estimated crypto amount needed to pay amount_ugx worth.

    Uses a rough UGX/USD rate (3700) then queries ChangeNow for the
    BTC/ETH/USDT equivalent. In production, pull live FX from an FX API.
    """
    api_key = current_app.config.get("CHANGENOW_API_KEY", "")
    to_coin = "usdtbsc"   # we receive USDT on BSC

    # Rough conversion: UGX → USD → target coin
    usd_amount = round(amount_ugx / 3700, 2)
    if from_coin in ("usdtbsc", "usdteth"):
        return {"estimated_amount": usd_amount, "coin": from_coin}

    url = f"{_BASE}/exchange-amount/{usd_amount}/usdt_{from_coin}"
    params = {"api_key": api_key} if api_key else {}
    try:
        resp = _req.get(url, params=params, timeout=8)
        data = resp.json()
        return {"estimated_amount": data.get("estimatedAmount", usd_amount), "coin": from_coin}
    except Exception:
        return {"estimated_amount": usd_amount, "coin": from_coin}


def create_exchange(
    *,
    from_coin: str,
    amount_ugx: int,
    public_id: str,
) -> CryptoOrderResult:
    """Create a ChangeNow exchange. Returns a deposit address for the customer."""
    api_key = current_app.config.get("CHANGENOW_API_KEY", "")
    recv_address = current_app.config.get("CHANGENOW_RECEIVING_ADDRESS", "")
    recv_network = current_app.config.get("CHANGENOW_RECEIVING_NETWORK", "bsc")

    if not api_key or not recv_address:
        # ── Mock mode ───────────────────────────────────────────────────
        import uuid, time
        return CryptoOrderResult(
            accepted=True,
            exchange_id=f"cn_mock_{uuid.uuid4().hex[:12]}",
            deposit_address="0xMOCK_ADDRESS_FOR_DEV_DO_NOT_SEND",
            deposit_coin=from_coin.upper(),
            deposit_amount_estimate=round(amount_ugx / 3700, 6),
            expires_at=int(time.time()) + 1200,
            extra={"mock": True},
        )

    # ── Real ChangeNow API ───────────────────────────────────────────────
    usd_amount = round(amount_ugx / 3700, 6)
    to_coin = "usdtbsc" if recv_network == "bsc" else "usdteth"

    payload = {
        "from": from_coin,
        "to": to_coin,
        "address": recv_address,
        "amount": usd_amount,
        "flow": "standard",       # standard = floating rate (no min limits)
        "type": "direct",
        "rateId": "",
        "refundAddress": "",
        "contactEmail": "",
        "extraId": public_id,     # our reference, returned in callbacks
    }
    try:
        resp = _req.post(
            f"{_BASE}/transactions/{api_key}",
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if "error" in data:
            return CryptoOrderResult(accepted=False, reason=data.get("message", data["error"]))

        return CryptoOrderResult(
            accepted=True,
            exchange_id=data["id"],
            deposit_address=data["payinAddress"],
            deposit_coin=data.get("fromCurrency", from_coin).upper(),
            deposit_amount_estimate=float(data.get("amount", usd_amount)),
            extra=data,
        )
    except Exception as exc:
        return CryptoOrderResult(accepted=False, reason=str(exc))


def get_status(exchange_id: str) -> str:
    """Poll ChangeNow for exchange status.

    Returns one of: waiting | confirming | exchanging | sending | finished | failed | refunded
    """
    api_key = current_app.config.get("CHANGENOW_API_KEY", "")
    if not api_key or exchange_id.startswith("cn_mock_"):
        return "finished"  # mock always succeeds

    try:
        resp = _req.get(
            f"{_BASE}/transactions/{exchange_id}/{api_key}",
            timeout=8,
        )
        return resp.json().get("status", "waiting")
    except Exception:
        return "waiting"
