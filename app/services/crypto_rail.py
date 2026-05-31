"""Crypto payment rail via Binance Pay.

Set BINANCE_PAY_API_KEY + BINANCE_PAY_SECRET_KEY in .env to go live.
Supports USDT/USDC on BEP-20. Without credentials, falls back to mock.

How it works:
  1. We call Binance Pay to create an order → get a QR code / deep-link
  2. Customer scans QR on Binance app and approves
  3. Binance calls our webhook at /v1/crypto/callback
  4. We settle the UGX equivalent using the exchange rate at time of payment
"""
from __future__ import annotations
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

from flask import current_app


@dataclass
class CryptoOrderResult:
    accepted: bool
    rail_reference: str | None = None
    qr_url: str | None = None      # QR code image URL to show customer
    pay_url: str | None = None     # Deep-link to Binance app
    expires_at: int | None = None  # Unix timestamp
    reason: str | None = None


def create_crypto_order(*, amount_ugx: int, public_id: str, merchant_id: int) -> CryptoOrderResult:
    """Create a Binance Pay order. Falls back to mock when keys not set."""
    api_key = current_app.config.get("BINANCE_PAY_API_KEY", "")
    secret_key = current_app.config.get("BINANCE_PAY_SECRET_KEY", "")

    if not api_key:
        ref = f"crypto_mock_{uuid.uuid4().hex[:12]}"
        return CryptoOrderResult(
            accepted=True,
            rail_reference=ref,
            qr_url="https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=" + ref,
            pay_url=f"https://pay.binance.com/en/mock/{ref}",
            expires_at=int(time.time()) + 900,
        )

    import requests as _req
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    # Estimate USDT amount (very rough — production should use live FX rate)
    usdt_amount = round(amount_ugx / 3700, 2)  # ~3700 UGX per USDT

    body = {
        "env": {"terminalType": "WEB"},
        "merchantTradeNo": public_id,
        "orderAmount": usdt_amount,
        "currency": "USDT",
        "description": f"Samsoftpay order {public_id}",
        "returnUrl": current_app.config.get("BASE_URL", "") + "/pay/crypto/success",
        "cancelUrl": current_app.config.get("BASE_URL", "") + "/pay/crypto/cancel",
    }
    body_str = json.dumps(body, separators=(",", ":"))
    payload_str = f"{timestamp}\n{nonce}\n{body_str}\n"
    sig = hmac.new(secret_key.encode(), payload_str.encode(), hashlib.sha512).hexdigest().upper()

    try:
        resp = _req.post(
            "https://bpay.binanceapi.com/binancepay/openapi/v3/order",
            data=body_str,
            headers={
                "Content-Type": "application/json",
                "BinancePay-Timestamp": timestamp,
                "BinancePay-Nonce": nonce,
                "BinancePay-Certificate-SN": api_key,
                "BinancePay-Signature": sig,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == "SUCCESS":
            d = data["data"]
            return CryptoOrderResult(
                accepted=True,
                rail_reference=d.get("prepayId"),
                qr_url=d.get("qrcodeLink"),
                pay_url=d.get("deeplink"),
                expires_at=int(time.time()) + 900,
            )
        return CryptoOrderResult(accepted=False, reason=data.get("errorMessage", "order_failed"))
    except Exception as exc:
        return CryptoOrderResult(accepted=False, reason=str(exc))
