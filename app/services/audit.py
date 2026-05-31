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
    """Write one audit entry. Safe to call mid-request; flushes with the session."""
    entry = AuditLog(
        event=event,
        merchant_id=merchant_id,
        actor_ip=_client_ip(),
        resource_id=resource_id,
        detail=json.dumps(detail) if detail else None,
    )
    db.session.add(entry)


def _client_ip() -> str:
    """Best-effort client IP — respects X-Forwarded-For behind a proxy."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"
