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


class DisbursementUnavailable(Exception):
    """A TRANSIENT rail/config failure (e.g. missing MOMO_DISBURSEMENT_* creds
    on the live switch). Deliberately NOT a PayoutError: callers treat
    PayoutError as a permanent client rejection and CACHE it under the
    idempotency key (a 400), which would make the config error un-retryable.
    This propagates past the `except PayoutError` branches to the generic
    handler that releases the key and returns a retryable 503."""
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
    # Same overflow guard as create_charge: a value above the signed 64-bit
    # BigInteger ceiling would crash at INSERT (OverflowError / NumericValueOutOfRange)
    # instead of failing cleanly. Reject it BEFORE any money moves.
    max_amount = current_app.config.get("MAX_TXN_AMOUNT", (1 << 63) - 1)
    if amount > max_amount:
        raise PayoutError(f"amount exceeds the maximum of {max_amount}")
    if currency != "UGX":
        raise PayoutError("demo only supports UGX")
    if not merchant.is_active:
        raise PayoutError("merchant is not active")
    # KYC gate on LIVE money creation (proven gap: a pending-KYC merchant's
    # sk_live_ key created real payouts — the dashboard wall never applied to
    # the API, the actual integration path). Test mode stays open: building
    # never requires approval. Zero writes on refusal.
    if g.get("api_mode") != "test" and merchant.kyc_status != "verified":
        raise PayoutError(
            "live payouts require a verified business — complete verification "
            "on your dashboard (test keys work immediately)")

    # Resolve the rail BEFORE any money moves. This used to happen after the
    # earmark was staged, so an unsupported channel (e.g. airtel_money) returned
    # a 400 while the merchant had already been debited amount+fee, with the
    # amount stranded in suspense and the payout stuck PENDING forever.
    adapter = _get_disbursement_adapter(channel)

    fee = calculate_payout_fee(currency=currency)

    # Which ledger this payout draws on. A test key spends the sandbox balance,
    # so integration testing can never drain a merchant's real funds. Resolved
    # once, here, and used for every account below.
    is_test = g.get("api_mode") == "test"

    if not is_test:
        # Platform payout kill switch (Pegasus lesson: the aggregator is THE
        # target and attacks are timed for when nobody is watching). One
        # command — `flask freeze-payouts on` — stops all live money-out
        # instantly, without a deploy. Zero writes on refusal.
        from .platform_flags import payouts_frozen
        if payouts_frozen():
            raise PayoutError(
                "payouts are temporarily paused platform-wide for a safety "
                "review — no action needed on your side; money in is unaffected")

        # NIBSS lesson: the window between "ledger says credited" and "money
        # is withdrawable" is the only place errors are cheaply reversible.
        # An OPEN critical reconciliation exception means our ledger and MTN
        # disagree about this merchant's money — freeze THEIR live payouts
        # until a human resolves it, instead of letting possibly-wrong money
        # become unrecoverable.
        from ..models import ReconException
        open_recon = ReconException.query.filter_by(
            merchant_id=merchant.id, status="open", severity="critical",
        ).count()
        if open_recon:
            raise PayoutError(
                "payouts are paused on this account while a payment "
                "reconciliation issue is investigated — support has been "
                "notified; your balance is safe and money in is unaffected")

    # Check the merchant has enough available balance (amount + fee).
    avail_acct = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE,
        merchant_id=merchant.id,
        currency=currency,
        is_test=is_test,
    )
    # Lock the balance row FOR UPDATE before reading it. Without this, two
    # concurrent payouts can both pass the check and overdraft the merchant
    # (classic double-spend). The lock serialises them until this txn commits.
    ledger.lock_account_for_update(avail_acct)
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
        is_test=is_test,
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
        is_test=is_test,
    )
    revenue = ledger.get_or_create_account(
        type=AccountType.PSP_REVENUE,
        merchant_id=None,
        currency=currency,
        is_test=is_test,
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

    # From here the merchant is already debited, so NOTHING may escape without
    # either committing a coherent state or undoing the earmark. `initiate` talks
    # to a real rail over the network: a timeout or an unexpected error must not
    # leave money in suspense against a payout that never left.
    try:
        result = adapter.initiate(payout)
    except PayoutError:
        db.session.rollback()
        raise
    except Exception as exc:
        # AMBIGUOUS network failure DURING the transfer call: MTN may have
        # received it and may still deliver. Rolling back here erased the
        # payout, the earmark AND the rail reference — money possibly
        # delivered with no record of it. Preserve everything and park the
        # payout as AUTHORIZED for `flask stranded-payouts`/reconciliation
        # to resolve against MTN's own answer. Clean pre-send failures
        # (token fetch etc.) still take the rollback path below.
        from .rails_mtn_disbursement import AmbiguousRailError
        if isinstance(exc, AmbiguousRailError):
            payout.rail_reference = exc.rail_reference
            payout.status = PayoutStatus.AUTHORIZED
            payout.failure_reason = "ambiguous_network_error_pending_reconciliation"
            db.session.commit()
            current_app.logger.error(
                "AMBIGUOUS payout %s (ref %s): network failed mid-transfer — "
                "parked for reconciliation, merchant earmark preserved",
                payout.public_id, exc.rail_reference,
            )
            return payout
        db.session.rollback()
        raise PayoutError(f"disbursement rail unavailable: {exc}") from exc

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
    # Row-lock before the terminal check — same race as complete_transaction:
    # the inbound webhook and a queued poller can both see AUTHORIZED and both
    # post the completion (double release or double refund).
    db.session.refresh(payout, with_for_update=True)
    if payout.status in (PayoutStatus.SUCCEEDED, PayoutStatus.FAILED):
        db.session.commit()   # release the lock
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

    # Completion must land in the SAME ledger the payout was earmarked from.
    mode = bool(payout.is_test)
    in_flight = ledger.get_or_create_account(
        type=AccountType.SUSPENSE,
        merchant_id=payout.merchant_id,
        currency=payout.currency,
        is_test=mode,
    )

    if success:
        psp_float = ledger.get_or_create_account(
            type=AccountType.PSP_FLOAT,
            merchant_id=None,
            currency=payout.currency,
            is_test=mode,
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
            is_test=mode,
        )
        psp_revenue = ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE,
            merchant_id=None,
            currency=payout.currency,
            is_test=mode,
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

    # Terminal side-effects, AFTER the money is committed. Never raise — a
    # bookkeeping/notification failure must not disturb a posted completion
    # (same pattern as the charge-side hooks).
    _finish_withdrawal_request(payout)
    _queue_payout_webhook(payout)


def _finish_withdrawal_request(payout) -> None:
    """A WithdrawalRequest used to stay 'processing' FOREVER — nothing anywhere
    moved it to a terminal state after its payout succeeded or failed, so the
    merchant's dashboard never confirmed their money left (and a failed payout
    never surfaced for admin retry). Never raises."""
    try:
        from ..models import WithdrawalRequest
        wr = WithdrawalRequest.query.filter_by(payout_id=payout.id).first()
        if wr is None or wr.status not in ("pending", "processing"):
            return
        wr.status = ("completed" if payout.status == PayoutStatus.SUCCEEDED
                     else "failed")
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            current_app.logger.exception(
                "failed to finalise withdrawal request for payout %s", payout.public_id)
        except Exception:
            pass


def _queue_payout_webhook(payout) -> None:
    """payout.succeeded / payout.failed events — payout outcomes were poll-only
    (the event catalogue stopped at charge.* / vending.*), so a platform like
    KarlPOS finalising on the 201 could lose a late rail failure. Same signed
    envelope + per-merchant whsec as every other event. Never raises."""
    try:
        merchant = db.session.get(Merchant, payout.merchant_id)
        if not merchant or not getattr(merchant, "webhook_url", None):
            return
        from .webhooks import enqueue
        enqueue(merchant, f"payout.{payout.status.value}", {
            "id": payout.public_id,
            "mode": "test" if payout.is_test else "live",
            "status": payout.status.value,
            "amount": payout.amount,
            "fee": payout.fee_amount,
            "currency": payout.currency,
            "recipient_phone": payout.recipient_phone,
            "rail_reference": payout.rail_reference,
            "failure_reason": payout.failure_reason,
            "completed_at": payout.completed_at.isoformat() if payout.completed_at else None,
        })
    except Exception:
        db.session.rollback()
        try:
            current_app.logger.exception(
                "failed to enqueue payout webhook for %s", payout.public_id)
        except Exception:
            pass


# ---------- Adapter selection ----------

def _get_disbursement_adapter(channel: Channel):
    if channel != Channel.MTN_MOMO:
        raise PayoutError(f"no disbursement adapter for channel {channel}")
    sandbox = g.get("api_mode") == "test"
    if not sandbox and current_app.config.get("MOMO_USE_REAL"):
        from .rails_mtn_disbursement import RealMTNMoMoDisbursementAdapter
        try:
            return RealMTNMoMoDisbursementAdapter()
        except Exception as exc:
            # Missing/mis-set MOMO_DISBURSEMENT_* creds. Raise the TRANSIENT
            # exception (not PayoutError) so the route's generic handler
            # releases the idempotency key and returns a retryable 503 — a
            # PayoutError here would be cached as a permanent 400 and block
            # retries after the config is fixed. Zero writes (before earmark).
            raise DisbursementUnavailable(
                "disbursement temporarily unavailable — configuration error") from exc
    return _MockDisbursementAdapter()


class _MockDisbursementAdapter:
    """In-process mock for fast local testing."""

    channel = Channel.MTN_MOMO

    def resolve_account_holder(self, phone: str) -> dict:
        """Deterministic sandbox Hakikisha: the not-found magic number resolves
        inactive with no name; every other number is an active test holder."""
        from .rails import TEST_PAYOUT_OUTCOMES, _phone_key
        key = _phone_key(phone)
        if TEST_PAYOUT_OUTCOMES.get(key) == "recipient_not_found":
            return {"active": False, "registered_name": None}
        return {"active": True,
                "registered_name": f"SANDBOX HOLDER {key[-4:] or '0000'}"}

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

        # DETERMINISTIC sandbox, like collections (guardrail 16): a magic
        # recipient number produces its documented outcome every time; every
        # other number succeeds. Randomness only via the legacy knob.
        from .rails import TEST_PAYOUT_OUTCOMES, _phone_key
        forced = TEST_PAYOUT_OUTCOMES.get(_phone_key(payout.recipient_phone), "unset")

        def _fire():
            with app.app_context():
                if forced != "unset":
                    success, reason = forced is None, forced
                else:
                    success = random.random() < prob
                    reason = None if success else random.choice([
                        "recipient_not_found", "wallet_locked", "timeout"
                    ])
                complete_payout(payout_id, success=success,
                                rail_reference=rail_ref, reason=reason)

        threading.Timer(delay, _fire).start()
        return InitiatePayoutResult(rail_reference=rail_ref, accepted=True)
