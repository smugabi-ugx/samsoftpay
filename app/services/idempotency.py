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
