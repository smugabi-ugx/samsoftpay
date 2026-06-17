"""Double-entry ledger.

Rules enforced here (kept simple but real):

1. Every posting is a list of (account, signed_amount) tuples that sum to zero
   per currency. The `post()` function refuses otherwise.
2. Every posting is grouped under a single `journal_id` (uuid) so the pair can
   be queried/audited as a unit.
3. Entries are append-only. To "correct" a posting, write a reversing one.
4. We update a cached_balance on Account for fast reads, but the journal is
   the source of truth — `recompute_balance(account)` proves it.

Typical postings:

Charge initiated (customer pays via MoMo, amount A, fee F):
    DR rail_clearing[MNO]            +A      (we now expect A from the MNO)
    CR merchant_pending              -(A-F)  (merchant earns A-F)
    CR psp_revenue                   -F      (we earn F)
                                     ────
                                      0

Rail settles to us (MNO pays us out):
    CR rail_clearing[MNO]            -A
    DR merchant_pending              +0      (no — merchant_pending stays as-is here)
    ...actually: when funds clear from rail to our settlement account we'd
    move them from rail_clearing to psp_float. For demo purposes we treat
    "rail success" as the same moment funds clear.

Settle to merchant bank (T+1 payout):
    CR merchant_available            -X
    DR psp_float                     +X
"""
import uuid
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Account, AccountType, JournalEntry


class LedgerError(Exception):
    pass


def get_or_create_account(
    *, type: AccountType, merchant_id: int | None = None, currency: str = "UGX"
) -> Account:
    """Race-safe get-or-create.

    Under concurrency two requests can both miss the SELECT and try to INSERT the
    same (type, merchant_id, currency), which violates the uq_account UNIQUE
    constraint. We insert inside a SAVEPOINT so a clash rolls back ONLY the insert
    (not the caller's whole transaction) and then re-read the winning row.
    """
    acct = (
        Account.query.filter_by(type=type, merchant_id=merchant_id, currency=currency)
        .one_or_none()
    )
    if acct is not None:
        return acct
    try:
        with db.session.begin_nested():
            acct = Account(type=type, merchant_id=merchant_id, currency=currency)
            db.session.add(acct)
            db.session.flush()
        return acct
    except IntegrityError:
        # Another request created it first — use theirs.
        return Account.query.filter_by(
            type=type, merchant_id=merchant_id, currency=currency
        ).one()


def lock_account_for_update(account: Account) -> Account:
    """Take a row-level lock on the account and refresh its cached_balance.

    Issues SELECT ... FOR UPDATE (on PostgreSQL) so concurrent debits of the same
    balance serialise — this is what prevents double-spend / overdraft. The lock is
    held until the surrounding transaction commits or rolls back. On SQLite (local
    dev) FOR UPDATE is a no-op, which is fine since local dev is single-threaded.
    """
    db.session.refresh(account, with_for_update=True)
    return account


def post(
    entries: Iterable[tuple[Account, int]],
    *,
    currency: str,
    transaction_id: int | None = None,
    memo: str | None = None,
) -> str:
    """Write a balanced journal. Returns the journal_id.

    entries: iterable of (Account, signed_amount) — sum MUST be zero.
    """
    entries = list(entries)
    if not entries:
        raise LedgerError("empty posting")
    total = sum(amt for _, amt in entries)
    if total != 0:
        raise LedgerError(f"unbalanced posting: sum={total} (must be 0)")

    journal_id = str(uuid.uuid4())
    for acct, amt in entries:
        if acct.currency != currency:
            raise LedgerError(
                f"currency mismatch: account {acct.id} is {acct.currency}, posting is {currency}"
            )
        if amt == 0:
            continue  # skip zero legs to keep journal clean
        db.session.add(
            JournalEntry(
                journal_id=journal_id,
                account_id=acct.id,
                amount=amt,
                currency=currency,
                transaction_id=transaction_id,
                memo=memo,
            )
        )
        acct.cached_balance += amt
    return journal_id


def recompute_balance(account: Account) -> int:
    """Sum the journal — proves cached_balance is correct."""
    total = (
        db.session.query(func.coalesce(func.sum(JournalEntry.amount), 0))
        .filter(JournalEntry.account_id == account.id)
        .scalar()
    )
    return int(total)


def assert_balances_match() -> dict:
    """Returns {account_id: (cached, recomputed)} for any mismatches. Empty if all good."""
    mismatches = {}
    for acct in Account.query.all():
        recomputed = recompute_balance(acct)
        if recomputed != acct.cached_balance:
            mismatches[acct.id] = (acct.cached_balance, recomputed)
    return mismatches
