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
    utcnow,
)
from . import ledger
from .fees import calculate_fee
from .rails import get_adapter


class OrchestratorError(Exception):
    pass


def _simulated_rail_forbidden(channel: Channel) -> bool:
    """Whether this channel must be refused for a live charge.

    Sandbox is exempt: an sk_test_ key is *supposed* to hit mocks, and since the
    test/live ledger split that money cannot reach a real balance anyway.

    Enforced only on Render. Locally and in the test suite the mocks ARE the
    rails — that is how the suite runs offline — so gating on the production
    environment keeps development working while closing the hole where it counts.
    """
    import os

    if not os.environ.get("RENDER"):
        return False
    if current_app.config.get("ALLOW_SIMULATED_RAILS"):
        return False
    if g.get("api_mode") == "test":
        return False
    from .rails import is_simulated

    return is_simulated(channel)


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
    # Upper bound BEFORE the DB. amount is a BigInteger (signed 64-bit); a value
    # above that ceiling used to reach the INSERT and crash — OverflowError on
    # SQLite, NumericValueOutOfRange on Postgres — surfacing to the caller as a
    # misleading 502 "rail unavailable, retry". Reject it cleanly as a 400. The
    # cap is configurable (MAX_TXN_AMOUNT) so it can later be tightened to MTN's
    # real per-transaction limit; the default is just the safe technical ceiling.
    max_amount = current_app.config.get("MAX_TXN_AMOUNT", (1 << 63) - 1)
    if amount > max_amount:
        raise OrchestratorError(f"amount exceeds the maximum of {max_amount}")
    if currency != "UGX":
        raise OrchestratorError("demo only supports UGX")
    if not merchant.is_active:
        raise OrchestratorError("merchant is not active")

    # A simulated rail must never settle live money. The mock flips a charge to
    # SUCCEEDED on a timer with nothing collected, which credits the merchant's
    # ledger with funds nobody paid and — for a vending order — dispenses a real
    # product for free. Airtel and Card have no real adapter yet, so in
    # production they are refused here rather than at the machine.
    #
    # Checked BEFORE anything is written, so a rejected channel leaves no
    # transaction and no ledger entry behind (same rule as payouts).
    if _simulated_rail_forbidden(channel):
        raise OrchestratorError(
            f"{channel.value} is not available for live payments yet"
        )

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
    # COMMIT, not flush: the record must be durable BEFORE any outbound rail
    # call. A crash or timeout mid-call used to roll back the only record of
    # a charge MTN may have accepted (the customer still gets the prompt).
    db.session.commit()

    # Initiate at the rail. The rail will (asynchronously) call back with success/fail.
    adapter = get_adapter(channel)
    try:
        result = adapter.initiate(txn)
    except Exception as exc:
        from .rails_mtn_disbursement import AmbiguousRailError
        if isinstance(exc, AmbiguousRailError):
            # Network failed DURING requesttopay — MTN may have accepted it.
            # The reference is already committed on the txn (the adapter does
            # that before the wire call); park as AUTHORIZED and let the
            # inbound callback or the hourly stale sweep resolve it from
            # MTN's own answer.
            txn.status = TxnStatus.AUTHORIZED
            db.session.commit()
            current_app.logger.error(
                "AMBIGUOUS charge %s (ref %s): network failed mid-requesttopay — "
                "parked AUTHORIZED for callback/sweep",
                txn.public_id, txn.rail_reference,
            )
            return txn
        # Clean pre-send failure (token fetch etc.) — the txn record survives
        # (already committed) but nothing was sent: mark failed.
        db.session.rollback()
        txn = db.session.get(Transaction, txn.id)
        txn.status = TxnStatus.FAILED
        txn.failure_reason = f"rail_error: {exc}"
        db.session.commit()
        return txn

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
    # Row-lock BEFORE the terminal check. Two drivers race for the same
    # transaction (the inbound rail webhook and the Celery poller, plus the
    # stale sweep) — an unlocked read-then-act let both see AUTHORIZED and
    # both post the full collection credit. FOR UPDATE serialises them; the
    # loser re-reads a terminal status and returns. No-op on SQLite (local
    # dev is effectively single-writer), same precedent as
    # ledger.lock_account_for_update.
    db.session.refresh(txn, with_for_update=True)
    if txn.status in (TxnStatus.SUCCEEDED, TxnStatus.FAILED):
        # Idempotent — another driver already processed the callback.
        db.session.commit()   # release the lock
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
        # Post the settlement: rail_clearing receives funds, merchant gets
        # amount-fee, psp_revenue gets fee.
        merchant = db.session.get(Merchant, txn.merchant_id)
        instant = bool(getattr(merchant, "instant_settlement", False))
        # Sandbox charges post to the sandbox ledger only — a test key can never
        # move a balance the merchant could withdraw.
        mode = bool(txn.is_test)
        rail_acct = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING,
            merchant_id=None,
            currency=txn.currency,
            is_test=mode,
        )
        # Instant-settlement merchants (e.g. our own products like KarlPOS) skip the
        # 24h hold — funds land directly in MERCHANT_AVAILABLE and are withdrawable now.
        dest_type = AccountType.MERCHANT_AVAILABLE if instant else AccountType.MERCHANT_PENDING
        merch_dest = ledger.get_or_create_account(
            type=dest_type,
            merchant_id=txn.merchant_id,
            currency=txn.currency,
            is_test=mode,
        )
        revenue = ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE,
            merchant_id=None,
            currency=txn.currency,
            is_test=mode,
        )
        ledger.post(
            [
                (rail_acct, +txn.amount),
                (merch_dest, -(txn.amount - txn.fee_amount)),
                (revenue, -txn.fee_amount),
            ],
            currency=txn.currency,
            transaction_id=txn.id,
            memo=f"charge {txn.public_id} succeeded"
                 + (" (instant settle)" if instant else ""),
        )
        txn.status = TxnStatus.SUCCEEDED
        if instant:
            # Already in available; mark settled so the sweep never re-moves it.
            txn.settled_at = datetime.now(timezone.utc)
    else:
        txn.status = TxnStatus.FAILED
        txn.failure_reason = reason or "unknown"

    txn.completed_at = datetime.now(timezone.utc)
    db.session.commit()

    # Payment -> dispense bridge. Only fires for vending orders, and only after
    # the money is committed above. It never raises: a supplier outage must not
    # undo a payment we have already taken and recorded.
    if success:
        from .vending import maybe_dispense_on_success
        maybe_dispense_on_success(txn)
        _maybe_mark_bill_paid(txn)

    _queue_webhook(txn)


