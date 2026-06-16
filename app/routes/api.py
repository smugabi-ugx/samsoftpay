"""Public API surface that merchants integrate with.

Auth:         Bearer secret_key in Authorization header.
Idempotency:  Idempotency-Key header required on all POST requests.
Replay guard: X-Timestamp header (Unix seconds) required on POST requests.
              Rejected if timestamp is more than 5 minutes old.
Rate limits:  POST /v1/charges  — 30/min, 200/hr per API key
              POST /v1/payouts  — 10/min, 100/hr per API key
"""
import json
import time

from flask import Blueprint, abort, g, jsonify, request

from ..extensions import db, limiter
from ..models import Channel, Merchant, Transaction
from ..services import idempotency
from ..services.audit import log_event
from ..services.orchestrator import OrchestratorError, create_charge

bp = Blueprint("api", __name__, url_prefix="/v1")

_MAX_TIMESTAMP_SKEW = 300  # seconds — reject requests older than 5 minutes


# ---------- helpers ----------

def _auth() -> Merchant:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        log_event("auth.failed", detail={"reason": "missing_bearer"})
        abort(401, description="missing bearer token")
    token = header[len("Bearer "):].strip()

    # Look up by SHA-256 hash first (keys are stored hashed so a DB leak can't be
    # replayed). Fall back to the legacy plaintext columns so keys created before
    # the hash backfill keep working. Sandbox keys take priority over live.
    from ..models import hash_api_key
    token_hash = hash_api_key(token)

    merchant = Merchant.query.filter_by(test_secret_key_hash=token_hash).one_or_none()
    if merchant:
        g.api_mode = "test"
    else:
        merchant = Merchant.query.filter_by(secret_key_hash=token_hash).one_or_none()
        if merchant:
            g.api_mode = "live"
        else:
            # Legacy plaintext fallback (pre-backfill).
            merchant = Merchant.query.filter_by(test_secret_key=token).one_or_none()
            if merchant:
                g.api_mode = "test"
            else:
                merchant = Merchant.query.filter_by(secret_key=token).one_or_none()
                if merchant:
                    g.api_mode = "live"

    if merchant is None or not merchant.is_active:
        log_event("auth.failed", detail={"reason": "invalid_key"})
        abort(401, description="invalid api key")
    return merchant


def _check_timestamp() -> None:
    """Reject stale or missing X-Timestamp headers to block replay attacks."""
    ts_header = request.headers.get("X-Timestamp")
    if not ts_header:
        abort(400, description=(
            "X-Timestamp header required. "
            "Set it to the current Unix timestamp (seconds). "
            "Requests older than 5 minutes are rejected."
        ))
    try:
        ts = int(ts_header)
    except ValueError:
        abort(400, description="X-Timestamp must be an integer Unix timestamp")
    skew = int(time.time()) - ts
    if skew > _MAX_TIMESTAMP_SKEW:
        abort(400, description=f"request timestamp is {skew}s old — max allowed skew is {_MAX_TIMESTAMP_SKEW}s")
    if skew < -60:
        abort(400, description="request timestamp is too far in the future — check your system clock")


# ---------- error handlers ----------

@bp.errorhandler(400)
@bp.errorhandler(401)
@bp.errorhandler(409)
@bp.errorhandler(429)
def _err(e):
    return jsonify(error=e.description), e.code


# ---------- charges ----------

