"""Public API surface that merchants integrate with.

Auth: Bearer secret_key in Authorization header.
Idempotency: required Idempotency-Key header on POST /v1/charges.
"""
import json

from flask import Blueprint, abort, jsonify, request

from ..extensions import db
from ..models import Channel, Merchant, Transaction
from ..services import idempotency
from ..services.orchestrator import OrchestratorError, create_charge

bp = Blueprint("api", __name__, url_prefix="/v1")


def _auth() -> Merchant:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        abort(401, description="missing bearer token")
    token = header[len("Bearer "):].strip()
    merchant = Merchant.query.filter_by(secret_key=token).one_or_none()
    if merchant is None or not merchant.is_active:
        abort(401, description="invalid api key")
    return merchant


@bp.errorhandler(400)
@bp.errorhandler(401)
@bp.errorhandler(409)
def _err(e):
    return jsonify(error=e.description), e.code


@bp.post("/charges")
def create_charge_route():
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

    # Validate
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
        return jsonify(body_out), 400

    out = {
        "id": txn.public_id,
        "status": txn.status.value,
        "amount": txn.amount,
        "fee": txn.fee_amount,
        "currency": txn.currency,
        "channel": txn.channel.value,
        "reference": txn.merchant_reference,
        "rail_reference": txn.rail_reference,
    }
    idempotency.store(merchant.id, idem_key, request_hash, 201, out)
    return jsonify(out), 201


@bp.get("/charges/<public_id>")
def get_charge(public_id: str):
    merchant = _auth()
    txn = Transaction.query.filter_by(public_id=public_id, merchant_id=merchant.id).one_or_none()
    if txn is None:
        abort(404)
    return jsonify(
        id=txn.public_id,
        status=txn.status.value,
        amount=txn.amount,
        fee=txn.fee_amount,
        currency=txn.currency,
        channel=txn.channel.value,
        reference=txn.merchant_reference,
        rail_reference=txn.rail_reference,
        failure_reason=txn.failure_reason,
        completed_at=txn.completed_at.isoformat() if txn.completed_at else None,
    )


@bp.post("/payouts")
def create_payout_route():
    from ..models import Payout
    from ..services.payouts import PayoutError, create_payout

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
        return jsonify(body_out), 400

    out = {
        "id": payout.public_id,
        "status": payout.status.value,
        "amount": payout.amount,
        "currency": payout.currency,
        "channel": payout.channel.value,
        "recipient_phone": payout.recipient_phone,
        "rail_reference": payout.rail_reference,
    }
    idempotency.store(merchant.id, idem_key, request_hash, 201, out)
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
        status=p.status.value,
        amount=p.amount,
        currency=p.currency,
        channel=p.channel.value,
        recipient_phone=p.recipient_phone,
        rail_reference=p.rail_reference,
        failure_reason=p.failure_reason,
        completed_at=p.completed_at.isoformat() if p.completed_at else None,
    )
