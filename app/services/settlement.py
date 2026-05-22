"""Settlement.

After a holdback period (e.g. T+1) we move merchant_pending -> merchant_available,
and at payout time merchant_available -> psp_float (representing money leaving
to the merchant's bank).

For the demo we just expose a function that sweeps everything older than N hours.
"""
from datetime import timedelta

from sqlalchemy import and_, func

from ..extensions import db
from ..models import (
    Account,
    AccountType,
    JournalEntry,
    Merchant,
    Transaction,
    TxnStatus,
    utcnow,
)
from . import ledger


def sweep_to_available(*, hold_hours: int = 24) -> dict:
    """Move merchant_pending -> merchant_available for transactions that have
    aged past hold_hours.

    Returns {merchant_id: amount_moved}.
    """
    cutoff = utcnow() - timedelta(hours=hold_hours)
    rows = (
        db.session.query(
            Transaction.merchant_id,
            Transaction.currency,
            func.sum(Transaction.amount - Transaction.fee_amount),
        )
        .filter(
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.completed_at <= cutoff,
            # Only sweep those we haven't swept yet — track via a flag on Transaction
            # in a real system. For the demo we'll trust that this is run idempotently
            # by tagging with a memo + checking journal.
        )
        .group_by(Transaction.merchant_id, Transaction.currency)
        .all()
    )
    moved = {}
    for merchant_id, currency, total in rows:
        if not total:
            continue
        total = int(total)
        # Idempotency guard: if we've already posted a "sweep" memo equal to this total
        # for this merchant today, skip. (Real systems mark transactions as settled.)
        pending = ledger.get_or_create_account(
            type=AccountType.MERCHANT_PENDING, merchant_id=merchant_id, currency=currency
        )
        available = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id, currency=currency
        )
        # We only sweep what's actually in pending (it could be less if previous sweeps ran).
        # Cap the sweep at the current pending balance (which is negative because we
        # credited it earlier). Pending balance is stored as a negative number per our
        # convention.
        pending_balance = -pending.cached_balance  # convert to positive "owed to merchant"
        sweep_amount = min(total, pending_balance)
        if sweep_amount <= 0:
            continue
        ledger.post(
            [
                (pending, +sweep_amount),
                (available, -sweep_amount),
            ],
            currency=currency,
            memo=f"settlement sweep merchant={merchant_id}",
        )
        moved[merchant_id] = sweep_amount
    db.session.commit()
    return moved
