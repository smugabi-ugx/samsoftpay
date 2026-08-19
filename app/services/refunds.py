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

    # Split charges are NOT refundable in v1 — the adversarial money audit found
    # three critical hazards in split refunds (clawback stranding when the
    # customer disbursement fails, cross-mode ledger leak, in-hold funding).
    # Reject cleanly with ZERO writes until the hardened split-refund lands;
    # never ship a known-unsafe money path.
    if getattr(txn, "split_meta", None):
        return {"ok": False, "error": "split_charge_refunds_not_yet_supported"}

    # Row-lock before the status checks: two concurrent refund requests for
    # the same charge both read SUCCEEDED and both created a refund payout —
    # double money out. FOR UPDATE serialises them; the loser re-reads
    # REFUNDED below. (No-op on SQLite, same as every other money lock here.)
    db.session.refresh(txn, with_for_update=True)

    if txn.status == TxnStatus.REFUNDED:
        return {"ok": False, "error": "already_refunded"}

    if txn.status != TxnStatus.SUCCEEDED:
        return {
            "ok": False,
            "error": f"cannot_refund_{txn.status.value}_transaction",
        }

    if not txn.customer_phone:
        return {"ok": False, "error": "no_customer_phone_on_record_to_refund_to"}

    # ── MODE SCOPING (executed-and-proven bug): the refund payout MUST live on
    # the SAME ledger as the charge. The dashboard path sets no g.api_mode, so
    # create_payout defaulted to LIVE — refunding a TEST charge debited REAL
    # withdrawable money; conversely a test key could mark a LIVE charge
    # REFUNDED while paying from sandbox. Rules (guardrail 12/15 discipline):
    #   1. An API caller whose key mode mismatches the charge mode is refused
    #      with zero writes.
    #   2. The refund's funding payout is ALWAYS forced to txn.is_test.
    from flask import g, has_request_context
    caller_mode = g.get("api_mode") if has_request_context() else None
    txn_mode = "test" if txn.is_test else "live"
    if caller_mode is not None and caller_mode != txn_mode:
        return {"ok": False,
                "error": f"mode_mismatch: this is a {txn_mode} charge — use your "
                         f"{'sk_test_' if txn.is_test else 'sk_live_'} key to refund it"}
    if has_request_context():
        g.api_mode = txn_mode   # create_payout reads this to pick the ledger

    # Net amount the merchant received after the charge fee was taken.
    net_amount = txn.amount - (txn.fee_amount or 0)
    if net_amount <= 0:
        return {"ok": False, "error": "net_refund_amount_is_zero"}

    # CLAIM the refund inside the locked window, BEFORE creating the payout.
    # create_payout commits internally (releasing our row lock), so marking
    # REFUNDED only afterwards left a gap where a second request could slip
    # through and pay out twice. Claim first, commit (this is what the loser
    # of the race sees), then attempt the payout — and revert the claim if
    # the payout is refused so the merchant can retry.
    txn.status = TxnStatus.REFUNDED
    txn.refunded_at = datetime.now(timezone.utc)
    db.session.commit()

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
        # Payout refused with zero writes (guardrail 13) — release the claim.
        txn.status = TxnStatus.SUCCEEDED
        txn.refunded_at = None
        db.session.commit()
        return {"ok": False, "error": str(exc)}

    from ..models import PayoutStatus
    if payout.status in (PayoutStatus.PENDING, PayoutStatus.AUTHORIZED):
        txn.refund_payout_id = payout.id
        db.session.commit()
    else:
        # Rail rejected synchronously — release the claim.
        txn.status = TxnStatus.SUCCEEDED
        txn.refunded_at = None
        db.session.commit()
        return {"ok": False, "error": "refund_payout_rejected"}

    return {"ok": True, "payout": payout}
