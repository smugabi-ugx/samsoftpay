"""Orchestrator — the brain.

This is where business rules live:
- creating a transaction
- choosing a rail
- writing the initial ledger posting (provisional)
- handling rail completion (settling the ledger)
- queuing a webhook
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from flask import current_app, g

from ..extensions import db
from ..models import (
    AccountType,
    Channel,
    Merchant,
    RailEvent,
    Transaction,
    TxnStatus,
    WebhookDelivery,
    utcnow,
)
from . import ledger
from .fees import calculate_fee
from .rails import get_adapter
from .webhooks import sign_payload


class OrchestratorError(Exception):
    pass


def create_charge(
    *,
    merchant: Merchant,
    amount: int,
    currency: str,
    channel: Channel,
    customer_phone: str | None,
    customer_email: str | None,
    merchant_reference: str | None,
) -> Transaction:
    if amount <= 0:
        raise OrchestratorError("amount must be positive")
    if currency != "UGX":
        raise OrchestratorError("demo only supports UGX")
    if not merchant.is_active:
        raise OrchestratorError("merchant is not active")

    fee = calculate_fee(amount=amount, channel=channel, currency=currency)
    if fee >= amount:
        raise OrchestratorError("fee exceeds amount")

    txn = Transaction(
        public_id=f"txn_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        fee_amount=fee,
        currency=currency,
        channel=channel,
        status=TxnStatus.PENDING,
        is_test=g.get("api_mode") == "test",
        merchant_reference=merchant_reference,
        customer_phone=customer_phone,
        customer_email=customer_email,
    )
    db.session.add(txn)
    db.session.flush()  # so txn.id is available

    # Initiate at the rail. The rail will (asynchronously) call back with success/fail.
    adapter = get_adapter(channel)
    result = adapter.initiate(txn)
    if not result.accepted:
        txn.status = TxnStatus.FAILED
        txn.failure_reason = result.reason or "rail_rejected"
        db.session.commit()
        return txn

    txn.rail_reference = result.rail_reference
    txn.status = TxnStatus.AUTHORIZED  # rail accepted; awaiting completion
    db.session.commit()
    return txn


def complete_transaction(
    txn_id: int, *, success: bool, rail_reference: str, reason: str | None = None
) -> None:
    """Called by the rail callback (or timer in our mock)."""
    txn = db.session.get(Transaction, txn_id)
    if txn is None:
        return
    if txn.status in (TxnStatus.SUCCEEDED, TxnStatus.FAILED):
        # Idempotent — callback was already processed.
        return

    # Persist the rail event for reconciliation.
    db.session.add(
        RailEvent(
            rail=txn.channel,
            rail_reference=rail_reference,
            event_type="succeeded" if success else "failed",
            amount=txn.amount,
            currency=txn.currency,
            raw_payload=json.dumps({"reason": reason}),
        )
    )

    if success:
        # Post the settlement: rail_clearing receives funds, merchant_pending
        # gets amount-fee, psp_revenue gets fee.
        rail_acct = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING,
            merchant_id=None,
            currency=txn.currency,
        )
        merch_pending = ledger.get_or_create_account(
            type=AccountType.MERCHANT_PENDING,
            merchant_id=txn.merchant_id,
            currency=txn.currency,
        )
        revenue = ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE,
            merchant_id=None,
            currency=txn.currency,
        )
        ledger.post(
            [
                (rail_acct, +txn.amount),
                (merch_pending, -(txn.amount - txn.fee_amount)),
                (revenue, -txn.fee_amount),
            ],
            currency=txn.currency,
            transaction_id=txn.id,
            memo=f"charge {txn.public_id} succeeded",
        )
        txn.status = TxnStatus.SUCCEEDED
    else:
        txn.status = TxnStatus.FAILED
        txn.failure_reason = reason or "unknown"

    txn.completed_at = datetime.now(timezone.utc)
    db.session.commit()

    _queue_webhook(txn)


def _queue_webhook(txn: Transaction) -> None:
    merchant = db.session.get(Merchant, txn.merchant_id)
    if not merchant or not merchant.webhook_url:
        return
    payload = json.dumps(
        {
            "event": f"charge.{txn.status.value}",
            "data": {
                "id": txn.public_id,
                "amount": txn.amount,
                "fee": txn.fee_amount,
                "currency": txn.currency,
                "channel": txn.channel.value,
                "status": txn.status.value,
                "merchant_reference": txn.merchant_reference,
                "failure_reason": txn.failure_reason,
                "completed_at": txn.completed_at.isoformat() if txn.completed_at else None,
            },
        },
        separators=(",", ":"),  # canonical JSON for signing
    )
    secret = current_app.config["WEBHOOK_SIGNING_SECRET"]
    sig = sign_payload(payload, secret)
    db.session.add(
        WebhookDelivery(
            merchant_id=merchant.id,
            transaction_id=txn.id,
            url=merchant.webhook_url,
            payload=payload,
            signature=sig,
            next_attempt_at=utcnow(),
        )
    )
    db.session.commit()