def _maybe_mark_bill_paid(txn: Transaction) -> None:
    """Flip a bill to paid when its charge actually SUCCEEDS. Never raises.

    Bills used to be marked paid the moment the MoMo prompt was SENT — a
    cancelled/ignored prompt left the bill permanently "paid" with no money
    and no way to retry (the pay routes 404 any non-active bill). The bill's
    merchant_reference is "<bill_public_id>|<account_ref>".
    """
    try:
        ref = txn.merchant_reference or ""
        if not ref.startswith("bill_"):
            return
        bill_public_id = ref.split("|", 1)[0]
        from ..models import Bill
        bill = Bill.query.filter_by(public_id=bill_public_id,
                                    merchant_id=txn.merchant_id).one_or_none()
        if bill is not None and bill.status == "active":
            bill.status = "paid"
            bill.transaction_id = txn.id
            db.session.commit()
    except Exception:  # a bill bookkeeping failure must never disturb the
        # committed money above — but it must ROLL BACK (a dirty session
        # poisons everything after us in this request) and be VISIBLE.
        db.session.rollback()
        try:
            current_app.logger.exception(
                "failed to mark bill paid for txn %s — bill remains active",
                txn.public_id,
            )
        except Exception:
            pass


def _queue_webhook(txn: Transaction) -> None:
    # Route through the ONE enqueue path (signing, event id + timestamp
    # envelope, immediate best-effort delivery + the beat-sweep fallback) and
    # build the charge data from the SINGLE shared helper — this used to
    # hand-build both the delivery row and the charge dict, duplicating logic
    # that had already drifted from the gift-card checkout's copy.
    from .webhooks import charge_event_data, enqueue
    merchant = db.session.get(Merchant, txn.merchant_id)
    if not merchant or not merchant.webhook_url:
        return
    enqueue(merchant, f"charge.{txn.status.value}", charge_event_data(txn),
            transaction_id=txn.id)
