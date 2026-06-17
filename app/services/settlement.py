"""Settlement.

After a holdback period (e.g. T+1) we move merchant_pending -> merchant_available,
and at payout time merchant_available -> psp_float (representing money leaving
to the merchant's bank).

For the demo we just expose a function that sweeps everything older than N hours.
"""
from datetime import timedelta

from ..extensions import db
from ..models import (
    AccountType,
    Transaction,
    TxnStatus,
    utcnow,
)
from . import ledger


def sweep_to_available(*, hold_hours: int = 24, batch_size: int = 500) -> dict:
    """Move merchant_pending -> merchant_available for transactions whose own hold
    period has elapsed.

    Each transaction is settled exactly once (tracked by Transaction.settled_at), so
    money is only released after ITS hold — not swept wholesale because some other
    transaction on the same merchant aged out. Work is committed per merchant so one
    merchant's failure or a long run never holds a table-wide lock.

    Returns {merchant_id: amount_moved}.
    """
    cutoff = utcnow() - timedelta(hours=hold_hours)

    # Collect the distinct merchant/currency pairs that have anything due.
    pairs = (
        db.session.query(Transaction.merchant_id, Transaction.currency)
        .filter(
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.settled_at.is_(None),
            Transaction.completed_at <= cutoff,
        )
        .distinct()
        .all()
    )

    moved = {}
    for merchant_id, currency in pairs:
        try:
            merchant_moved = _settle_one_merchant(
                merchant_id=merchant_id,
                currency=currency,
                cutoff=cutoff,
                batch_size=batch_size,
            )
            if merchant_moved:
                moved[merchant_id] = merchant_moved
            db.session.commit()   # commit per merchant — bounded lock scope
        except Exception:
            db.session.rollback()
            # Keep going; one bad merchant must not stall settlement for the rest.
            from flask import current_app
            current_app.logger.exception(
                "settlement sweep failed for merchant %s", merchant_id
            )
    return moved


def _settle_one_merchant(*, merchant_id, currency, cutoff, batch_size) -> int:
    """Settle all due transactions for one merchant. Caller commits."""
    due = (
        Transaction.query.filter(
            Transaction.merchant_id == merchant_id,
            Transaction.currency == currency,
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.settled_at.is_(None),
            Transaction.completed_at <= cutoff,
        )
        .order_by(Transaction.completed_at)
        .limit(batch_size)
        .all()
    )
    if not due:
        return 0

    pending = ledger.get_or_create_account(
        type=AccountType.MERCHANT_PENDING, merchant_id=merchant_id, currency=currency
    )
    available = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id, currency=currency
    )

    now = utcnow()
    total = sum(max(0, int(t.amount - (t.fee_amount or 0))) for t in due)

    # Post the money move FIRST. Only if it succeeds do we mark the transactions
    # settled — so settled_at can never be set without the matching ledger entry.
    # (pending/available are stored as negative credits, hence +total / -total.)
    if total > 0:
        ledger.post(
            [
                (pending, +total),
                (available, -total),
            ],
            currency=currency,
            memo=f"settlement sweep merchant={merchant_id} ({len(due)} txns)",
        )
    for txn in due:
        # net<=0 txns move no money but are still marked so the sweep skips them next time.
        txn.settled_at = now
    return total
