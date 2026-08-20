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

    # Page each non-panic finding (panic already paged above), deduped hourly.
    for f in findings:
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
            send_alert(
                "Refund outlier: merchant refunds outsized vs charges",
                str(f),
                severity="critical",
                key=f"refund-outlier-{mid}",
                dedupe_seconds=86400,
            )
    return findings
