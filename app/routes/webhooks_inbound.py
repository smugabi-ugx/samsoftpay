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

from ..extensions import db, limiter
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
@limiter.limit("600 per minute")  # generous per-IP cap: a real rail burst is fine, DB-exhaustion floods are not
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


@bp.route("/mtn/callback", methods=["POST", "PUT"])
@limiter.limit("600 per minute")
def mtn_native_callback():
    """MTN's OWN requesttopay callback (providerCallbackHost) — hint-verify design.

    MTN does not sign its callbacks with a secret we can verify, and a callback
    marks money as moved — so this route NEVER completes a charge from the
    payload alone. The callback is treated as a HINT: we extract the reference,
    then ask MTN's status API for the truth and complete from THAT answer (the
    same authority the poller and the stale sweep already use). A forged
    callback can therefore only trigger a status check against MTN — it can
    never invent a success. This is what turns completion from ~poller-latency
    into instant, without weakening guardrail 9's fail-closed stance.

    Always answers 200: MTN retries non-2xx, and the response must not leak
    which references exist.
    """
    from ..services import sweep as sweep_svc

    payload = request.get_json(silent=True) or {}
    # MTN callbacks are inconsistently shaped across products/versions; accept
    # the reference from the header or the common body fields. externalId is
    # OUR public id (set at initiate), referenceId is OUR X-Reference-Id uuid.
    ref = (request.headers.get("X-Reference-Id")
           or payload.get("referenceId")
           or payload.get("externalId")
           or "").strip()
    if not ref:
        return jsonify(ok=True)

    # Only meaningful with the real rail: in mock mode the timer completes
    # charges and there is no MTN to verify against.
    if not current_app.config.get("MOMO_USE_REAL"):
        return jsonify(ok=True)

    txn = Transaction.query.filter(
        (Transaction.rail_reference == ref) | (Transaction.public_id == ref)
    ).one_or_none()
    if txn is None or not txn.rail_reference:
        current_app.logger.info("mtn callback for unknown reference %s", ref[:64])
        return jsonify(ok=True)
    if txn.channel != Channel.MTN_MOMO:
        return jsonify(ok=True)

    # THE VERIFY: MTN's status API is the only authority. None/PENDING = do
    # nothing (poller/sweep continue to own it) — an unknown outcome is never
    # a failure (guardrail 21).
    status = sweep_svc._query_mtn_status(txn.rail_reference)
    if status == "SUCCESSFUL":
        complete_transaction(txn.id, success=True, rail_reference=txn.rail_reference)
    elif status == "FAILED":
        complete_transaction(txn.id, success=False,
                             rail_reference=txn.rail_reference, reason="momo_failed")
    return jsonify(ok=True)
