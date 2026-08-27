"""Celery beat task: automatic settlement sweep.

Runs every hour and moves merchant_pending -> merchant_available
for transactions older than hold_hours (default 24h).
"""
from ..celery_app import celery


@celery.task(name="app.tasks.sweep.auto_settlement_sweep")
def auto_settlement_sweep() -> None:
    from ..services.settlement import sweep_to_available
    try:
        moved = sweep_to_available()   # uses the admin-configured hold (default 30 min)
        if moved:
            total = sum(moved.values())
            print(f"settlement sweep: moved {total} across {len(moved)} merchant(s)")
    except Exception as exc:
        print(f"settlement sweep error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.prune_old_rows")
def prune_old_rows() -> None:
    """Daily: delete delivered webhook rows and aged idempotency keys.

    Both tables grew unbounded (no retention anywhere). 30 days keeps plenty
    of debugging/replay history; the created_at / (status, next_attempt_at)
    indexes shipped in b5c6d7e8f9a0 make these deletes cheap.
    """
    from datetime import timedelta
    from ..extensions import db
    from ..models import IdempotencyKey, WebhookDelivery, utcnow

    cutoff = utcnow() - timedelta(days=30)
    try:
        sent = (db.session.query(WebhookDelivery)
                .filter(WebhookDelivery.status == "sent",
                        WebhookDelivery.created_at <= cutoff)
                .delete(synchronize_session=False))
        keys = (db.session.query(IdempotencyKey)
                .filter(IdempotencyKey.created_at <= cutoff)
                .delete(synchronize_session=False))
        db.session.commit()
        if sent or keys:
            print(f"prune: {sent} sent webhooks, {keys} idempotency keys")
    except Exception as exc:
        db.session.rollback()
        print(f"prune error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.resolve_stale_transactions")
def resolve_stale_transactions() -> None:
    """Hourly: resolve PENDING/AUTHORIZED charges older than an hour from
    MTN's own answer (succeed/fail/expire; skip on network error).

    This is the safety net the pollers now rely on: they no longer terminally
    fail a charge/payout at their 90s timeout (MTN can complete at 91s — that
    produced "money taken, no credit" and refund-while-delivered). Something
    must therefore eventually resolve stragglers, and rails_mtn_real.py always
    assumed a beat task did — but none was ever scheduled until now.
    """
    from ..services.sweep import sweep_stale_transactions
    try:
        result = sweep_stale_transactions(stale_minutes=60)
        if result.get("swept"):
            print(f"stale sweep: {result}")
    except Exception as exc:
        print(f"stale sweep error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.resolve_stale_payouts")
def resolve_stale_payouts() -> None:
    """Hourly: resolve payouts stuck AUTHORIZED from MTN's own transfer status.

    The OUTBOUND mirror of resolve_stale_transactions. Payouts previously had NO
    automated straggler net — if the 90s poller was lost, the payout sat
    AUTHORIZED until a human ran `flask stranded-payouts`. For the one flow where
    "money may have left but we don't know" is the worst case, that asymmetry was
    the gap. Skips on network-unknown (never refunds on an unknown outcome).
    """
    from ..services.sweep import sweep_stale_payouts
    try:
        result = sweep_stale_payouts(stale_minutes=60)
        if result.get("swept"):
            print(f"stale payout sweep: {result}")
    except Exception as exc:
        print(f"stale payout sweep error: {exc}")
        raise


@celery.task(name="app.tasks.sweep.complete_pending_topups")
def complete_pending_topups() -> None:
    """Backstop that finishes MoMo top-ups whose payer navigated away.

    Top-up settlement was driven ONLY by the merchant's browser hitting
    topup_status_json. A merchant who paid then closed the QR page left the
    TopUpRequest stuck 'pending' forever (its money reached `available` via the
    24h sweep, but the record lied). This resolver settles + advances the
    status server-side, and is safe because _settle_topup is now idempotent
    against txn.settled_at (the sweep may have settled it first).
    """
    from datetime import datetime, timezone

    from ..extensions import db
    from ..models import PaymentLink, TopUpRequest, Transaction, TxnStatus
    from ..routes.wallet import _settle_topup

    pending = (TopUpRequest.query
               .filter(TopUpRequest.status == "pending",
                       TopUpRequest.method == "momo",
                       TopUpRequest.payment_link_id.isnot(None))
               .limit(500).all())
    done = 0
    for topup in pending:
        try:
            db.session.refresh(topup, with_for_update=True)
            if topup.status != "pending":
                db.session.commit()
                continue
            link = db.session.get(PaymentLink, topup.payment_link_id)
            if not (link and link.transaction_id):
                db.session.commit()
                continue
            txn = db.session.get(Transaction, link.transaction_id)
            if txn and txn.status == TxnStatus.SUCCEEDED:
                _settle_topup(topup.merchant_id, txn, topup.public_id)
                topup.status = "completed"
                topup.transaction_id = txn.id
                topup.processed_at = datetime.now(timezone.utc)
                db.session.commit()
                done += 1
            elif txn and txn.status == TxnStatus.FAILED:
                topup.status = "rejected"
                topup.admin_notes = txn.failure_reason or "Payment failed"
                db.session.commit()
            else:
                db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f"complete_pending_topups error for {topup.public_id}: {exc}")
    if done:
        print(f"complete_pending_topups: completed {done} top-up(s)")
