"""XY Vending §2.2.3 — order dispense result callback.

The supplier's doc calls this "Callback interface for order dispense results
(provided by merchants)". *We* are the merchant in that sentence: XY's backend
POSTs here after a machine has finished dispensing, to tell us what actually
came out of the machine.

WHY THIS MATTERS FOR MONEY
--------------------------
Until now the only signal we had was `ApplyExportGoods` returning code "1",
which means "the command was accepted" — NOT "the soda came out". A jammed
tray, an empty coil or a machine that lost power mid-cycle all return an
accepted command and then dispense nothing. We would have marked the order
`dispensed`, kept the customer's money, and never known.

This callback carries the ground truth:
  - `status`  — the order finished, was refunded, or partially refunded
  - `chsl`    — per product, the quantity actually dispensed (1 or 0)
  - `tkje`/`tksj` — refund amount and time, if XY refunded at their end

So an order can now be corrected from `dispensed` to `failed` when the machine
did not in fact deliver, which is what makes a refund decision possible.

WHAT THIS ENDPOINT DELIBERATELY DOES NOT DO
-------------------------------------------
It records the outcome. It does NOT move money. A forged or replayed callback
must never be able to trigger a refund on its own, so refunding stays a
merchant/operator action through the existing refund tooling. The blast radius
of a bad callback is therefore "an order's status is wrong", not "money left
the account".

SIGNATURE
---------
The doc's field table for §2.2.3 says "encrypt: No", but its own worked example
at the end of the section signs the callback with the same scheme as every
other interface:

    sign = MD5(secret + timestamp + reqData)
    reqData = the payload's scalar fields, "k=v&k=v", keys sorted ascending

We verify it and FAIL CLOSED in production, per the same rule as the rail
callbacks (an unsigned inbound call must never be able to change money state).

The doc is internally inconsistent about two field names — the table says
`status`/`dsfshdh` while the signing example says `state`/`dsfshbh` — and its
example is not actually sorted. We therefore accept any of the documented
spellings and try the small set of documented orderings rather than rejecting
a legitimate callback over a typo in the vendor's PDF.

SIGNATURE RECIPE (for testing / for the supplier's engineers):
  sign = MD5( merchant_xy_secret + timestamp + canonical ) where canonical is
  "k1=v1&k2=v2&..." over all non-empty payload fields EXCEPT `sign` itself,
  sorted by key. Both documented field spellings are accepted on our side
  (`status`/`state`, `dsfshdh`/`dsfshbh`) — the vendor doc contradicts itself,
  so do not "tidy" that tolerance away. See tests/test_xy_dispense_callback.py
  for a worked signed payload.
"""
from __future__ import annotations

import hashlib
import os

from flask import Blueprint, current_app, jsonify, request

from ..extensions import db
from ..models import Merchant, PaymentLink
from ..services import vending, xy_vending

bp = Blueprint("xy_inbound", __name__, url_prefix="/inbound/xy")

# Fields that are part of the signature base. `splist` is a nested list and the
# doc never includes it in a sign example, so it is excluded.
_NON_SIGNED = {"sign", "key", "timestamp", "splist"}

# The doc contradicts itself between its field table and its signing example.
_ALIASES = {"status": "state", "dsfshdh": "dsfshbh"}


def _sign_base(params: dict) -> str:
    return "&".join(f"{k}={xy_vending._str(params[k])}" for k in sorted(params))


def _candidate_signs(secret: str, timestamp: str, payload: dict) -> set[str]:
    """Every signature the doc could plausibly mean, for one payload.

    Small and bounded: the documented spelling, and the alternate spelling used
    in the signing example. Both are MD5 over the same secret+timestamp, so this
    widens spelling tolerance without weakening the secret.
    """
    scalars = {k: v for k, v in payload.items()
               if k not in _NON_SIGNED and not isinstance(v, (dict, list))}

    variants = [scalars]
    aliased = {_ALIASES.get(k, k): v for k, v in scalars.items()}
    if aliased != scalars:
        variants.append(aliased)

    out = set()
    for variant in variants:
        raw = f"{secret}{timestamp}{_sign_base(variant)}"
        out.add(hashlib.md5(raw.encode("utf-8")).hexdigest().lower())
    return out


def _verify(payload: dict, secret: str) -> bool:
    """Verify the callback signature. Fail closed when a secret is configured."""
    if not secret:
        # No credentials for this merchant. In production that is a hard stop —
        # we will not accept an unverifiable call that can change order state.
        return not os.environ.get("RENDER")
    supplied = str(payload.get("sign") or "").strip().lower()
    if not supplied:
        return False
    timestamp = xy_vending._str(payload.get("timestamp", ""))
    return supplied in _candidate_signs(secret, timestamp, payload)


# ---------- payload interpretation ----------

