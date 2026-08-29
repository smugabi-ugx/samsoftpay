"""Celery beat tasks: operational monitoring and alerting.

Two jobs the rest of the schedule doesn't cover:

  - `heartbeat` — writes a fresh liveness timestamp to Redis every minute. It
    runs ON a worker, SCHEDULED by beat, so a fresh timestamp proves the whole
    worker+beat pipeline is alive. The WEB process reads it at /ops/status; an
    external uptime monitor hitting that URL is what actually detects a dead
    worker (a dead worker can never alert about itself).

  - `check_money_stuck` — hourly scan for money that is stuck in a state no
    automatic resolver will clear, and alert a human:
      * stranded payouts (money in SUSPENSE, no rail_reference, needs a manual
        `flask reverse-payout`), and
      * charges stuck AUTHORIZED far longer than the pollers/stale-sweep should
        ever leave them (MTN never gave a final answer).
    Both are conditions the auto-resolvers are designed to AVOID touching, so
    they accumulate silently without this.
"""
from ..celery_app import celery


@celery.task(name="app.tasks.monitoring.heartbeat")
def heartbeat() -> dict:
    """Record a worker+beat liveness timestamp. Runs every minute."""
    from ..services.alerts import record_heartbeat
    record_heartbeat()
    return {"ok": True}


# A charge that is still AUTHORIZED this long after creation means every poller
# and the hourly stale-sweep have had many chances to resolve it from MTN's own
# answer and could not. That is a real "is money stuck?" signal, not normal lag.
_AUTHORIZED_ALERT_HOURS = 6


@celery.task(name="app.tasks.monitoring.check_money_stuck")
def check_money_stuck() -> dict:
    """Hourly: alert on money stuck in a state no auto-resolver will clear."""
    from datetime import timedelta

    from ..models import (
        Account, AccountType, PaymentLink, Payout, PayoutStatus, Transaction,
        TxnStatus, utcnow,
    )
    from ..services.alerts import send_alert

    problems = []

    # 0. Un-wedge idempotency reservations stranded IN_FLIGHT by a crashed request
    #    (the holder died between reserve() and store()). Without this they 409
    #    'still in flight' until the 30-day prune; reap_stale_inflight resolves them
    #    to a terminal 'verify/retry-with-new-key' response (never re-executes, so
    #    zero double-spend risk). Best-effort — never break the money-stuck scan.
    try:
        from ..services import idempotency
        idempotency.reap_stale_inflight()
    except Exception:
        from ..extensions import db as _db
        _db.session.rollback()

    # 1. Stranded payouts: PENDING with no rail_reference — money earmarked into
    #    SUSPENSE for a payout that never reached a rail. `flask reverse-payout`.
    stranded = (Payout.query
                .filter(Payout.status == PayoutStatus.PENDING,
                        Payout.rail_reference.is_(None))
                .all())
    if stranded:
        total = sum(p.amount for p in stranded)
        ids = ", ".join(p.public_id for p in stranded[:10])
        problems.append(
            f"{len(stranded)} stranded payout(s) holding {total} in SUSPENSE "
            f"(no rail reference). Investigate then `flask reverse-payout <id>`. "
            f"ids: {ids}")

    # 2. Charges stuck AUTHORIZED well past when a resolver should have cleared them.
    cutoff = utcnow() - timedelta(hours=_AUTHORIZED_ALERT_HOURS)
    stuck = (Transaction.query
             .filter(Transaction.status == TxnStatus.AUTHORIZED,
                     Transaction.created_at <= cutoff)
             .all())
    if stuck:
        total = sum(t.amount for t in stuck)
        ids = ", ".join(t.public_id for t in stuck[:10])
        problems.append(
            f"{len(stuck)} charge(s) stuck AUTHORIZED > {_AUTHORIZED_ALERT_HOURS}h "
            f"({total} total) — MTN never returned a final status. "
            f"Check reconciliation. ids: {ids}")

    # 3b. Vending orders stuck DISPENSING: dispense_for_link commits the atomic
    #     claim pending->dispensing and only THEN makes the supplier ApplyExportGoods
    #     HTTP call. If the worker dies in that window (deploy/OOM/SIGKILL) before
    #     _finish runs, the order is stuck 'dispensing' forever — retry_dispense
    #     refuses a DISPENSING order and no sweep clears it, while the charge is
    #     SUCCEEDED (customer paid). A genuine dispense finishes in seconds, so an
    #     order still 'dispensing' an hour after creation is a dead in-flight call.
    #     Do NOT auto re-dispense: the original ApplyExportGoods may already have
    #     reached the machine, so a blind retry risks a double dispense. Alert a
    #     human, who reconciles the §2.2.3 callback and runs `flask reset-vending`.
    dispensing_cutoff = utcnow() - timedelta(hours=1)
    stuck_vend = (PaymentLink.query
                  .filter(PaymentLink.vending_status == "dispensing",
                          PaymentLink.created_at <= dispensing_cutoff)
                  .all())
    if stuck_vend:
        ids = ", ".join(v.public_id for v in stuck_vend[:10])
        problems.append(
            f"{len(stuck_vend)} vending order(s) stuck DISPENSING > 1h — a worker "
            f"likely died between claim and supplier confirmation. The customer PAID. "
            f"Do NOT blind-retry (double-dispense risk); reconcile then "
            f"`flask reset-vending <id>`. ids: {ids}")

    # 3. Any LIVE suspense balance is worth a warning even if not obviously stranded
    #    (money in flight is normal; money PARKED here is the leak signature).
    live_suspense = (Account.query
                     .filter(Account.type == AccountType.SUSPENSE,
                             Account.is_test.is_(False))
                     .all())
    held = sum(-a.cached_balance for a in live_suspense if a.cached_balance)

    # 4. Webhook deliveries that EXHAUSTED all 8 attempts are a permanent
    #    dead-letter: a platform that finalizes state on the webhook (e.g. KarlPOS)
    #    silently diverges from us, and the retry sweep (attempts < 8) never
    #    re-drives them. Nothing else surfaces this — alert so a human resends or
    #    fixes the endpoint before the ledgers drift unnoticed.
    from ..models import WebhookDelivery
    dead = (WebhookDelivery.query
            .filter(WebhookDelivery.status == "failed",
                    WebhookDelivery.attempts >= 8,
                    WebhookDelivery.created_at >= utcnow() - timedelta(days=2))
            .all())
    if dead:
        m_ids = {d.merchant_id for d in dead}
        problems.append(
            f"{len(dead)} webhook deliver(y/ies) permanently FAILED (8/8 attempts) in "
            f"the last 48h across {len(m_ids)} merchant(s) — their charge/payout/vending "
            f"outcomes never arrived. Check the endpoint(s) and resend.")

    if problems:
        send_alert(
            "Money stuck",
            "\n".join(problems) + f"\n\nTotal LIVE suspense held: {held}.",
            severity="critical",
            key="money-stuck",
            dedupe_seconds=3600,
        )
        print(f"money-stuck: {len(problems)} problem(s) alerted")
    else:
        print("money-stuck: clear")

    return {"ok": not problems, "problems": problems, "live_suspense_held": held}


