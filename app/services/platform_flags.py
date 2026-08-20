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
