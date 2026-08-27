"""Operational platform flags — DB-backed switches flippable without a deploy.

The only consumer that matters today is the payout kill switch (Pegasus
lesson: an aggregator breach is timed for when nobody is watching; the
response must be one command, from anywhere). Reads fail SAFE per flag:
if the table is unreachable we do NOT freeze payouts on a read error —
availability incidents must not silently halt legitimate money movement —
but we do log loudly.
"""
from __future__ import annotations

from ..extensions import db
from ..models import PlatformFlag, utcnow

FREEZE_PAYOUTS = "freeze_payouts"
MAINTENANCE_MODE = "maintenance_mode"
# Numeric setting (not a toggle): how long a payment is held in `pending` before
# the sweep releases it to withdrawable `available`. Admin-adjustable, no deploy.
# Read via settlement.get_hold_minutes(); default 30 min. Stored as a string.
SETTLEMENT_HOLD_MINUTES = "settlement_hold_minutes"
# The SANDBOX (is_test) hold — test money clears near-instantly so integrators
# aren't blocked. Read via settlement.get_sandbox_hold_minutes(); default 1 min.
SANDBOX_SETTLEMENT_HOLD_MINUTES = "sandbox_settlement_hold_minutes"

# Whitelisted, admin-togglable flags (the /admin/flags switchboard). `enforced`
# says where the flag actually bites; maintenance_mode is DELIBERATELY
# informational-only (a banner) — it must never block money.
FLAG_META = {
    FREEZE_PAYOUTS: {
        "label": "Freeze payouts",
        "description": "Instantly halts every LIVE payout platform-wide "
                       "(create_payout refuses with zero writes). Test mode "
                       "keeps working. The incident kill switch.",
        "danger": True,
        "enforced": "payouts.create_payout (live only)",
    },
    MAINTENANCE_MODE: {
        "label": "Maintenance banner",
        "description": "Shows a site-wide banner telling users maintenance is "
                       "in progress. Informational only — money keeps moving.",
        "danger": False,
        "enforced": "banner only (never blocks money)",
    },
}


def get_flag_row(key: str):
    """The full PlatformFlag row (or None) — fails safe to None on DB errors."""
    try:
        return db.session.get(PlatformFlag, key)
    except Exception:
        return None


def maintenance_on() -> bool:
    return get_flag(MAINTENANCE_MODE) == "on"


def get_flag(key: str) -> str | None:
    try:
        row = db.session.get(PlatformFlag, key)
        return row.value if row else None
    except Exception:
        from flask import current_app
        current_app.logger.exception("platform_flags read failed for %s", key)
        return None


def set_flag(key: str, value: str, *, updated_by: str | None = None) -> None:
    row = db.session.get(PlatformFlag, key)
    if row is None:
        row = PlatformFlag(key=key, value=value, updated_by=updated_by)
        db.session.add(row)
    else:
        row.value = value
        row.updated_at = utcnow()
        row.updated_by = updated_by
    db.session.commit()


def payouts_frozen() -> bool:
    return get_flag(FREEZE_PAYOUTS) == "on"
