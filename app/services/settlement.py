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

    # Collect the distinct merchant/currency/MODE groups that have anything due.
    # Mode is part of the key: sandbox and live are separate ledgers, and a sweep
    # that mixed them would move sandbox money into a withdrawable balance.
    pairs = (
        db.session.query(Transaction.merchant_id, Transaction.currency,
                         Transaction.is_test)
        .filter(
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.settled_at.is_(None),
            Transaction.completed_at <= cutoff,
        )
        .distinct()
        .all()
    )

    moved = {}
    for merchant_id, currency, is_test in pairs:
        try:
            merchant_moved = _settle_one_merchant(
                merchant_id=merchant_id,
                currency=currency,
                is_test=bool(is_test),
                cutoff=cutoff,
                batch_size=batch_size,
            )
            if merchant_moved:
                # Accumulate: one merchant can now appear twice (live + sandbox).
                moved[merchant_id] = moved.get(merchant_id, 0) + merchant_moved
            db.session.commit()   # commit per merchant — bounded lock scope
        except Exception:
            db.session.rollback()
            # Keep going; one bad merchant must not stall settlement for the rest.
            from flask import current_app
            current_app.logger.exception(
                "settlement sweep failed for merchant %s", merchant_id
            )

    # Second pass: split-charge shares. A split txn carries settled_at from the
    # moment it succeeds (so the pass above skips it); its money lives in
    # SplitAllocation rows that settle per-share here, after the same hold.
    from .splits import sweep_split_allocations
    for merchant_id, amount in sweep_split_allocations(
            cutoff=cutoff, batch_size=batch_size).items():
        moved[merchant_id] = moved.get(merchant_id, 0) + amount
    return moved


def _settle_one_merchant(*, merchant_id, currency, is_test, cutoff, batch_size) -> int:
    """Settle all due transactions for one merchant, in ONE ledger. Caller commits."""
    due = (
        Transaction.query.filter(
            Transaction.merchant_id == merchant_id,
            Transaction.currency == currency,
            Transaction.is_test.is_(True) if is_test else Transaction.is_test.is_(False),
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.settled_at.is_(None),
            Transaction.completed_at <= cutoff,
        )
        .order_by(Transaction.completed_at)
        .limit(batch_size)
        # Two sweeps can now run concurrently (hourly beat in the worker +
        # the wallet's manual button in the web process). FOR UPDATE SKIP
        # LOCKED makes each due txn settle exactly once; without it both runs
        # read the same due set and both posted pending->available — a
        # double-credit that still summed to zero, so reconciliation passed.
        # (SQLite ignores FOR UPDATE — dev is single-writer.)
        .with_for_update(skip_locked=True)
        .all()
    )
    if not due:
        return 0

    pending = ledger.get_or_create_account(
        type=AccountType.MERCHANT_PENDING, merchant_id=merchant_id,
        currency=currency, is_test=is_test,
    )
    available = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id,
        currency=currency, is_test=is_test,
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
