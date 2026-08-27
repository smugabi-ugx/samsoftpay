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


DEFAULT_HOLD_MINUTES = 30   # the "clear" window when the admin hasn't set one


def get_hold_minutes() -> int:
    """The settlement hold, in minutes — admin-configurable at runtime via the
    `settlement_hold_minutes` platform flag (no deploy). Defaults to 30 and fails
    SAFE to the default on any missing/bad value or a DB read error, so settlement
    can never be broken by a malformed setting. Bounded 0 min .. 30 days."""
    try:
        from .platform_flags import get_flag, SETTLEMENT_HOLD_MINUTES
        raw = get_flag(SETTLEMENT_HOLD_MINUTES)
        v = int(raw)
        if 0 <= v <= 43_200:
            return v
    except (TypeError, ValueError, Exception):
        pass
    return DEFAULT_HOLD_MINUTES


def sweep_to_available(*, hold_minutes: int | None = None,
                       hold_hours: int | None = None, batch_size: int = 500) -> dict:
    """Move merchant_pending -> merchant_available for transactions whose own hold
    period has elapsed.

    Each transaction is settled exactly once (tracked by Transaction.settled_at), so
    money is only released after ITS hold — not swept wholesale because some other
    transaction on the same merchant aged out. Work is committed per merchant so one
    merchant's failure or a long run never holds a table-wide lock.

    The hold defaults to the admin-configured `settlement_hold_minutes` (30 min out
    of the box). An explicit `hold_minutes`/`hold_hours` overrides it (tests + the
    manual sweep button pass one); `hold_hours` is kept for backward compatibility.

    Returns {merchant_id: amount_moved}.
    """
    if hold_minutes is None:
        hold_minutes = hold_hours * 60 if hold_hours is not None else get_hold_minutes()
    cutoff = utcnow() - timedelta(minutes=hold_minutes)

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
        # RELEASE INVARIANT (NIBSS lesson: erroneously-credited money that
        # becomes withdrawable converts an ops fix into multi-year litigation).
        # pending is credit-normal (negative); if this release drove it ABOVE
        # zero we just moved money into `available` that pending never held —
        # a dry posting. Abort THIS merchant's settlement (caller rolls back,
        # nothing releases) and page a human. All other merchants still settle.
        # cached_balance is updated with an atomic SQL increment, so the
        # instance attribute holds an expression until re-read — refresh to
        # get the post-release number from the database.
        db.session.flush()
        db.session.refresh(pending)
        if int(pending.cached_balance) > 0:
            try:
                from .alerts import send_alert
                send_alert(
                    "SETTLEMENT INVARIANT VIOLATION — release aborted",
                    f"merchant={merchant_id} currency={currency} is_test={is_test}: "
                    f"sweep tried to release {total} but merchant_pending went "
                    f"positive ({int(pending.cached_balance)}). Rolled back; "
                    f"investigate before this merchant settles again.",
                    severity="critical",
                    key=f"settle-invariant-{merchant_id}-{currency}-{int(is_test)}",
                )
            except Exception:
                pass
            raise RuntimeError(
                f"settlement invariant violated for merchant {merchant_id}: "
                f"released {total} > pending balance")
    for txn in due:
        # net<=0 txns move no money but are still marked so the sweep skips them next time.
        txn.settled_at = now

    if total > 0:
        # First-class settlement record (Paystack lesson): each release is a
        # row the merchant can see, list via GET /v1/settlements, and set a
        # watch by. Same transaction as the ledger post — they commit or roll
        # back together, so a Settlement row can never exist without its money.
        import uuid as _uuid
        from ..models import Settlement
        db.session.add(Settlement(
            public_id=f"setl_{_uuid.uuid4().hex[:16]}",
            merchant_id=merchant_id, currency=currency, is_test=is_test,
            amount=total, txn_count=len(due), kind="sweep",
        ))
    return total
