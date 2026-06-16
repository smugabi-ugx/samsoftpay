"""Inbound webhook callbacks from payment rails.

For real MTN MoMo Collections, the callback URL you configure on
developer.mtn.com hits this endpoint. We:
  1. Verify the X-Samsoftpay-Signature HMAC (I5 — previously missing)
  2. Look up the transaction by rail_reference
  3. Call orchestrator.complete_transaction

In mock mode, the timer in rails.py calls complete_transaction directly.
This route handles real-rail callbacks and also acts as the loopback URL
for the demo merchant's webhook_url.
"""
import os
import hmac
import hashlib

from flask import Blueprint, abort, current_app, jsonify, request

from ..extensions import db
from ..models import Channel, Transaction
from ..services.orchestrator import complete_transaction

bp = Blueprint("inbound", __name__, url_prefix="/inbound")


def _is_placeholder_secret(secret: str) -> bool:
    return (not secret
            or secret.startswith("whsec_demo")
            or secret == "whsec_change_me_in_production")


def _verify_signature(payload: bytes) -> bool:
    """Verify X-Samsoftpay-Signature (HMAC-SHA256). Fail CLOSED.

    A rail callback marks a transaction succeeded, which moves real money in the
    ledger. We therefore reject unless a valid signature is present. In production
    (RENDER set) an unconfigured/placeholder secret is a hard failure — never a skip.
    """
    secret = current_app.config.get("WEBHOOK_SIGNING_SECRET", "")
    if _is_placeholder_secret(secret):
        if os.environ.get("RENDER"):
            # Should never happen — _assert_production_env blocks boot — but fail closed.
            return False
        # Local dev only: allow unsigned callbacks so the mock rail loopback works.
        return True
    sig = request.headers.get("X-Samsoftpay-Signature", "")
    if not sig:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


@bp.post("/<channel>")
def receive(channel: str):
    try:
        ch = Channel(channel)
    except ValueError:
        abort(404)

    raw_body = request.get_data()
    if not _verify_signature(raw_body):
        abort(401)

    payload = request.get_json(silent=True) or {}
    rail_ref = payload.get("rail_reference") or payload.get("referenceId")
    if not rail_ref:
        abort(400)

    txn = Transaction.query.filter_by(rail_reference=rail_ref, channel=ch).one_or_none()
    if txn is None:
        abort(404)

    success = bool(payload.get("success"))
    reason  = payload.get("reason")
    complete_transaction(txn.id, success=success, rail_reference=rail_ref, reason=reason)
    return jsonify(ok=True)
