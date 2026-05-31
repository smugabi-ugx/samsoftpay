"""Payout orchestration.

A payout takes money from a merchant's available balance and sends it out
via a disbursement rail (MTN MoMo Disbursements).

Ledger model:

CREATING a payout (synchronous, pre-rail):
    DR merchant_available          +amount   (reduces what we owe merchant; remember
                                              merchant_available is stored as a negative
                                              number, so debiting means moving it back
                                              toward zero)
    CR payout_in_flight            -amount   (we earmark the money — it's leaving but
                                              not yet confirmed)

PAYOUT SUCCEEDS (rail confirmed):
    DR payout_in_flight            +amount
    CR psp_float                   -amount   (our MoMo float covers the delivery; MTN
                                              actually debited our MoMo balance for this)

PAYOUT FAILS:
    DR payout_in_flight            +amount
    CR merchant_available          -amount   (give it back to the merchant)

This keeps the ledger balanced no matter the outcome. The payout_in_flight
account is a useful suspense bucket so we always know how much money is "in
the air" at any moment.
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import current_app, g

from ..extensions import db
from ..models import (
    AccountType,
    Channel,
    Merchant,
    Payout,
    PayoutStatus,
    RailEvent,
)
from . import ledger
from .fees import calculate_payout_fee


class PayoutError(Exception):
    pass


def create_payout(
    *,
    merchant: Merchant,
    amount: int,
    currency: str,
    recipient_phone: str,
    recipient_name: str | None = None,
    channel: Channel = Channel.MTN_MOMO,
) -> Payout:
    if amount <= 0:
        raise PayoutError("amount must be positive")
    if currency != "UGX":
        raise PayoutError("demo only supports UGX")
    if not merchant.is_active:
        raise PayoutError("merchant is not active")

    fee = calculate_payout_fee(currency=currency)

    # Check the merchant has enough available balance (amount + fee).
    avail_acct = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE,
        merchant_id=merchant.id,
        currency=currency,
    )
    # available is stored as a credit (negative). Convert to positive.
    available_positive = -avail_acct.cached_balance
    if available_positive < amount + fee:
        raise PayoutError(
            f"insufficient available balance: have {available_positive}, "
            f"need {amount + fee} (amount {amount} + fee {fee})"
        )

    payout = Payout(
        public_id=f"pout_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        fee_amount=fee,
        currency=currency,
        channel=channel,
        status=PayoutStatus.PENDING,
        is_test=g.get("api_mode") == "test",
        recipient_phone=recipient_phone,
        recipient_name=recipient_name,
    )
    db.session.add(payout)
    db.session.flush()

    # Earmark payout amount into in-flight; credit fee to PSP revenue immediately.
    in_flight = ledger.get_or_create_account(
        type=AccountType.SUSPENSE,
        merchant_id=merchant.id,
        currency=currency,
    )
    revenue = ledger.get_or_create_account(
        type=AccountType.PSP_REVENUE,
        merchant_id=None,
        currency=currency,
    )
    ledger.post(
        [
            (avail_acct, +(amount + fee)),
            (in_flight, -amount),
            (revenue, -fee),
        ],
        currency=currency,
        memo=f"payout {payout.public_id} earmarked (fee {fee})",
    )

    # Pick a disbursement adapter and initiate.
    adapter = _get_disbursement_adapter(channel)
    result = adapter.initiate(payout)
    if not result.accepted:
        # Reverse the earmark; payout never left.
        ledger.post(
            [
                (in_flight, +amount),
                (avail_acct, -amount),
            ],
            currency=currency,
            memo=f"payout {payout.public_id} rejected by rail",
        )
        payout.status = PayoutStatus.FAILED
        payout.failure_reason = result.reason or "rail_rejected"
        db.session.commit()
        return payout

    payout.rail_reference = result.rail_reference
    payout.status = PayoutStatus.AUTHORIZED
    db.session.commit()
    return payout


def complete_payout(
    payout_id: int, *, success: bool, rail_reference: str, reason: str | None = None
) -> None:
    payout = db.session.get(Payout, payout_id)
    if payout is None:
        return
    if payout.status in (PayoutStatus.SUCCEEDED, PayoutStatus.FAILED):
        return  # idempotent

    db.session.add(
        RailEvent(
            rail=payout.channel,
            rail_reference=rail_reference,
            event_type="payout_succeeded" if success else "payout_failed",
            amount=payout.amount,
            currency=payout.currency,
            raw_payload=json.dumps({"reason": reason, "payout_id": payout.public_id}),
        )
    )

    in_flight = ledger.get_or_create_account(
        type=AccountType.SUSPENSE,
        merchant_id=payout.merchant_id,
        currency=payout.currency,
    )

    if success:
        psp_float = ledger.get_or_create_account(
            type=AccountType.PSP_FLOAT,
            merchant_id=None,
            currency=payout.currency,
        )
        # Money left our MoMo float; release the in_flight earmark.
        ledger.post(
            [
                (in_flight, +payout.amount),
                (psp_float, -payout.amount),
            ],
            currency=payout.currency,
            memo=f"payout {payout.public_id} delivered",
        )
        payout.status = PayoutStatus.SUCCEEDED
    else:
        # Reverse both the earmark and the fee — full refund to merchant.
        avail_acct = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE,
            merchant_id=payout.merchant_id,
            currency=payout.currency,
        )
        psp_revenue = ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE,
            merchant_id=None,
            currency=payout.currency,
        )
        ledger.post(
            [
                (in_flight, +payout.amount),
                (psp_revenue, +payout.fee_amount),
                (avail_acct, -(payout.amount + payout.fee_amount)),
            ],
            currency=payout.currency,
            memo=f"payout {payout.public_id} failed, full refund to merchant",
        )
        payout.status = PayoutStatus.FAILED
        payout.failure_reason = reason or "unknown"

    payout.completed_at = datetime.now(timezone.utc)
    db.session.commit()


# ---------- Adapter selection ----------

def _get_disbursement_adapter(channel: Channel):
    if channel != Channel.MTN_MOMO:
        raise PayoutError(f"no disbursement adapter for channel {channel}")
    sandbox = g.get("api_mode") == "test"
    if not sandbox and current_app.config.get("MOMO_USE_REAL"):
        from .rails_mtn_disbursement import RealMTNMoMoDisbursementAdapter
        return RealMTNMoMoDisbursementAdapter()
    return _MockDisbursementAdapter()


class _MockDisbursementAdapter:
    """In-process mock for fast local testing."""

    channel = Channel.MTN_MOMO

    def initiate(self, payout: Payout):
        from .rails_mtn_disbursement import InitiatePayoutResult
        rail_ref = f"disb_mock_{uuid.uuid4().hex[:12]}"

        db.session.add(
            RailEvent(
                rail=Channel.MTN_MOMO,
                rail_reference=rail_ref,
                event_type="payout_initiated",
                amount=payout.amount,
                currency=payout.currency,
                raw_payload=json.dumps({"mock": True, "payout_id": payout.public_id}),
            )
        )

        app = current_app._get_current_object()
        delay = app.config["RAIL_CALLBACK_DELAY_SECONDS"]
        prob = app.config["RAIL_SUCCESS_PROBABILITY"]
        payout_id = payout.id

        def _fire():
            with app.app_context():
                success = random.random() < prob
                reason = None if success else random.choice([
                    "recipient_not_found", "wallet_locked", "timeout"
                ])
                complete_payout(payout_id, success=success,
                                rail_reference=rail_ref, reason=reason)

        threading.Timer(delay, _fire).start()
        return InitiatePayoutResult(rail_reference=rail_ref, accepted=True)
