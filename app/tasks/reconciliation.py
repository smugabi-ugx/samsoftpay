"""Celery beat task: nightly ledger reconciliation.

Runs run_reconciliation() and raises an alarm (error log) if any invariant is
broken — the journal not summing to zero, a cached balance disagreeing with the
journal, or rail successes not matching succeeded transactions. Silent drift in a
money ledger is the worst failure mode; this makes it loud.
"""
from ..celery_app import celery


def _problems(report: dict) -> list:
    """Return a list of human-readable problems found in a reconciliation report."""
    problems = []
    internal = report.get("internal", {})

    for currency, total in internal.get("journal_sum_by_currency", {}).items():
        if int(total) != 0:
            problems.append(f"journal for {currency} sums to {total} (must be 0)")

    mismatches = internal.get("balance_mismatches", {})
    if mismatches:
        problems.append(f"{len(mismatches)} account balance mismatch(es): {mismatches}")

    for channel, info in report.get("external", {}).items():
        if not info.get("match", True):
            problems.append(
                f"{channel}: {info.get('rail_succeeded_events')} rail successes "
                f"vs {info.get('transactions_succeeded')} succeeded txns"
            )
    return problems


@celery.task(name="app.tasks.reconciliation.reconcile_ledger")
def reconcile_ledger() -> dict:
    from flask import current_app
    from ..services.reconciliation import run_reconciliation

    report = run_reconciliation()
    problems = _problems(report)
    if problems:
        current_app.logger.error(
            "LEDGER RECONCILIATION FAILED: %s", "; ".join(problems)
        )
    else:
        current_app.logger.info("ledger reconciliation OK")
    return {"ok": not problems, "problems": problems, "report": report}