_FINISHED = {"1", "finish", "finished", "完成"}


def _is_finished(status: str) -> bool:
    return str(status).strip().lower() in _FINISHED


def _dispensed_count(payload: dict) -> int:
    """Total units the machine reports it actually pushed out."""
    total = 0
    for item in payload.get("splist") or []:
        try:
            total += int(str(item.get("chsl", 0)).strip() or 0)
        except (TypeError, ValueError):
            continue
    return total


def _refund_minor(payload: dict) -> int:
    try:
        return int(str(payload.get("tkje", 0)).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _find_order(payload: dict) -> PaymentLink | None:
    """Locate our order from the supplier's identifiers.

    `ddbh` is the order number we sent them, which is the payment link's
    public_id (see xy_vending.apply_export_goods). `dsfjybh` is the charge id we
    sent as the third-party transaction number — used as a fallback so a
    callback still lands if the operator's own order number was substituted.
    """
    ddbh = str(payload.get("ddbh") or "").strip()
    if ddbh:
        link = PaymentLink.query.filter_by(public_id=ddbh).one_or_none()
        if link is not None:
            return link
    dsfjybh = str(payload.get("dsfjybh") or "").strip()
    if dsfjybh:
        from ..models import Transaction
        txn = Transaction.query.filter_by(public_id=dsfjybh).one_or_none()
        if txn is not None:
            return PaymentLink.query.filter_by(transaction_id=txn.id).one_or_none()
    return None


def _ack(message: str = "Received data successfully"):
    """The supplier expects code "1" to mean 'received'. Non-1 makes them retry."""
    return jsonify(code="1", message=message)


def _reject(message: str, http_status: int = 401):
    return jsonify(code="0", message=message), http_status


# ---------- the endpoint ----------

@bp.post("/dispense-result")
def dispense_result():
    """XY §2.2.3 — the machine finished (or failed to) dispense an order.

    Give this URL to the supplier as the order-result callback:
        https://api.samsoftpay.com/inbound/xy/dispense-result
    """
    payload = request.get_json(silent=True) or {}
    if not payload:
        return _reject("empty or non-JSON body", 400)

    link = _find_order(payload)
    if link is None:
        # Nothing we can act on, but acknowledge so the supplier does not retry
        # forever. Recorded so an unmatched callback is visible, never silent.
        _audit("vending.callback_unmatched", None, payload)
        current_app.logger.warning(
            "XY dispense callback for unknown order ddbh=%s dsfjybh=%s",
            payload.get("ddbh"), payload.get("dsfjybh"),
        )
        return _ack("received; order not recognised")

    merchant = db.session.get(Merchant, link.merchant_id)
    secret = xy_vending.for_merchant(merchant).secret if merchant else ""
    if not _verify(payload, secret):
        _audit("vending.callback_rejected", link, payload)
        current_app.logger.warning(
            "XY dispense callback failed signature for order %s", link.public_id
        )
        return _reject("invalid signature")

    if not vending.is_vending_order(link):
        return _ack("received; not a vending order")

    finished = _is_finished(payload.get("status", payload.get("state", "")))
    dispensed = _dispensed_count(payload)
    refunded = _refund_minor(payload)

    # Ground truth: the product is only really out if the machine says a unit
    # left a tray AND the order was not refunded at the supplier's end.
    delivered = finished and dispensed > 0 and refunded <= 0

    if delivered:
        vending._finish(link.id, ok=True)
    else:
        reason = _reason(finished, dispensed, refunded)
        vending._finish(link.id, ok=False, error=f"supplier: {reason}")

    _audit("vending.dispense_confirmed" if delivered else "vending.dispense_disconfirmed",
           link, payload)
    return _ack()


def _reason(finished: bool, dispensed: int, refunded: int) -> str:
    if refunded > 0:
        return f"refunded {refunded} at supplier"
    if dispensed <= 0:
        return "machine reported nothing dispensed"
    if not finished:
        return "order not finished"
    return "not delivered"


def _audit(event: str, link: PaymentLink | None, payload: dict) -> None:
    """Record the raw supplier report. Never raises — an audit failure must not
    cost us the callback."""
    try:
        from ..services.audit import log_event
        log_event(
            event,
            merchant_id=link.merchant_id if link is not None else None,
            resource_id=link.public_id if link is not None else None,
            detail={
                "jqbh": payload.get("jqbh"),
                "shbh": payload.get("shbh"),
                "ddbh": payload.get("ddbh"),
                "dsfjybh": payload.get("dsfjybh"),
                "paytype": payload.get("paytype"),
                "status": payload.get("status", payload.get("state")),
                "payamount": payload.get("payamount"),
                "tkje": payload.get("tkje"),
                "splist": payload.get("splist"),
            },
        )
    except Exception:  # pragma: no cover - defensive
        pass