@celery.task(name="app.tasks.monitoring.payout_anomaly_scan")
def payout_anomaly_scan() -> dict:
    """Every 10 min: rolling-sum payout anomaly detection (Flutterwave ₦11B /
    Pegasus weekend-raid class). Pages on findings; auto-freezes on the
    platform panic threshold if armed. Per-payout checks cannot see a
    sub-threshold drip — only aggregates can."""
    from ..services.anomaly import scan_payout_anomalies
    findings = scan_payout_anomalies()
    if findings:
        print(f"payout-anomaly: {len(findings)} finding(s) alerted")
    else:
        print("payout-anomaly: clear")
    return {"ok": not findings, "findings": findings}


@celery.task(name="app.tasks.monitoring.charge_anomaly_scan")
def charge_anomaly_scan() -> dict:
    """Every 10 min: charge-side fraud/abuse detection — failed-charge storms
    (leaked-key probing / card testing), velocity spikes, large single charges.
    The payout/refund scans watch money OUT; this watches money IN. Persists to
    the admin anomaly feed and pages, deduped."""
    from ..services.anomaly import scan_charge_anomalies
    findings = scan_charge_anomalies()
    if findings:
        print(f"charge-anomaly: {len(findings)} finding(s) recorded/alerted")
    else:
        print("charge-anomaly: clear")
    return {"ok": not findings, "findings": findings}


@celery.task(name="app.tasks.monitoring.refund_outlier_scan")
def refund_outlier_scan() -> dict:
    """Daily: refunds-vs-charges outlier report (Interswitch lesson — the
    refund path watched as closely as the payout path, from day one)."""
    from ..services.anomaly import scan_refund_outliers
    findings = scan_refund_outliers()
    if findings:
        print(f"refund-outliers: {len(findings)} merchant(s) flagged")
    else:
        print("refund-outliers: clear")
    return {"ok": not findings, "findings": findings}