@bp.post("/charges")
@limiter.limit("30 per minute;200 per hour")
def create_charge_route():
    _check_timestamp()
    merchant = _auth()

    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        abort(400, description="Idempotency-Key header required")

    body = request.get_json(silent=True) or {}
    request_hash = idempotency.hash_body(body)

    existing = idempotency.find(merchant.id, idem_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            abort(409, description="idempotency key reused with different request body")
        return jsonify(json.loads(existing.response_body)), existing.response_status

    try:
        amount = int(body["amount"])
        currency = body.get("currency", "UGX")
        channel = Channel(body["channel"])
        customer = body.get("customer") or {}
    except (KeyError, ValueError, TypeError) as exc:
        abort(400, description=f"invalid request: {exc}")

    try:
        txn = create_charge(
            merchant=merchant,
            amount=amount,
            currency=currency,
            channel=channel,
            customer_phone=customer.get("phone"),
            customer_email=customer.get("email"),
            merchant_reference=body.get("reference"),
        )
    except OrchestratorError as exc:
        body_out = {"error": str(exc)}
        idempotency.store(merchant.id, idem_key, request_hash, 400, body_out)
        log_event("charge.rejected", merchant_id=merchant.id, detail={"reason": str(exc)})
        return jsonify(body_out), 400

    out = {
        "id": txn.public_id,
        "mode": "test" if txn.is_test else "live",
        "status": txn.status.value,
        "amount": txn.amount,
        "fee": txn.fee_amount,
        "currency": txn.currency,
        "channel": txn.channel.value,
        "reference": txn.merchant_reference,
        "rail_reference": txn.rail_reference,
        "created_at": txn.created_at.isoformat() if txn.created_at else None,
    }
    idempotency.store(merchant.id, idem_key, request_hash, 201, out)
    log_event("charge.created", merchant_id=merchant.id, resource_id=txn.public_id,
              detail={"amount": txn.amount, "channel": txn.channel.value, "mode": g.api_mode})
    return jsonify(out), 201


@bp.get("/charges/<public_id>")
def get_charge(public_id: str):
    merchant = _auth()
    txn = Transaction.query.filter_by(public_id=public_id, merchant_id=merchant.id).one_or_none()
    if txn is None:
        abort(404)
    return jsonify(
        id=txn.public_id,
        mode="test" if txn.is_test else "live",
        status=txn.status.value,
        amount=txn.amount,
        fee=txn.fee_amount,
        currency=txn.currency,
        channel=txn.channel.value,
        reference=txn.merchant_reference,
        rail_reference=txn.rail_reference,
        failure_reason=txn.failure_reason,
        created_at=txn.created_at.isoformat() if txn.created_at else None,
        completed_at=txn.completed_at.isoformat() if txn.completed_at else None,
    )


# ---------- refunds ----------

@bp.post("/charges/<public_id>/refund")
@limiter.limit("10 per minute")
def refund_charge_route(public_id: str):
    from ..services.refunds import RefundError, refund_charge

    merchant = _auth()
    txn = Transaction.query.filter_by(
        public_id=public_id, merchant_id=merchant.id
    ).one_or_none()
    if txn is None:
        abort(404)

    result = refund_charge(txn=txn, merchant=merchant)

    if not result["ok"]:
        log_event("refund.rejected", merchant_id=merchant.id,
                  resource_id=txn.public_id, detail={"reason": result["error"]})
        return jsonify(error=result["error"]), 400

    payout = result["payout"]
    log_event("refund.initiated", merchant_id=merchant.id,
              resource_id=txn.public_id,
              detail={"payout_id": payout.public_id, "amount": payout.amount})
    return jsonify(
        charge_id=txn.public_id,
        status=txn.status.value,
        refund=dict(
            id=payout.public_id,
            amount=payout.amount,
            currency=payout.currency,
            recipient_phone=payout.recipient_phone,
            status=payout.status.value,
        ),
    ), 202


# ---------- payouts ----------

@bp.post("/payouts")
@limiter.limit("10 per minute;100 per hour")
def create_payout_route():
    from ..models import Payout
    from ..services.payouts import PayoutError, create_payout

    _check_timestamp()
    merchant = _auth()

    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        abort(400, description="Idempotency-Key header required")

    body = request.get_json(silent=True) or {}
    request_hash = idempotency.hash_body(body)

    existing = idempotency.find(merchant.id, idem_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            abort(409, description="idempotency key reused with different request body")
        return jsonify(json.loads(existing.response_body)), existing.response_status

    try:
        amount = int(body["amount"])
        currency = body.get("currency", "UGX")
        recipient = body.get("recipient") or {}
        recipient_phone = recipient["phone"]
        channel = Channel(body.get("channel", "mtn_momo"))
    except (KeyError, ValueError, TypeError) as exc:
        abort(400, description=f"invalid request: {exc}")

    try:
        payout = create_payout(
            merchant=merchant,
            amount=amount,
            currency=currency,
            recipient_phone=recipient_phone,
            recipient_name=recipient.get("name"),
            channel=channel,
        )
    except PayoutError as exc:
        body_out = {"error": str(exc)}
        idempotency.store(merchant.id, idem_key, request_hash, 400, body_out)
        log_event("payout.rejected", merchant_id=merchant.id, detail={"reason": str(exc)})
        return jsonify(body_out), 400

    out = {
        "id": payout.public_id,
        "mode": "test" if payout.is_test else "live",
        "status": payout.status.value,
        "amount": payout.amount,
        "fee": payout.fee_amount,
        "currency": payout.currency,
        "channel": payout.channel.value,
        "recipient_phone": payout.recipient_phone,
        "rail_reference": payout.rail_reference,
        "created_at": payout.created_at.isoformat() if payout.created_at else None,
    }
    idempotency.store(merchant.id, idem_key, request_hash, 201, out)
    log_event("payout.created", merchant_id=merchant.id, resource_id=payout.public_id,
              detail={"amount": payout.amount, "fee": payout.fee_amount})
    return jsonify(out), 201


@bp.get("/payouts/<public_id>")
def get_payout(public_id: str):
    from ..models import Payout
    merchant = _auth()
    p = Payout.query.filter_by(public_id=public_id, merchant_id=merchant.id).one_or_none()
    if p is None:
        abort(404)
    return jsonify(
        id=p.public_id,
        mode="test" if p.is_test else "live",
        status=p.status.value,
        amount=p.amount,
        fee=p.fee_amount,
        currency=p.currency,
        channel=p.channel.value,
        recipient_phone=p.recipient_phone,
        rail_reference=p.rail_reference,
        failure_reason=p.failure_reason,
        created_at=p.created_at.isoformat() if p.created_at else None,
        completed_at=p.completed_at.isoformat() if p.completed_at else None,
    )


# ---------- payment links ----------

@bp.post("/payment-links")
@limiter.limit("30 per minute")
def create_payment_link():
    import re as _re
    import uuid as _uuid
    from flask import url_for
    from ..models import PaymentLink

    _check_timestamp()
    merchant = _auth()
    body = request.get_json(silent=True) or {}

    try:
        amount = int(body["amount"])
        currency = body.get("currency", "UGX")
    except (KeyError, ValueError, TypeError) as exc:
        abort(400, description=f"invalid request: {exc}")

    if amount <= 0:
        abort(400, description="amount must be positive")

    # Validate redirect URLs — only allow http(s) to prevent stored XSS
    def _safe_url(val):
        if val and not _re.match(r"^https?://", str(val)):
            abort(400, description=f"success_url and cancel_url must start with https://")
        return val or None

    link = PaymentLink(
        public_id=f"lnk_{_uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        currency=currency,
        description=str(body.get("description") or "")[:255] or None,
        reference=str(body.get("reference") or "")[:120] or None,
        success_url=_safe_url(body.get("success_url")),
        cancel_url=_safe_url(body.get("cancel_url")),
        allow_multiple_uses=bool(body.get("allow_multiple_uses", False)),
    )
    db.session.add(link)
    db.session.commit()

    log_event("payment_link.created", merchant_id=merchant.id, resource_id=link.public_id,
              detail={"amount": link.amount})
    return jsonify(
        id=link.public_id,
        amount=link.amount,
        currency=link.currency,
        description=link.description,
        reference=link.reference,
        url=url_for("checkout.checkout_page", public_id=link.public_id, _external=True),
    ), 201


@bp.get("/payment-links/<public_id>")
def get_payment_link(public_id: str):
    from flask import url_for
    from ..models import PaymentLink, Transaction
    merchant = _auth()
    link = PaymentLink.query.filter_by(
        public_id=public_id, merchant_id=merchant.id
    ).one_or_none()
    if link is None:
        abort(404)
    txn_status = None
    if link.transaction_id:
        t = db.session.get(Transaction, link.transaction_id)
        if t:
            txn_status = t.status.value
    return jsonify(
        id=link.public_id,
        amount=link.amount,
        currency=link.currency,
        description=link.description,
        reference=link.reference,
        is_active=link.is_active,
        allow_multiple_uses=link.allow_multiple_uses,
        transaction_status=txn_status,
        url=url_for("checkout.checkout_page", public_id=link.public_id, _external=True),
    )
