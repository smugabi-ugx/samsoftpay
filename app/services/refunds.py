"""Charge refunds.

A refund uses the Disbursement rail to return money to the original payer.

Ledger flow (via create_payout):
    DR merchant_available  +(net_amount + payout_fee)
    CR suspense            -net_amount
    CR psp_revenue         -payout_fee

On disbursement success:
    DR suspense            +net_amount
    CR psp_float           -net_amount

On disbursement failure:
    DR suspense            +net_amount
    DR psp_revenue         +payout_fee
    CR merchant_available  -(net_amount + payout_fee)

The merchant is refunded net_amount = original_amount - original_charge_fee.
The PSP absorbed the charge fee upfront; the payout fee covers the disbursement cost.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..extensions import db
from ..models import Channel, Merchant, Transaction, TxnStatus


class RefundError(Exception):
    pass


def refund_charge(txn: Transaction, merchant: Merchant) -> dict:
    """Initiate a refund for a succeeded charge.

    Returns {"ok": True, "payout": payout} on success,
    or {"ok": False, "error": "<reason>"} on failure.
    """
    if txn.merchant_id != merchant.id:
        raise RefundError("transaction does not belong to this merchant")

    if txn.status == TxnStatus.REFUNDED:
        return {"ok": False, "error": "already_refunded"}

    if txn.status != TxnStatus.SUCCEEDED:
        return {
            "ok": False,
            "error": f"cannot_refund_{txn.status.value}_transaction",
        }

    if not txn.customer_phone:
        return {"ok": False, "error": "no_customer_phone_on_record_to_refund_to"}

    # Net amount the merchant received after the charge fee was taken.
    net_amount = txn.amount - (txn.fee_amount or 0)
    if net_amount <= 0:
        return {"ok": False, "error": "net_refund_amount_is_zero"}

    try:
        from .payouts import PayoutError, create_payout

        payout = create_payout(
            merchant=merchant,
            amount=net_amount,
            currency=txn.currency,
            channel=Channel(txn.channel) if isinstance(txn.channel, str) else txn.channel,
            recipient_phone=txn.customer_phone,
            recipient_name="Customer",
        )
    except PayoutError as exc:
        return {"ok": False, "error": str(exc)}

    # Mark the original transaction as refunded only if payout was accepted.
    from ..models import PayoutStatus
    if payout.status in (PayoutStatus.PENDING, PayoutStatus.AUTHORIZED):
        txn.status = TxnStatus.REFUNDED
        txn.refunded_at = datetime.now(timezone.utc)
        txn.refund_payout_id = payout.id
        db.session.commit()

    return {"ok": True, "payout": payout}
