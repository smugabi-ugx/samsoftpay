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


def store(merchant_id: int, key: str, request_hash: str, status: int, body: dict) -> None:
    rec = IdempotencyKey(
        merchant_id=merchant_id,
        key=key,
        request_hash=request_hash,
        response_status=status,
        response_body=json.dumps(body),
    )
    db.session.add(rec)
    db.session.commit()
