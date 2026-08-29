"""Idempotency: same (merchant, key) returns the same response.

Real PSPs scope the key to the endpoint AND the request body hash — if the
body differs for the same key, return 409. We do the same.
"""
import hashlib
import json

from ..extensions import db
from ..models import IdempotencyKey


def hash_body(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def find(merchant_id: int, key: str) -> IdempotencyKey | None:
    return IdempotencyKey.query.filter_by(merchant_id=merchant_id, key=key).one_or_none()


# Sentinel status meaning "reserved, execution in flight".
IN_FLIGHT = 0


def reserve(merchant_id: int, key: str, request_hash: str) -> bool:
    """Claim the key BEFORE executing. Returns False if another request holds it.

    find-then-execute-then-store deduplicated sequential retries but not
    CONCURRENT ones: two requests with the same key both found nothing and
    both executed — a double MoMo prompt, or two real disbursements. The
    reservation makes the uq_idem unique constraint decide a single winner
    before any money moves.
    """
    rec = IdempotencyKey(
        merchant_id=merchant_id,
        key=key,
        request_hash=request_hash,
        response_status=IN_FLIGHT,
        response_body="{}",
    )
    db.session.add(rec)
    try:
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


def release(merchant_id: int, key: str) -> None:
    """Delete an in-flight reservation so the caller's retry can re-reserve.

    Used on outcomes that must NOT be cached under the key (transient rail
    outage, malformed body) — leaving the reservation would 409 every retry
    forever while telling the caller to retry.
    """
    rec = find(merchant_id, key)
    if rec is not None and rec.response_status == IN_FLIGHT:
        db.session.delete(rec)
        db.session.commit()


# A reservation should complete in seconds (the charge/payout commits before
# store() is reached). Anything IN_FLIGHT past this lease is a crashed holder
# (OOM/deploy/SIGKILL between reserve() and store()) — well beyond any legitimate
# in-flight or client-retry window.
LEASE_SECONDS = 900   # 15 minutes


def reap_stale_inflight(lease_seconds: int = LEASE_SECONDS) -> int:
    """Resolve reservations stuck IN_FLIGHT past the lease. Returns how many.

    Without this, a process death between reserve() and store() strands the key
    at IN_FLIGHT and every retry gets '409 still in flight' for the full 30-day
    retention — the replay path is wedged even though the charge itself completed.

    We deliberately do NOT delete-and-re-execute: the original attempt may have
    committed its charge/payout, so re-running under the same key could DOUBLE
    charge. Instead we convert the reservation to a TERMINAL response that tells
    the caller the original may have completed and to verify (or use a new key),
    so the key stops 409-ing forever with zero double-spend risk.
    """
    from datetime import timedelta
    from ..models import utcnow
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    stale = (IdempotencyKey.query
             .filter(IdempotencyKey.response_status == IN_FLIGHT,
                     IdempotencyKey.created_at < cutoff)
             .all())
    for rec in stale:
        rec.response_status = 409
        rec.response_body = json.dumps({
            "error": "idempotency reservation expired — the original request may have "
                     "completed. Verify the result (e.g. GET /v1/charges?reference=…) "
                     "or retry with a NEW Idempotency-Key.",
            "expired": True,
        })
    if stale:
        db.session.commit()
    return len(stale)


def store(merchant_id: int, key: str, request_hash: str, status: int, body: dict) -> None:
    """Record the final response — filling in a reservation when one exists."""
    rec = find(merchant_id, key)
    if rec is not None:
        rec.response_status = status
        rec.response_body = json.dumps(body)
        db.session.commit()
        return
    rec = IdempotencyKey(
        merchant_id=merchant_id,
        key=key,
        request_hash=request_hash,
        response_status=status,
        response_body=json.dumps(body),
    )
    db.session.add(rec)
    try:
        db.session.commit()
    except Exception:
        # uq_idem race — the other writer's response is canonical.
        db.session.rollback()
