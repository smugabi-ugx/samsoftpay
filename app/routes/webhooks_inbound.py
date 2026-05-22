"""Inbound webhooks from real rails would land here.

For real MTN MoMo Collections, the callback URL you configure on developer.mtn.com
hits this endpoint. We verify the X-Reference-Id, look up the transaction, and
call orchestrator.complete_transaction.

In the mock world, the timer in rails.py does this directly in-process. This route
exists as a placeholder so the structure is right when you wire up a real rail.
"""
from flask import Blueprint, abort, jsonify, request

from ..extensions import db
from ..models import Channel, Transaction
from ..services.orchestrator import complete_transaction

bp = Blueprint("inbound", __name__, url_prefix="/inbound")


@bp.post("/<channel>")
def receive(channel: str):
    try:
        ch = Channel(channel)
    except ValueError:
        abort(404)
    payload = request.get_json(silent=True) or {}
    rail_ref = payload.get("rail_reference") or payload.get("referenceId")
    if not rail_ref:
        abort(400)
    txn = Transaction.query.filter_by(rail_reference=rail_ref, channel=ch).one_or_none()
    if txn is None:
        abort(404)
    success = bool(payload.get("success"))
    reason = payload.get("reason")
    complete_transaction(txn.id, success=success, rail_reference=rail_ref, reason=reason)
    return jsonify(ok=True)
