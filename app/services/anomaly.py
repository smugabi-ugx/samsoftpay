"""Aggregate payout anomaly detection.

The Flutterwave April-2024 attackers kept every transfer BELOW the
per-transaction fraud thresholds and drained ₦11B over four days; the Pegasus
(Uganda, 2020) raid was timed for a weekend when nobody was watching.
Per-payout checks cannot see either — only ROLLING SUMS can. This module scans
live payouts for:

  1. merchant-hourly:      one merchant's last-hour payout sum over the cap
  2. destination-daily:    one phone number receiving over the daily cap
                           (across ALL merchants — mule-account concentration)
  3. merchant-baseline:    a merchant's last-24h sum far above their own
                           trailing 30-day daily average (needs 7+ days history)
  4. platform-panic:       the whole platform's last-hour sum over the panic
                           threshold -> AUTO-FREEZES payouts + pages (off by
                           default; set PAYOUT_PLATFORM_HOURLY_PANIC to arm)

Detection PAGES a human (send_alert, deduped); it does not block ordinary
flow. The single deliberate exception is the panic auto-freeze, which is the
weekend guard: a compromised key cannot out-race a threshold that flips the
platform kill switch by itself.

Config (app.config, all in minor units / UGX):
  PAYOUT_MERCHANT_HOURLY_CAP     default 20_000_000
  PAYOUT_DESTINATION_DAILY_CAP   default 10_000_000
  PAYOUT_BASELINE_MULTIPLIER     default 5 (x the 30-day daily average)
  PAYOUT_PLATFORM_HOURLY_PANIC   default 0 (disarmed)
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func as safunc

from ..extensions import db
from ..models import Payout, PayoutStatus, utcnow

_COUNTED = (PayoutStatus.PENDING, PayoutStatus.AUTHORIZED, PayoutStatus.SUCCEEDED)


def _sum(q) -> int:
    return int(q.scalar() or 0)


def scan_payout_anomalies() -> list[dict]:
    """Return a list of findings; alert + (maybe) auto-freeze as side effects."""
    from flask import current_app
    from .alerts import send_alert

    cfg = current_app.config
    now = utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)
    findings: list[dict] = []

    base = (db.session.query(safunc.coalesce(safunc.sum(Payout.amount), 0))
            .filter(Payout.is_test.is_(False),
                    Payout.status.in_(_COUNTED)))

    # 1. per-merchant hourly cap
    cap = int(cfg.get("PAYOUT_MERCHANT_HOURLY_CAP", 20_000_000))
    rows = (db.session.query(Payout.merchant_id,
                             safunc.sum(Payout.amount).label("total"))
            .filter(Payout.is_test.is_(False),
                    Payout.status.in_(_COUNTED),
                    Payout.created_at >= hour_ago)
            .group_by(Payout.merchant_id)
            .having(safunc.sum(Payout.amount) > cap)
            .all())
    for mid, total in rows:
        findings.append({"kind": "merchant_hourly_cap", "merchant_id": mid,
                         "total": int(total), "cap": cap})

    # 2. per-destination daily cap (across merchants — mule concentration)
    dcap = int(cfg.get("PAYOUT_DESTINATION_DAILY_CAP", 10_000_000))
    rows = (db.session.query(Payout.recipient_phone,
                             safunc.sum(Payout.amount).label("total"),
                             safunc.count(Payout.id))
            .filter(Payout.is_test.is_(False),
                    Payout.status.in_(_COUNTED),
                    Payout.created_at >= day_ago)
            .group_by(Payout.recipient_phone)
            .having(safunc.sum(Payout.amount) > dcap)
            .all())
    for phone, total, n in rows:
        findings.append({"kind": "destination_daily_cap", "phone": phone,
                         "total": int(total), "count": int(n), "cap": dcap})

    # 3. merchant 24h sum vs their own trailing 30-day daily average
    mult = int(cfg.get("PAYOUT_BASELINE_MULTIPLIER", 5))
    month_ago = now - timedelta(days=30)
    week_ago = now - timedelta(days=7)
    day_rows = (db.session.query(Payout.merchant_id,
                                 safunc.sum(Payout.amount).label("day_total"))
                .filter(Payout.is_test.is_(False),
                        Payout.status.in_(_COUNTED),
                        Payout.created_at >= day_ago)
                .group_by(Payout.merchant_id).all())
    for mid, day_total in day_rows:
        # History check: only merchants with 7+ days of payout history have a
        # baseline worth trusting (a brand-new merchant's first day is not an
        # anomaly, it's onboarding).
        first = (db.session.query(safunc.min(Payout.created_at))
                 .filter(Payout.merchant_id == mid,
                         Payout.is_test.is_(False)).scalar())
        if first is not None and first.tzinfo is None:
            # SQLite hands back naive datetimes; our clock is aware UTC.
            from datetime import timezone as _tz
            first = first.replace(tzinfo=_tz.utc)
        if first is None or first > week_ago:
            continue
        hist_total = _sum(base.filter(Payout.merchant_id == mid,
                                      Payout.created_at >= month_ago,
                                      Payout.created_at < day_ago))
        hist_days = min(30, max(1, (now - first).days))
        daily_avg = hist_total / hist_days
        if daily_avg > 0 and int(day_total) > mult * daily_avg:
            findings.append({"kind": "merchant_baseline_deviation",
                             "merchant_id": mid, "day_total": int(day_total),
                             "daily_avg": int(daily_avg), "multiplier": mult})

    # 4. platform panic threshold -> auto-freeze
    panic = int(cfg.get("PAYOUT_PLATFORM_HOURLY_PANIC", 0))
    if panic > 0:
        platform_hour = _sum(base.filter(Payout.created_at >= hour_ago))
        # Anti-DoS (auditor finding): ONE merchant burning their own balance
        # must not be able to freeze every other merchant's payouts. A single
        # contributor already trips their merchant_hourly_cap alert above; the
        # platform-wide freeze needs the volume to be broad (>=2 merchants) —
        # the shape a credential-compromise drain actually has.
        contributors = (db.session.query(Payout.merchant_id)
                        .filter(Payout.is_test.is_(False),
                                Payout.status.in_(_COUNTED),
                                Payout.created_at >= hour_ago)
                        .distinct().count())
        if platform_hour > panic and contributors >= 2:
            findings.append({"kind": "platform_panic", "total": platform_hour,
                             "threshold": panic, "action": "auto_freeze"})
            from . import platform_flags
            if not platform_flags.payouts_frozen():
                platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, "on",
                                        updated_by="anomaly-auto-freeze")
                send_alert(
                    "PAYOUTS AUTO-FROZEN — platform hourly volume over panic threshold",
                    f"Last-hour live payout volume {platform_hour} exceeded the "
                    f"panic threshold {panic}. The platform payout kill switch "
                    f"is now ON. Verify legitimacy, then `flask freeze-payouts off`.",
                    severity="critical", key="payout-panic-freeze",
                )

    # Persist + page each finding. Panic was already paged above (auto-freeze);
    # still record it to the admin feed.
    for f in findings:
        subj = "platform" if f["kind"] == "platform_panic" else (
            f.get("phone") or "merchant")
        record_anomaly(
            kind=f["kind"],
            category="platform" if f["kind"] == "platform_panic" else "payout",
            severity="critical", merchant_id=f.get("merchant_id"), subject=subj,
            metric=f.get("total") or f.get("day_total"), detail=str(f))
        if f["kind"] == "platform_panic":
            continue
        send_alert(
            f"Payout anomaly: {f['kind']}",
            str(f),
            severity="critical",
            key=f"payout-anomaly-{f['kind']}-{f.get('merchant_id') or f.get('phone')}",
        )
    return findings


def scan_refund_outliers() -> list[dict]:
    """Daily refunds-vs-charges outlier report (Interswitch lesson: ₦30B left
    through a refund path that was less defended than the payment path, taken
    by insiders over YEARS). Refund invariants are enforced in code; this is
    the watching layer — a merchant (or a compromised dashboard session)
    whose refunds are outsized against their charge volume gets a human's
    eyes the same day, not at year three.

    Config: REFUND_OUTLIER_RATIO (default 0.30 of 24h charge volume),
            REFUND_OUTLIER_MIN (default 100_000 — ignore tiny absolute sums).
    """
    from flask import current_app
    from .alerts import send_alert
    from ..models import Transaction, TxnStatus

    cfg = current_app.config
    ratio = float(cfg.get("REFUND_OUTLIER_RATIO", 0.30))
    floor = int(cfg.get("REFUND_OUTLIER_MIN", 100_000))
    now = utcnow()
    day_ago = now - timedelta(hours=24)
    findings: list[dict] = []

    refunds = (db.session.query(
                   Transaction.merchant_id,
                   safunc.count(Transaction.id),
                   safunc.coalesce(safunc.sum(Transaction.amount), 0))
               .filter(Transaction.is_test.is_(False),
                       Transaction.status == TxnStatus.REFUNDED,
                       Transaction.refunded_at >= day_ago)
               .group_by(Transaction.merchant_id).all())
    for mid, n_ref, ref_sum in refunds:
        ref_sum = int(ref_sum)
        if ref_sum < floor:
            continue
        charge_sum = int(db.session.query(
                             safunc.coalesce(safunc.sum(Transaction.amount), 0))
                         .filter(Transaction.merchant_id == mid,
                                 Transaction.is_test.is_(False),
                                 Transaction.status == TxnStatus.SUCCEEDED,
                                 Transaction.completed_at >= day_ago)
                         .scalar() or 0)
        # Outlier when refunds dwarf same-day charge volume — including the
        # degenerate case of refunding yesterday's money with no sales today.
        if charge_sum == 0 or ref_sum > ratio * charge_sum:
            f = {"kind": "refund_outlier", "merchant_id": mid,
                 "refunds_24h": int(n_ref), "refund_sum_24h": ref_sum,
                 "charge_sum_24h": charge_sum, "ratio_threshold": ratio}
            findings.append(f)
            record_anomaly(kind="refund_outlier", category="refund",
                           severity="critical", merchant_id=mid,
                           subject="merchant", metric=ref_sum, detail=str(f))
            send_alert(
                "Refund outlier: merchant refunds outsized vs charges",
                str(f),
                severity="critical",
                key=f"refund-outlier-{mid}",
                dedupe_seconds=86400,
            )
    return findings


# ── Persisted anomaly feed (the admin-reviewable record + bank audit trail) ──

def record_anomaly(*, kind: str, category: str, severity: str,
                   merchant_id=None, subject: str | None = None,
                   metric: int | None = None, detail: str | None = None) -> bool:
    """Upsert an AnomalyEvent. One OPEN row per (kind, merchant_id, subject):
    a persisting condition refreshes last_seen_at instead of spamming rows.
    Returns True if a NEW row was created. Best-effort — NEVER raises, so a
    scan (which runs inside a task) is never disrupted by a logging failure."""
    try:
        from ..models import AnomalyEvent
        dedupe_key = f"{kind}:{merchant_id or '-'}:{subject or '-'}"
        existing = (db.session.query(AnomalyEvent)
                    .filter(AnomalyEvent.dedupe_key == dedupe_key,
                            AnomalyEvent.status == "open")
                    .first())
        if existing is not None:
            existing.last_seen_at = utcnow()
            if metric is not None:
                existing.metric = int(metric)
            if detail is not None:
                existing.detail = detail[:500]
            db.session.commit()
            return False
        db.session.add(AnomalyEvent(
            kind=kind, category=category, severity=severity,
            merchant_id=merchant_id, subject=subject,
            metric=int(metric) if metric is not None else None,
            detail=(detail or "")[:500], dedupe_key=dedupe_key, status="open"))
        db.session.commit()
        return True
    except Exception:
        from flask import current_app
        try:
            db.session.rollback()
            current_app.logger.exception("record_anomaly failed for %s", kind)
        except Exception:
            pass
        return False


def scan_charge_anomalies() -> list[dict]:
    """Charge-side fraud/abuse detection — the gap the payout/refund scans don't
    cover. Watches a short rolling window of LIVE charges for:

      1. failed_charge_storm        one merchant with a burst of FAILED charges
                                    (a compromised/leaked key being probed, or
                                    card-testing) in the window
      2. failed_charge_storm_phone  one customer phone failing repeatedly across
                                    merchants (card/number testing)
      3. charge_velocity            one merchant's charge COUNT far above normal
                                    volume in the window (scripted abuse)
      4. large_charge               a single live charge over a high-value floor
                                    (worth a human's glance)

    Each finding is persisted (record_anomaly) AND paged (send_alert, deduped).
    Read-only over the ledger; it never blocks a charge.

    Config (app.config):
      CHARGE_ANOMALY_WINDOW_MIN     default 15
      CHARGE_FAILED_STORM_COUNT     default 10   (per merchant, in window)
      CHARGE_FAILED_PHONE_COUNT     default 6    (per phone, in window)
      CHARGE_VELOCITY_COUNT         default 200  (per merchant, in window)
      LARGE_CHARGE_AMOUNT           default 5_000_000  (UGX minor units)
    """
    from flask import current_app
    from .alerts import send_alert
    from ..models import Transaction, TxnStatus

    cfg = current_app.config
    now = utcnow()
    window = now - timedelta(minutes=int(cfg.get("CHARGE_ANOMALY_WINDOW_MIN", 15)))
    storm_n = int(cfg.get("CHARGE_FAILED_STORM_COUNT", 10))
    phone_n = int(cfg.get("CHARGE_FAILED_PHONE_COUNT", 6))
    vel_n = int(cfg.get("CHARGE_VELOCITY_COUNT", 200))
    large = int(cfg.get("LARGE_CHARGE_AMOUNT", 5_000_000))
    findings: list[dict] = []

    live = Transaction.is_test.is_(False)

    # 1. failed-charge storm per merchant
    rows = (db.session.query(Transaction.merchant_id, safunc.count(Transaction.id))
            .filter(live, Transaction.status == TxnStatus.FAILED,
                    Transaction.created_at >= window)
            .group_by(Transaction.merchant_id)
            .having(safunc.count(Transaction.id) >= storm_n).all())
    for mid, n in rows:
        findings.append({"kind": "failed_charge_storm", "merchant_id": mid,
                         "failed": int(n), "threshold": storm_n})
        record_anomaly(kind="failed_charge_storm", category="charge",
                       severity="critical", merchant_id=mid, subject="merchant",
                       metric=int(n),
                       detail=f"{n} failed charges in window (>= {storm_n})")
        send_alert("Charge anomaly: failed-charge storm",
                   f"Merchant {mid}: {n} failed live charges in the window "
                   f"(threshold {storm_n}) — possible leaked key / card testing.",
                   severity="critical", key=f"charge-storm-{mid}")

    # 2. failed-charge storm per destination phone (across merchants)
    rows = (db.session.query(Transaction.customer_phone, safunc.count(Transaction.id))
            .filter(live, Transaction.status == TxnStatus.FAILED,
                    Transaction.customer_phone.isnot(None),
                    Transaction.created_at >= window)
            .group_by(Transaction.customer_phone)
            .having(safunc.count(Transaction.id) >= phone_n).all())
    for phone, n in rows:
        findings.append({"kind": "failed_charge_storm_phone", "phone": phone,
                         "failed": int(n), "threshold": phone_n})
        record_anomaly(kind="failed_charge_storm_phone", category="charge",
                       severity="critical", merchant_id=None, subject=phone,
                       metric=int(n),
                       detail=f"{n} failed charges from one phone (>= {phone_n})")
        send_alert("Charge anomaly: repeated failures from one number",
                   f"Phone {phone}: {n} failed live charges in the window "
                   f"(threshold {phone_n}) — possible card/number testing.",
                   severity="critical", key=f"charge-phone-{phone}")

    # 3. charge-count velocity per merchant
    rows = (db.session.query(Transaction.merchant_id, safunc.count(Transaction.id))
            .filter(live, Transaction.created_at >= window)
            .group_by(Transaction.merchant_id)
            .having(safunc.count(Transaction.id) >= vel_n).all())
    for mid, n in rows:
        findings.append({"kind": "charge_velocity", "merchant_id": mid,
                         "count": int(n), "threshold": vel_n})
        record_anomaly(kind="charge_velocity", category="charge",
                       severity="warning", merchant_id=mid, subject="merchant",
                       metric=int(n),
                       detail=f"{n} charges in window (>= {vel_n})")
        send_alert("Charge anomaly: velocity spike",
                   f"Merchant {mid}: {n} live charges in the window "
                   f"(threshold {vel_n}).", severity="warning",
                   key=f"charge-velocity-{mid}")

    # 4. large single charge
    rows = (db.session.query(Transaction)
            .filter(live, Transaction.amount >= large,
                    Transaction.created_at >= window).all())
    for txn in rows:
        findings.append({"kind": "large_charge", "merchant_id": txn.merchant_id,
                         "amount": int(txn.amount), "txn": txn.public_id,
                         "floor": large})
        record_anomaly(kind="large_charge", category="charge", severity="warning",
                       merchant_id=txn.merchant_id, subject=txn.public_id,
                       metric=int(txn.amount),
                       detail=f"single charge {txn.amount} (>= {large})")
        send_alert("Charge anomaly: large single charge",
                   f"Merchant {txn.merchant_id}: charge {txn.public_id} of "
                   f"{txn.amount} (floor {large}).", severity="warning",
                   key=f"large-charge-{txn.public_id}")

    return findings
