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

The CUSTOMER is refunded the FULL original amount. The platform returns its
charge fee to the merchant (psp_revenue -> merchant_available) so the
merchant's net cost of a refund is only the payout fee — the platform never
profits from a refunded sale.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..extensions import db
from ..models import Channel, Merchant, Transaction, TxnStatus


class RefundError(Exception):
    pass


def reconcile_failed_refund_payout(payout) -> None:
    """When a REFUND's disbursement FAILS at the rail, re-open the original charge.

    refund_charge marked the charge REFUNDED, returned the platform's charge fee to
    the merchant (_fee_return(+1)), then created this payout to pay the customer.
    complete_payout's failure path already reverses THIS payout's own earmark — but
    on its own that leaves the charge-fee return standing (merchant over-credited,
    psp_revenue short) and the charge stuck REFUNDED (a retry returns
    'already_refunded' — the customer could never be refunded again without DB
    surgery). So here we reverse the charge-fee return and set the charge back to
    SUCCEEDED so the merchant can retry. The early-release from hold STAYS — that
    money is legitimately the merchant's, now settled. Runs INSIDE the payout-
    failure transaction (caller commits), so it's atomic with the reversal.
    """
    from ..models import AccountType, TxnStatus
    from . import ledger as _ledger
    txn = Transaction.query.filter_by(refund_payout_id=payout.id).one_or_none()
    if txn is None or txn.status != TxnStatus.REFUNDED:
        return
    fee_back = int(txn.fee_amount or 0)
    mode = bool(txn.is_test)
    if fee_back > 0:
        rev = _ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE, merchant_id=None, currency=txn.currency, is_test=mode)
        avail = _ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=txn.merchant_id,
            currency=txn.currency, is_test=mode)
        # Reverse of _fee_return(+1): platform takes its charge fee back, merchant
        # available drops by the same — sums to zero, psp_revenue made whole.
        _ledger.post([(rev, -fee_back), (avail, +fee_back)], currency=txn.currency,
                     memo=f"refund fee-return reversed (refund disbursement failed) {txn.public_id}")
    txn.status = TxnStatus.SUCCEEDED
    txn.refunded_at = None
    txn.refund_payout_id = None


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

    # The customer is refunded IN FULL. They paid txn.amount; the old code
    # refunded amount − charge_fee, silently shorting every refunded customer
    # by the fee and making refunds revenue-positive for the platform
    # (confirmed by the gold-standard audit: paid 100,000 → refunded 98,500).
    refund_amount = txn.amount
    fee_back = int(txn.fee_amount or 0)
    if refund_amount <= 0:
        return {"ok": False, "error": "refund_amount_is_zero"}

    # CLAIM the refund inside the locked window, BEFORE creating the payout.
    # create_payout commits internally (releasing our row lock), so marking
    # REFUNDED only afterwards left a gap where a second request could slip
    # through and pay out twice. Claim first, commit (this is what the loser
    # of the race sees), then attempt the payout — and revert the claim if
    # the payout is refused so the merchant can retry.
    txn.status = TxnStatus.REFUNDED
    txn.refunded_at = datetime.now(timezone.utc)
    db.session.commit()

    # The platform GIVES BACK its charge fee (psp_revenue → merchant_available,
    # on the charge's own ledger) so the merchant's net cost of a refund is only
    # the payout fee — we don't profit from a refunded sale. Posted BEFORE the
    # payout so the balance check sees the compensated funds; reversed below if
    # the payout is refused.
    from ..models import AccountType
    from . import ledger as _ledger
    mode = bool(txn.is_test)

    def _fee_return(direction: int) -> None:
        if fee_back <= 0:
            return
        rev = _ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE, merchant_id=None,
            currency=txn.currency, is_test=mode)
        avail = _ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant.id,
            currency=txn.currency, is_test=mode)
        _ledger.post(
            [(rev, +fee_back * direction), (avail, -fee_back * direction)],
            currency=txn.currency,
            memo=(f"refund fee return {txn.public_id}" if direction > 0
                  else f"refund fee return reversal {txn.public_id}"))
        db.session.commit()

    _fee_return(+1)

    # EARLY-RELEASE FROM HOLD (adversarial money audit — HIGH): if this charge's
    # proceeds are still inside the settlement hold (settled_at is None, i.e. net
    # is sitting in MERCHANT_PENDING, not yet swept to MERCHANT_AVAILABLE), move
    # them pending -> available BEFORE funding the refund. Otherwise create_payout
    # debits `available` for money that never reached it, AND the charge's pending
    # credit is orphaned forever — the sweep only settles SUCCEEDED charges and
    # this one is now REFUNDED — silently over-debiting the merchant's withdrawable
    # balance by the net amount (ledger still sums to zero, so reconciliation
    # passes). Same direction as settlement.sweep_to_available (pending/available
    # are credit-normal, hence +net / -net). A settled or instant charge already
    # has settled_at set, so this is a no-op for them. If the payout is later
    # refused we leave this in place: the money is legitimately the merchant's and
    # now correctly settled — only the fee-return and REFUNDED claim are reverted.
    if txn.settled_at is None:
        net = int(txn.amount) - int(txn.fee_amount or 0)
        if net > 0:
            pending_acct = _ledger.get_or_create_account(
                type=AccountType.MERCHANT_PENDING, merchant_id=merchant.id,
                currency=txn.currency, is_test=mode)
            avail_acct = _ledger.get_or_create_account(
                type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant.id,
                currency=txn.currency, is_test=mode)
            _ledger.post(
                [(pending_acct, +net), (avail_acct, -net)],
                currency=txn.currency,
                memo=f"refund early-release from hold {txn.public_id}")
        txn.settled_at = datetime.now(timezone.utc)
        db.session.commit()

    try:
        from .payouts import DisbursementUnavailable, PayoutError, create_payout

        payout = create_payout(
            merchant=merchant,
            amount=refund_amount,
            currency=txn.currency,
            channel=Channel(txn.channel) if isinstance(txn.channel, str) else txn.channel,
            recipient_phone=txn.customer_phone,
            recipient_name="Customer",
        )
    except (PayoutError, DisbursementUnavailable) as exc:
        # Payout refused/unavailable with zero writes (guardrail 13) — reverse
        # the fee return and release the refund claim so the merchant can retry.
        _fee_return(-1)
        txn.status = TxnStatus.SUCCEEDED
        txn.refunded_at = None
        db.session.commit()
        return {"ok": False, "error": str(exc)}

    from ..models import PayoutStatus
    if payout.status in (PayoutStatus.PENDING, PayoutStatus.AUTHORIZED):
        txn.refund_payout_id = payout.id
        db.session.commit()
        # Tell the customer their refund is on the way. Best-effort, never raises.
        if not txn.is_test:
            from .emails import email_refund_issued
            email_refund_issued(txn, refund_amount)
    else:
        # Rail rejected synchronously — reverse the fee return, release claim.
        _fee_return(-1)
        txn.status = TxnStatus.SUCCEEDED
        txn.refunded_at = None
        db.session.commit()
        return {"ok": False, "error": "refund_payout_rejected"}

    return {"ok": True, "payout": payout}
