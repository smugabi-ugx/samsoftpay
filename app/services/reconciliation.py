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


# ─────────────────────────────────────────────────────────────────────────────
# EXTERNAL reconciliation against MTN's OWN records (the third leg)
#
# The internal check above only compares our rows to our rows. This asks MTN
# directly, per reference, and records any disagreement as a resolvable
# exception. A later run that finds agreement auto-resolves it (backdated
# reconciliation, the Hyperswitch model shrunk to our reality).
# ─────────────────────────────────────────────────────────────────────────────

def reconcile_against_mtn(*, min_age_minutes: int = 10, window_days: int = 7,
                          limit: int = 500) -> dict:
    """Match live MTN-collections transactions against MTN's own status.

    Only real-money (is_test=False) MTN charges, old enough that MTN has a
    final answer and recent enough to be worth checking. Requires a working
    MTN connection (MOMO_USE_REAL + credentials); in sandbox it runs against
    MTN's sandbox status endpoint, so it is testable now.

    Returns a summary; writes/updates rows in recon_exceptions.
    """
    from datetime import timedelta

    from ..models import ReconException, utcnow
    from .sweep import _query_mtn_status

    now = utcnow()
    checked = matched = new_exc = resolved = skipped_unknown = 0

    txns = (
        Transaction.query.filter(
            Transaction.channel == Channel.MTN_MOMO,
            Transaction.is_test.is_(False),
            Transaction.rail_reference.isnot(None),
            Transaction.created_at <= now - timedelta(minutes=min_age_minutes),
            Transaction.created_at >= now - timedelta(days=window_days),
        )
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .all()
    )

    for txn in txns:
        checked += 1
        mtn_status = _query_mtn_status(txn.rail_reference)   # SUCCESSFUL|FAILED|PENDING|None
        if mtn_status is None:
            # Unknown is NOT a mismatch (same principle as the stale sweep) —
            # never fabricate an exception from a network blip.
            skipped_unknown += 1
            continue

        our = txn.status.value  # succeeded|failed|pending|authorized|refunded
        kind = severity = detail = None

        if mtn_status == "SUCCESSFUL" and our not in ("succeeded", "refunded"):
            kind, severity = "mtn_succeeded_local_not", "critical"
            detail = f"MTN says SUCCESSFUL; we have '{our}' — likely a lost callback (money in our float, unbooked)."
        elif mtn_status == "FAILED" and our == "succeeded":
            kind, severity = "local_succeeded_mtn_failed", "critical"
            detail = "We credited this charge but MTN says FAILED — money booked that MTN never collected."
        # (MTN PENDING, or agreement, is not an exception.)

        if kind is None:
            # Agreement (or a still-legitimately-pending txn) — auto-resolve any
            # open exception for this reference.
            resolved += _auto_resolve(txn.rail_reference)
            matched += 1
            continue

        new_exc += _upsert_exception(
            rail_reference=txn.rail_reference, txn=txn, kind=kind, severity=severity,
            our_status=our, mtn_status=mtn_status, detail=detail,
        )

    # Also flag SUCCEEDED live txns with NO reference at all — unreconcilable.
    unref = (
        Transaction.query.filter(
            Transaction.channel == Channel.MTN_MOMO,
            Transaction.is_test.is_(False),
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.rail_reference.is_(None),
            Transaction.created_at >= now - timedelta(days=window_days),
        ).all()
    )
    for txn in unref:
        new_exc += _upsert_exception(
            rail_reference=f"no-ref:{txn.public_id}", txn=txn,
            kind="succeeded_no_reference", severity="warning",
            our_status="succeeded", mtn_status=None,
            detail="SUCCEEDED with no rail reference — cannot be reconciled against MTN.",
        )

    db.session.commit()
    return {
        "checked": checked, "matched": matched, "new_or_updated_exceptions": new_exc,
        "auto_resolved": resolved, "skipped_unknown": skipped_unknown,
        "open_exceptions": ReconException.query.filter_by(status="open").count(),
    }


def _upsert_exception(*, rail_reference, txn, kind, severity, our_status,
                      mtn_status, detail) -> int:
    """Create or refresh the single open exception for a reference. Returns 1
    if new, 0 if it already existed (refreshed)."""
    from ..models import ReconException, utcnow
    row = ReconException.query.filter_by(rail_reference=rail_reference).first()
    if row is not None:
        row.status = "open"
        row.kind, row.severity = kind, severity
        row.our_status, row.mtn_status = our_status, mtn_status
        row.detail, row.last_seen_at = detail, utcnow()
        row.txn_public_id = txn.public_id if txn else row.txn_public_id
        row.merchant_id = txn.merchant_id if txn else row.merchant_id
        row.our_amount = txn.amount if txn else row.our_amount
        return 0
    db.session.add(ReconException(
        rail_reference=rail_reference,
        txn_public_id=txn.public_id if txn else None,
        merchant_id=txn.merchant_id if txn else None,
        kind=kind, severity=severity, our_status=our_status, mtn_status=mtn_status,
        our_amount=txn.amount if txn else None, detail=detail,
    ))
    return 1


def _auto_resolve(rail_reference: str) -> int:
    """Resolve an open exception whose records now agree. Returns 1 if resolved."""
    from ..models import ReconException, utcnow
    row = ReconException.query.filter_by(rail_reference=rail_reference, status="open").first()
    if row is None:
        return 0
    row.status, row.resolved_at, row.resolved_by = "resolved", utcnow(), "auto"
    return 1
