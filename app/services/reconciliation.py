"""Reconciliation.

Two checks:

1. INTERNAL CONSISTENCY: ledger journal sum == 0 globally, and cached balances
   match recomputed balances. If they don't, you have a bug somewhere.

2. EXTERNAL CONSISTENCY: for each rail, the set of `succeeded` rail events
   should map 1:1 with transactions in SUCCEEDED state. Anything unmatched is
   a candidate for a suspense entry.
"""
from collections import defaultdict

from sqlalchemy import func

from ..extensions import db
from ..models import Channel, JournalEntry, RailEvent, Transaction, TxnStatus
from . import ledger


def run_reconciliation() -> dict:
    report = {"internal": {}, "external": {}}

    # 1. Internal: journal totals to zero per currency
    by_currency = (
        db.session.query(
            JournalEntry.currency, func.coalesce(func.sum(JournalEntry.amount), 0)
        )
        .group_by(JournalEntry.currency)
        .all()
    )
    report["internal"]["journal_sum_by_currency"] = {c: int(s) for c, s in by_currency}
    report["internal"]["balance_mismatches"] = ledger.assert_balances_match()

    # 2. External: count rail successes vs. txn successes per channel
    for ch in Channel:
        rail_success_count = (
            db.session.query(func.count(RailEvent.id))
            .filter(RailEvent.rail == ch, RailEvent.event_type == "succeeded")
            .scalar()
        )
        txn_success_count = (
            db.session.query(func.count(Transaction.id))
            .filter(Transaction.channel == ch, Transaction.status == TxnStatus.SUCCEEDED)
            .scalar()
        )
        report["external"][ch.value] = {
            "rail_succeeded_events": int(rail_success_count),
            "transactions_succeeded": int(txn_success_count),
            "match": rail_success_count == txn_success_count,
        }

    return report
