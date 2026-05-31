"""Audit logging — append-only trail of every sensitive action."""
import json

from flask import request

from ..extensions import db
from ..models import AuditLog


def log_event(
    event: str,
    *,
    merchant_id: int | None = None,
    resource_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Write one audit entry and commit immediately.

    Uses its own try/except so an audit write failure never breaks the
    request, and so entries are persisted even when the caller aborts().
    """
    entry = AuditLog(
        event=event,
        merchant_id=merchant_id,
        actor_ip=_client_ip(),
        resource_id=resource_id,
        detail=json.dumps(detail) if detail else None,
    )
    try:
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"
