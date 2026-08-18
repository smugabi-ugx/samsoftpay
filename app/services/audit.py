"""Audit logging — append-only trail of every sensitive action."""
import json

from flask import has_request_context, request

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
        # An audit write failing must not break the money action — but a
        # money action vanishing from the audit trail must not be silent.
        try:
            from flask import current_app
            current_app.logger.warning("audit write failed for event %s", event)
        except Exception:
            pass


def _client_ip() -> str:
    # Also called from Celery tasks (settlement, polling, auto-dispense) where
    # there is no request context at all — those entries are still worth writing.
    if not has_request_context():
        return "worker"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"
