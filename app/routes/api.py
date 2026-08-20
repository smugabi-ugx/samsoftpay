"""Public API surface that merchants integrate with.

Auth:         Bearer secret_key in Authorization header.
Idempotency:  Idempotency-Key header required on all POST requests.
Replay guard: X-Timestamp header (Unix seconds) required on POST requests.
              Rejected if timestamp is more than 5 minutes old.
Rate limits:  POST /v1/charges  — CHARGE_RATE_LIMIT (default 120/min, 3000/hr) per key
              POST /v1/payouts  — PAYOUT_RATE_LIMIT (default 30/min, 600/hr) per key
              High-volume platforms: use /v1/payouts/bulk (one request, many
              payouts) and honour 429 Retry-After.
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

    # g.key_scope: "full" (charges + payouts) or "collections" (charges only).
    # Full secret keys checked first, then collections-only keys.
    g.key_scope = "full"
    merchant = Merchant.query.filter_by(test_secret_key_hash=token_hash).one_or_none()
    if merchant:
        g.api_mode = "test"
    else:
        merchant = Merchant.query.filter_by(secret_key_hash=token_hash).one_or_none()
        if merchant:
            g.api_mode = "live"
        else:
            merchant = Merchant.query.filter_by(test_collections_key_hash=token_hash).one_or_none()
            if merchant:
                g.api_mode, g.key_scope = "test", "collections"
            else:
                merchant = Merchant.query.filter_by(collections_key_hash=token_hash).one_or_none()
                if merchant:
                    g.api_mode, g.key_scope = "live", "collections"
                else:
                    # Legacy plaintext fallback (pre-backfill) — full keys only.
                    merchant = Merchant.query.filter_by(test_secret_key=token).one_or_none()
                    if merchant:
                        g.api_mode = "test"
                    else:
                        merchant = Merchant.query.filter_by(secret_key=token).one_or_none()
                        if merchant:
                            g.api_mode = "live"

    # A MANAGED merchant (a platform's subaccount) is a ledger identity only —
    # it must never authenticate, even though it technically holds (random,
    # never-displayed) key columns. Belt-and-braces with splits.create_subaccount.
    if merchant is None or not merchant.is_active or getattr(merchant, "is_managed", False):
        log_event("auth.failed", detail={"reason": "invalid_key"})
        abort(401, description="invalid api key")
    return merchant


def _require_full_scope() -> None:
    """Reject a collections-only key on a money-OUT endpoint.

    This is the whole point of scoped keys: a kiosk credential (which can be
    decompiled off a public machine) must never be able to move money out.
    """
    if g.get("key_scope") != "full":
        log_event("auth.scope_denied", detail={"endpoint": request.path})
        abort(403, description="this endpoint requires a full secret key; "
                               "collections-only keys cannot move money out")


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


def _replayed(payload: dict, status: int):
    """Return a cached idempotent response, marked as a replay.

    Flutterwave lesson: a replay must be DISTINGUISHABLE from a fresh
    execution — `Idempotent-Replayed: true` lets a retrying integrator KNOW
    the payout/charge wasn't fired twice, instead of guessing.
    """
    resp = jsonify(payload)
    resp.headers["Idempotent-Replayed"] = "true"
    return resp, status


def _optional_idempotency(merchant, body):
    """Optional-but-honored idempotency for money-adjacent CREATE endpoints
    (payment links, vending orders). If the caller sends an Idempotency-Key we
    dedupe a retried create (reserve-before-execute, like /charges); if not, we
    proceed — so existing integrators that don't send one keep working.

    Returns (idem_key, request_hash, cached). `cached` is (body, status) to
    return directly when the key was already completed, else None. Call all
    input validation BEFORE this so a bad request never reserves a key.
    """
    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        return None, None, None
    request_hash = idempotency.hash_body(body)
    existing = idempotency.find(merchant.id, idem_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            abort(409, description="idempotency key reused with different request body")
        if existing.response_status == idempotency.IN_FLIGHT:
            abort(409, description="a request with this Idempotency-Key is still in flight — retry shortly")
        return idem_key, request_hash, (json.loads(existing.response_body), existing.response_status)
    if not idempotency.reserve(merchant.id, idem_key, request_hash):
        abort(409, description="a request with this Idempotency-Key is still in flight — retry shortly")
    return idem_key, request_hash, None


# ---------- error handlers ----------

@bp.errorhandler(400)
@bp.errorhandler(401)
@bp.errorhandler(403)
@bp.errorhandler(404)
@bp.errorhandler(409)
@bp.errorhandler(429)
def _err(e):
    # Every /v1 error is machine-readable JSON — a kiosk or server integration
    # must never have to parse an HTML error page.
    return jsonify(error=e.description or e.name), e.code


# ---------- charges ----------

@bp.post("/charges")
@limiter.limit(lambda: __import__("flask").current_app.config["CHARGE_RATE_LIMIT"])
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
        if existing.response_status == idempotency.IN_FLIGHT:
            abort(409, description="a request with this Idempotency-Key is still in flight — retry shortly")
        return _replayed(json.loads(existing.response_body), existing.response_status)
    # Reserve the key BEFORE executing: concurrent duplicates used to both
    # find nothing and both execute (double prompt / double disbursement).
    if not idempotency.reserve(merchant.id, idem_key, request_hash):
        abort(409, description="a request with this Idempotency-Key is still in flight — retry shortly")

    try:
        amount = int(body["amount"])
        currency = body.get("currency", "UGX")
        channel = Channel(body["channel"])
        customer = body.get("customer") or {}
        split = body.get("split")   # optional [{subaccount, amount|bps}, ...]
        if split is not None and not isinstance(split, list):
            raise ValueError("split must be a list")
    except (KeyError, ValueError, TypeError) as exc:
        idempotency.release(merchant.id, idem_key)
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
            split=split,
        )
    except OrchestratorError as exc:
        body_out = {"error": str(exc)}
        idempotency.store(merchant.id, idem_key, request_hash, 400, body_out)
        log_event("charge.rejected", merchant_id=merchant.id, detail={"reason": str(exc)})
        return jsonify(body_out), 400
    except Exception as exc:
        # A rail network error used to propagate as a raw 500 with the session
        # rolled back mid-flight. Return a clean 502 the integrator can retry.
        # Deliberately NOT stored under the idempotency key: a transient rail
        # outage must not poison this key with a permanent error (retrying
        # with the same key should attempt the charge again).
        db.session.rollback()
        idempotency.release(merchant.id, idem_key)
        from flask import current_app
        current_app.logger.exception("charge creation crashed (rail error?)")
        return jsonify(error="payment rail temporarily unavailable — retry with the same Idempotency-Key"), 502

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


def _charge_public(txn) -> dict:
    """Public charge shape — shared by the by-id and list endpoints so they
    can never drift."""
    # Stripe lesson: ambiguity about WHEN money becomes withdrawable — not the
    # hold itself — is what merchants distrust. available_on states it per
    # charge: the actual settlement time once swept, else completed_at + the
    # 24h hold (the sweep runs hourly, so the estimate is at most an hour shy).
    available_on = None
    if txn.settled_at is not None:
        available_on = txn.settled_at.isoformat()
    elif txn.completed_at is not None and txn.status.value == "succeeded":
        from datetime import timedelta
        available_on = (txn.completed_at + timedelta(hours=24)).isoformat()
    return dict(
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
        settled=txn.settled_at is not None,
        available_on=available_on,
    )


def _parse_list_params(default_limit=20, max_limit=100):
    """limit (clamped) + created_after/created_before (ISO 8601). 400 on bad input."""
    from datetime import datetime
    try:
        limit = int(request.args.get("limit", default_limit))
    except ValueError:
        abort(400, description="limit must be an integer")
    limit = max(1, min(limit, max_limit))

    def _dt(name):
        raw = request.args.get(name)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            abort(400, description=f"{name} must be an ISO 8601 datetime")
    return limit, _dt("created_after"), _dt("created_before")


@bp.get("/charges/<public_id>")
def get_charge(public_id: str):
    merchant = _auth()
    txn = Transaction.query.filter_by(public_id=public_id, merchant_id=merchant.id).one_or_none()
    if txn is None:
        abort(404)
    out = _charge_public(txn)
    # Split charges: expose each resolved share so the platform can reconcile
    # every sub-ledger against ours. Allocations exist only once SUCCEEDED.
    if txn.split_meta:
        from ..models import SplitAllocation, Subaccount
        allocs = SplitAllocation.query.filter_by(transaction_id=txn.id).all()
        sub_by_mid = {
            s.merchant_id: s.public_id
            for s in Subaccount.query.filter_by(platform_merchant_id=merchant.id).all()
        }
        out["split"] = [
            dict(
                # The platform's own residual row has no sub_ id — label it.
                subaccount=sub_by_mid.get(a.merchant_id, "platform_residual"),
                amount=a.amount,
                settlement_status=(
                    "reversed" if a.reversed_at else
                    "available" if a.settled_at else "pending"),
                settled_at=a.settled_at.isoformat() if a.settled_at else None,
            )
            for a in allocs
        ]
    return jsonify(out)


@bp.get("/charges")
def list_charges():
    """List this merchant's charges, newest first. Cursor pagination
    (limit + starting_after=<charge id>), mode-scoped to the key (a test key
    sees only test charges), never another merchant's rows. Filters: status,
    reference, created_after/created_before (ISO 8601)."""
    from ..models import TxnStatus
    merchant = _auth()
    q = Transaction.query.filter_by(
        merchant_id=merchant.id, is_test=(g.api_mode == "test"))

    status_f = request.args.get("status")
    if status_f:
        try:
            q = q.filter(Transaction.status == TxnStatus(status_f))
        except ValueError:
            abort(400, description=f"invalid status: {status_f}")
    ref = request.args.get("reference")
    if ref:
        q = q.filter(Transaction.merchant_reference == ref)

    limit, after, before = _parse_list_params()
    if after:
        q = q.filter(Transaction.created_at >= after)
    if before:
        q = q.filter(Transaction.created_at <= before)

    starting_after = request.args.get("starting_after")
    if starting_after:
        cursor = Transaction.query.filter_by(
            public_id=starting_after, merchant_id=merchant.id).one_or_none()
        if cursor is None:
            abort(400, description="invalid starting_after cursor")
        q = q.filter(Transaction.id < cursor.id)

    rows = q.order_by(Transaction.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return jsonify(
        object="list",
        data=[_charge_public(t) for t in rows],
        has_more=has_more,
        next_cursor=(rows[-1].public_id if rows and has_more else None),
    )


# ---------- refunds ----------

@bp.post("/charges/<public_id>/refund")
@limiter.limit("10 per minute")
def refund_charge_route(public_id: str):
    """Refund a succeeded charge.

    Now carries the same replay guard as every other money POST — it was the
    one exception to the module's own "Idempotency-Key required on all POSTs"
    contract, on the endpoint where a replayed request pays out real money.
    The refund itself is additionally guarded by the REFUNDED claim inside
    refund_charge, so this is defense in depth, not the only gate.
    """
    from ..services.refunds import RefundError, refund_charge

    _check_timestamp()
    merchant = _auth()
    _require_full_scope()   # refund moves money out — collections-only keys forbidden
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
@limiter.limit(lambda: __import__("flask").current_app.config["PAYOUT_RATE_LIMIT"])
def create_payout_route():
    from ..models import Payout
    from ..services.payouts import PayoutError, create_payout

    _check_timestamp()
    merchant = _auth()
    _require_full_scope()   # money out — collections-only keys forbidden

    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        abort(400, description="Idempotency-Key header required")

    body = request.get_json(silent=True) or {}
    request_hash = idempotency.hash_body(body)

    existing = idempotency.find(merchant.id, idem_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            abort(409, description="idempotency key reused with different request body")
        if existing.response_status == idempotency.IN_FLIGHT:
            abort(409, description="a request with this Idempotency-Key is still in flight — retry shortly")
        return _replayed(json.loads(existing.response_body), existing.response_status)
    # Reserve the key BEFORE executing: concurrent duplicates used to both
    # find nothing and both execute (double prompt / double disbursement).
    if not idempotency.reserve(merchant.id, idem_key, request_hash):
        abort(409, description="a request with this Idempotency-Key is still in flight — retry shortly")

    try:
        amount = int(body["amount"])
        currency = body.get("currency", "UGX")
        recipient = body.get("recipient") or {}
        recipient_phone = recipient["phone"]
        channel = Channel(body.get("channel", "mtn_momo"))
    except (KeyError, ValueError, TypeError) as exc:
        idempotency.release(merchant.id, idem_key)
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


def _payout_public(p) -> dict:
    """Public payout shape — shared by the by-id and list endpoints."""
    return dict(
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


@bp.get("/payouts/<public_id>")
def get_payout(public_id: str):
    from ..models import Payout
    merchant = _auth()
    p = Payout.query.filter_by(public_id=public_id, merchant_id=merchant.id).one_or_none()
    if p is None:
        abort(404)
    return jsonify(_payout_public(p))


@bp.get("/payouts")
def list_payouts():
    """List this merchant's payouts, newest first. Cursor pagination
    (limit + starting_after=<payout id>), mode-scoped, never another merchant's
    rows. Filters: status, created_after/created_before (ISO 8601)."""
    from ..models import Payout, PayoutStatus
    merchant = _auth()
    q = Payout.query.filter_by(merchant_id=merchant.id, is_test=(g.api_mode == "test"))

    status_f = request.args.get("status")
    if status_f:
        try:
            q = q.filter(Payout.status == PayoutStatus(status_f))
        except ValueError:
            abort(400, description=f"invalid status: {status_f}")

    limit, after, before = _parse_list_params()
    if after:
        q = q.filter(Payout.created_at >= after)
    if before:
        q = q.filter(Payout.created_at <= before)

    starting_after = request.args.get("starting_after")
    if starting_after:
        cursor = Payout.query.filter_by(
            public_id=starting_after, merchant_id=merchant.id).one_or_none()
        if cursor is None:
            abort(400, description="invalid starting_after cursor")
        q = q.filter(Payout.id < cursor.id)

    rows = q.order_by(Payout.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return jsonify(
        object="list",
        data=[_payout_public(p) for p in rows],
        has_more=has_more,
        next_cursor=(rows[-1].public_id if rows and has_more else None),
    )


# ---------- vending: orders, QR, dispense (XY Vending) ----------

@bp.post("/vending/orders")
@limiter.limit("60 per minute")
def create_vending_order():
    """Create a vending order and get back the QR the machine should display.

    Body:
      { "machine": "<jqbh>",
        "amount": 2500,                     (whole UGX — what the customer pays)
        "goods": [ {"spbh": "0001", "spmc": "Coke", "spdj": 2500} ],
        "reference": "...",     (optional, your own order id)
        "pay_account": "...",   (optional fallback payer id for the supplier)
        "success_url": "https://…" (optional) }

    Returns the checkout URL plus a ready-made QR. The customer scans it, pays
    with Mobile Money, and the machine dispenses automatically on success — the
    machine itself never needs to handle the payment.
    """
    from flask import url_for
    from ..services import vending

    _check_timestamp()
    merchant = _auth()
    body = request.get_json(silent=True) or {}

    try:
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError):
        abort(400, description="amount (positive integer) is required")

    # Idempotency (honored when a key is sent): a retried POST /vending/orders
    # used to create a duplicate order + QR against the same machine.
    idem_key, request_hash, cached = _optional_idempotency(merchant, body)
    if cached is not None:
        return _replayed(cached[0], cached[1])

    try:
        link = vending.create_order(
            merchant=merchant,
            machine=str(body.get("machine") or ""),
            goods=body.get("goods") or [],
            amount=amount,
            currency=str(body.get("currency") or "UGX"),
            reference=str(body.get("reference") or "")[:120] or None,
            pay_account=body.get("pay_account"),
            description=str(body.get("description") or "")[:255] or None,
            success_url=body.get("success_url"),
            is_test=(g.api_mode == "test"),
        )
    except vending.VendingError as exc:
        if idem_key:
            idempotency.release(merchant.id, idem_key)   # nothing created — allow retry
        abort(400, description=str(exc))

    pay_url = url_for("checkout.checkout_page", public_id=link.public_id, _external=True)
    log_event("vending.order_created", merchant_id=merchant.id, resource_id=link.public_id,
              detail={"machine": body.get("machine"), "amount": amount})
    out = dict(
        order_id=link.public_id,
        amount=link.amount,
        currency=link.currency,
        machine=str(body.get("machine")),
        pay_url=pay_url,
        qr_content=pay_url,      # encode this string if you draw the QR yourself
        qr_svg_url=url_for("checkout.order_qr_svg", public_id=link.public_id, _external=True),
        qr_png_url=url_for("checkout.order_qr_png", public_id=link.public_id, _external=True),
        display_url=url_for("checkout.vending_display", public_id=link.public_id, _external=True),
        status_url=url_for("api.get_vending_order", order_id=link.public_id, _external=True),
        payment_status="unpaid",
        vending_status=link.vending_status,
    )
    if idem_key:
        idempotency.store(merchant.id, idem_key, request_hash, 201, out)
    return jsonify(out), 201


@bp.get("/vending/orders/<order_id>")
def get_vending_order(order_id: str):
    """Poll an order: is it paid, and has the machine dispensed?

    Also acts as a safety net — if the charge succeeded but the dispense never
    fired (worker restart, supplier blip), polling retries it once here.
    """
    from ..models import PaymentLink, TxnStatus
    from ..services import vending

    merchant = _auth()
    link = PaymentLink.query.filter_by(
        public_id=order_id, merchant_id=merchant.id
    ).one_or_none()
    if link is None or not vending.is_vending_order(link):
        abort(404, description="vending order not found")

    if link.vending_status == vending.PENDING and link.transaction_id:
        txn = db.session.get(Transaction, link.transaction_id)
        if txn is not None and txn.status == TxnStatus.SUCCEEDED:
            vending.dispense_for_link(link, txn)
            db.session.refresh(link)

    return jsonify(**vending.order_state(link)), 200


@bp.post("/vending/orders/<order_id>/dispense")
@limiter.limit("30 per minute")
def retry_vending_order(order_id: str):
    """Retry a dispense that failed. Only works on a SUCCEEDED charge."""
    from ..models import PaymentLink
    from ..services import vending

    _check_timestamp()
    merchant = _auth()
    link = PaymentLink.query.filter_by(
        public_id=order_id, merchant_id=merchant.id
    ).one_or_none()
    if link is None or not vending.is_vending_order(link):
        abort(404, description="vending order not found")

    try:
        ok = vending.retry_dispense(link)
    except vending.VendingError as exc:
        abort(400, description=str(exc))
    db.session.refresh(link)
    return jsonify(ok=ok, **vending.order_state(link)), 200


@bp.get("/vending/machines")
def list_vending_machines():
    """Your machines, from our registry. Run a sync first to populate it."""
    from ..services import vending
    merchant = _auth()
    if not getattr(merchant, "vending_enabled", False):
        abort(403, description="vending is not enabled for this merchant")
    machines = vending.machines_for(merchant)
    return jsonify(machines=[vending.machine_dict(m) for m in machines],
                   count=len(machines)), 200


@bp.post("/vending/machines/sync")
@limiter.limit("10 per minute")
def sync_vending_machines():
    """Refresh the registry from the supplier using YOUR XY credentials."""
    from ..services import vending
    _check_timestamp()
    merchant = _auth()
    if not getattr(merchant, "vending_enabled", False):
        abort(403, description="vending is not enabled for this merchant")
    try:
        added, updated = vending.sync_machines(merchant)
    except vending.VendingError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:      # supplier unreachable / rejected
        return jsonify(ok=False, error=str(exc)), 502
    machines = vending.machines_for(merchant)
    log_event("vending.machines_synced", merchant_id=merchant.id,
              detail={"added": added, "updated": updated})
    return jsonify(ok=True, added=added, updated=updated,
                   machines=[vending.machine_dict(m) for m in machines]), 200


@bp.get("/vending/machines/<machine>/goods")
def vending_machine_goods(machine: str):
    """Live tray inventory for one machine — build your menu from this."""
    from ..services import vending, xy_vending
    merchant = _auth()
    if not getattr(merchant, "vending_enabled", False):
        abort(403, description="vending is not enabled for this merchant")
    if not vending.owns_machine(merchant, str(machine)):
        abort(404, description="machine not registered to this merchant")
    try:
        return jsonify(xy_vending.query_machine_goods(
            str(machine), creds=xy_vending.for_merchant(merchant))), 200
    except xy_vending.XYVendingError as exc:
        return jsonify(ok=False, error=str(exc)), 502


@bp.post("/vending/dispense")
@limiter.limit("60 per minute")
def vending_dispense():
    """Tell an XY Vending machine to dispense after a payment is confirmed.

    Body:
      { "machine": "<jqbh>",
        "order_id": "<your order no>",
        "pay_account": "<payer account>",
        "goods": [ {"spbh": "0001", "spmc": "Coke", "spdj": 250}, ... ],
        "charge_id": "txn_..."   (optional — used as the third-party txn id) }

    Requires a confirmed, SUCCEEDED charge id belonging to this merchant: we will
    NOT dispense unless the charge actually succeeded. This is the payment->dispense
    guard ("no payment, no dispense").
    """
    _check_timestamp()
    merchant = _auth()
    from ..services import xy_vending

    body = request.get_json(silent=True) or {}
    machine = body.get("machine")
    order_id = body.get("order_id")
    goods = body.get("goods") or []
    charge_id = body.get("charge_id")
    if not machine or not order_id or not goods:
        abort(400, description="machine, order_id and goods are required")

    # Enforce: only dispense against a real SUCCEEDED charge for THIS merchant,
    # and CONSUME it atomically — one charge pays for ONE dispense. Without
    # the claim, the same SUCCEEDED charge_id authorized unlimited dispenses
    # under fresh order_ids (guardrail 11's "once" only held inside the order
    # flow, and this endpoint bypasses that flow by design).
    third_party_txn = charge_id or order_id
    if charge_id:
        txn = Transaction.query.filter_by(public_id=charge_id, merchant_id=merchant.id).one_or_none()
        if txn is None:
            abort(404, description="charge not found")
        from ..models import TxnStatus, utcnow
        if txn.status != TxnStatus.SUCCEEDED:
            abort(400, description=f"cannot dispense: charge status is {txn.status.value}, not succeeded")
        claimed = (
            db.session.query(Transaction)
            .filter(Transaction.id == txn.id,
                    Transaction.vending_consumed_at.is_(None))
            .update({"vending_consumed_at": utcnow()}, synchronize_session=False)
        )
        db.session.commit()
        if not claimed:
            abort(409, description="this charge has already paid for a dispense")

    from ..services import vending as _vending
    if not _vending.owns_machine(merchant, str(machine)):
        abort(404, description="machine not registered to this merchant")

    try:
        resp = xy_vending.apply_export_goods(
            jqbh=str(machine),
            order_id=str(order_id),
            third_party_txn_id=str(third_party_txn),
            pay_account=str(body.get("pay_account") or merchant.handle or merchant.id),
            goods=goods,
            creds=xy_vending.for_merchant(merchant),
        )
    except xy_vending.XYVendingError as exc:
        # Supplier failed — no product came out, so RELEASE the consumption
        # claim: the customer's paid charge must remain usable for a retry.
        if charge_id:
            db.session.query(Transaction).filter(
                Transaction.public_id == charge_id,
                Transaction.merchant_id == merchant.id,
            ).update({"vending_consumed_at": None}, synchronize_session=False)
            db.session.commit()
        log_event("vending.dispense_failed", merchant_id=merchant.id,
                  resource_id=str(order_id), detail={"error": str(exc)})
        return jsonify(ok=False, error=str(exc)), 502

    log_event("vending.dispensed", merchant_id=merchant.id, resource_id=str(order_id),
              detail={"machine": machine, "charge_id": charge_id})
    return jsonify(ok=True, machine=machine, order_id=order_id, supplier_response=resp), 200


# ---------- bulk payouts (CSV or JSON) ----------

def _parse_bulk_items() -> list:
    """Read payout items from a JSON body {payouts:[...]}, an uploaded CSV file,
    or a raw text/csv body. CSV columns (header row): phone, amount, name, reference."""
    ctype = (request.content_type or "")
    if "application/json" in ctype:
        body = request.get_json(silent=True) or {}
        return body.get("payouts") or []

    csv_text = None
    if request.files:
        f = next(iter(request.files.values()))
        csv_text = f.read().decode("utf-8", "ignore")
    elif "csv" in ctype or "text/plain" in ctype:
        csv_text = request.get_data(as_text=True)

    if not csv_text:
        return []
    import csv as _csv
    import io as _io
    items = []
    for row in _csv.DictReader(_io.StringIO(csv_text)):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        items.append({
            "phone": r.get("phone") or r.get("msisdn") or r.get("number"),
            "amount": r.get("amount"),
            "name": r.get("name") or None,
            "reference": r.get("reference") or r.get("ref") or None,
        })
    return items


@bp.post("/payouts/bulk")
@limiter.limit(lambda: __import__("flask").current_app.config["PAYOUT_RATE_LIMIT"])
def create_bulk_payout():
    """Create many payouts in one call from a CSV (or JSON array).

    Each item is deduped by its `reference` (idempotency), so re-submitting the
    same batch never double-pays. Items are independent — one failure (bad row or
    insufficient balance) does not stop the rest. Returns a per-item result list.
    """
    import uuid as _uuid
    from ..models import Channel
    from ..services.payouts import PayoutError, create_payout

    _check_timestamp()
    merchant = _auth()
    _require_full_scope()   # bulk money out — collections-only keys forbidden

    items = _parse_bulk_items()
    if not items:
        abort(400, description="no payout items provided (JSON {payouts:[...]} or CSV)")
    if len(items) > 1000:
        abort(400, description="batch too large (max 1000 items per call)")

    batch_id = "batch_" + _uuid.uuid4().hex[:16]
    # The caller's Idempotency-Key anchors per-item dedupe for rows WITHOUT
    # their own reference. The old fallback used the fresh random batch_id —
    # so a retried CSV upload with no references paid every row AGAIN.
    client_key = request.headers.get("Idempotency-Key") or batch_id
    results = []
    for i, item in enumerate(items):
        try:
            amount = int(item["amount"])
            phone = str(item["phone"]).strip()
            name = item.get("name")
            ref = str(item.get("reference") or f"{client_key}-{i}")
            channel = Channel(item.get("channel", "mtn_momo"))
        except (KeyError, ValueError, TypeError) as exc:
            results.append({"index": i, "ok": False, "error": f"invalid item: {exc}"})
            continue

        # Per-item idempotency keyed by the caller's reference.
        idem_key = f"bulkpayout:{ref}"
        item_hash = idempotency.hash_body(item)
        existing = idempotency.find(merchant.id, idem_key)
        if existing is not None:
            if existing.response_status == idempotency.IN_FLIGHT:
                results.append({"index": i, "ok": False, "reference": ref,
                                "error": "a payout with this reference is still in flight"})
            elif existing.request_hash != item_hash:
                # Same reference, DIFFERENT payout details: silently returning
                # the old payout as ok:true meant a caller's reference-reuse bug
                # reported success while paying nobody the new amount.
                results.append({"index": i, "ok": False, "reference": ref,
                                "error": "reference reused with different payout details"})
            else:
                # replayed=True: same contract as the Idempotent-Replayed
                # header — the caller KNOWS this item wasn't disbursed twice.
                results.append({"index": i, "ok": True, "reference": ref,
                                "replayed": True,
                                **json.loads(existing.response_body)})
            continue

        # RESERVE the key BEFORE disbursing. Without this, two concurrent
        # identical batches both saw existing=None above and both called
        # create_payout — a real double-disbursement (the account row-lock only
        # serialises the balance check, it does not dedupe). Same reserve-before-
        # execute guard the single /payouts and /charges paths already carry.
        if not idempotency.reserve(merchant.id, idem_key, item_hash):
            results.append({"index": i, "ok": False, "reference": ref,
                            "error": "a payout with this reference is still in flight"})
            continue

        try:
            payout = create_payout(
                merchant=merchant, amount=amount, currency="UGX",
                recipient_phone=phone, recipient_name=name, channel=channel,
            )
            out = {
                "id": payout.public_id,
                "status": payout.status.value,
                "amount": payout.amount,
                "recipient_phone": payout.recipient_phone,
            }
            idempotency.store(merchant.id, idem_key, item_hash, 201, out)
            results.append({"index": i, "ok": True, "reference": ref, **out})
        except PayoutError as exc:
            # No money moved on a clean rejection — release so the reference can
            # be retried (e.g. after the merchant tops up), matching the bulk
            # path's prior retry-on-rejection behaviour.
            idempotency.release(merchant.id, idem_key)
            results.append({"index": i, "ok": False, "reference": ref, "error": str(exc)})

    accepted = sum(1 for r in results if r.get("ok"))
    log_event("payout.bulk", merchant_id=merchant.id, resource_id=batch_id,
              detail={"total": len(items), "accepted": accepted})
    return jsonify(
        batch_id=batch_id, total=len(items), accepted=accepted,
        failed=len(items) - accepted, results=results,
    ), 200


# ---------- subaccounts (split payments) ----------

@bp.post("/subaccounts")
@limiter.limit("30 per minute")
def create_subaccount_route():
    """Register a sub-merchant this platform can direct split shares to.

    A subaccount is a managed ledger identity — it cannot log in or call the
    API; its money settles through the normal per-merchant machinery and the
    platform pays it out (merchant-of-record stays the platform).
    """
    from ..services.splits import SplitError, create_subaccount

    _check_timestamp()
    merchant = _auth()
    body = request.get_json(silent=True) or {}
    try:
        sub = create_subaccount(
            platform=merchant,
            name=str(body.get("name") or ""),
            payout_phone=(str(body.get("payout_phone")) if body.get("payout_phone") else None),
            external_ref=(str(body.get("external_ref"))[:120] if body.get("external_ref") else None),
        )
    except SplitError as exc:
        abort(400, description=str(exc))
    log_event("subaccount.created", merchant_id=merchant.id, resource_id=sub.public_id,
              detail={"name": sub.name})
    return jsonify(id=sub.public_id, name=sub.name, external_ref=sub.external_ref,
                   payout_phone=sub.payout_phone, status="active"), 201


def _subaccount_balances(sub, mode: bool) -> list[dict]:
    from ..models import Account, AccountType
    rows = Account.query.filter(
        Account.merchant_id == sub.merchant_id,
        Account.is_test.is_(True) if mode else Account.is_test.is_(False),
        Account.type.in_([AccountType.MERCHANT_PENDING, AccountType.MERCHANT_AVAILABLE]),
    ).all()
    by_ccy: dict = {}
    for a in rows:
        entry = by_ccy.setdefault(a.currency, {"currency": a.currency, "pending": 0, "available": 0})
        # credit-normal accounts are stored negative — flip for display
        if a.type == AccountType.MERCHANT_PENDING:
            entry["pending"] = -a.cached_balance
        else:
            entry["available"] = -a.cached_balance
    return list(by_ccy.values())


@bp.get("/subaccounts")
def list_subaccounts():
    from ..models import Merchant as _M, Subaccount
    merchant = _auth()
    subs = (Subaccount.query.filter_by(platform_merchant_id=merchant.id)
            .order_by(Subaccount.id.desc()).all())
    managed = {m.id: m for m in _M.query.filter(
        _M.id.in_([s.merchant_id for s in subs])).all()} if subs else {}
    return jsonify(object="list", data=[
        dict(id=s.public_id, name=s.name, external_ref=s.external_ref,
             payout_phone=s.payout_phone,
             status="active" if managed.get(s.merchant_id) and managed[s.merchant_id].is_active else "inactive")
        for s in subs
    ])


@bp.get("/subaccounts/<public_id>")
def get_subaccount(public_id: str):
    from ..models import Merchant as _M, Subaccount
    merchant = _auth()
    sub = Subaccount.query.filter_by(
        public_id=public_id, platform_merchant_id=merchant.id).one_or_none()
    if sub is None:
        abort(404)
    managed = db.session.get(_M, sub.merchant_id)
    return jsonify(
        id=sub.public_id, name=sub.name, external_ref=sub.external_ref,
        payout_phone=sub.payout_phone,
        status="active" if managed and managed.is_active else "inactive",
        balances=_subaccount_balances(sub, mode=(g.api_mode == "test")),
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

    success_url = _safe_url(body.get("success_url"))
    cancel_url = _safe_url(body.get("cancel_url"))

    # Idempotency AFTER all validation: a retried POST /payment-links used to
    # silently create a duplicate link (+QR) the merchant found only when a
    # customer paid the wrong one. Honored when a key is sent.
    idem_key, request_hash, cached = _optional_idempotency(merchant, body)
    if cached is not None:
        return _replayed(cached[0], cached[1])

    link = PaymentLink(
        public_id=f"lnk_{_uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        currency=currency,
        description=str(body.get("description") or "")[:255] or None,
        reference=str(body.get("reference") or "")[:120] or None,
        success_url=success_url,
        cancel_url=cancel_url,
        allow_multiple_uses=bool(body.get("allow_multiple_uses", False)),
        is_test=(g.api_mode == "test"),
    )
    db.session.add(link)
    db.session.commit()

    log_event("payment_link.created", merchant_id=merchant.id, resource_id=link.public_id,
              detail={"amount": link.amount})
    out = dict(
        id=link.public_id,
        amount=link.amount,
        currency=link.currency,
        description=link.description,
        reference=link.reference,
        url=url_for("checkout.checkout_page", public_id=link.public_id, _external=True),
        # Same QR renderer vending orders use (checkout.py) — public, no auth,
        # cached hard. A plain link's URL never changes once created.
        qr_png_url=url_for("checkout.order_qr_png", public_id=link.public_id, _external=True),
        qr_svg_url=url_for("checkout.order_qr_svg", public_id=link.public_id, _external=True),
    )
    if idem_key:
        idempotency.store(merchant.id, idem_key, request_hash, 201, out)
    return jsonify(out), 201


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


# ---------- balance ----------

@bp.get("/balance")
@limiter.limit("60 per minute;1000 per hour")
def get_balance():
    """What Samsoftpay is holding for this merchant, per currency.

    Exists because a platform built on top of Samsoftpay (KarlPOS, TK Vending)
    keeps its own sub-ledger of what it owes each of ITS users, and needs to
    check that total against the cash actually held here. Without this endpoint
    that reconciliation cannot be automated at all — the asset side has to be
    typed in by hand.

    Two properties matter for that use, and drive the shape of this response:

    1. It reports the JOURNAL sum, not `cached_balance`. The cache is derived
       and can drift; a reconciliation endpoint that trusts a cache cannot
       detect the very thing it exists to detect. `cached` is returned
       alongside, and `consistent` says whether they agree — so a caller can
       tell "you hold less than I thought" apart from "your own books
       disagree with each other".

    2. It is mode-scoped like everything else. An sk_test_ key sees the sandbox
       ledger only. Sandbox balances are not real money and must never be
       reconciled against, so returning them under a live-looking figure would
       be worse than returning nothing.

    Merchant accounts are credit-normal — a positive balance owed to the
    merchant is stored negative — so the sign is flipped here, matching
    wallet.py. Amounts are minor units, like every other amount in this API.
    """
    merchant = _auth()
    is_test = (g.api_mode == "test")

    from sqlalchemy import func as safunc

    from ..models import Account, AccountType, JournalEntry

    accounts = Account.query.filter(
        Account.merchant_id == merchant.id,
        Account.is_test.is_(is_test),
        Account.type.in_([AccountType.MERCHANT_AVAILABLE, AccountType.MERCHANT_PENDING]),
    ).all()

    by_currency: dict[str, dict] = {}
    consistent = True

    for acct in accounts:
        journal_sum = int(
            db.session.query(safunc.coalesce(safunc.sum(JournalEntry.amount), 0))
            .filter(JournalEntry.account_id == acct.id)
            .scalar()
        )
        if journal_sum != int(acct.cached_balance):
            consistent = False

        slot = by_currency.setdefault(
            acct.currency,
            {"currency": acct.currency, "available": 0, "pending": 0, "cached": 0},
        )
        # Flip: liability accounts hold what we owe as a negative number.
        owed = -journal_sum
        if acct.type == AccountType.MERCHANT_AVAILABLE:
            slot["available"] = owed
        else:
            slot["pending"] = owed
        slot["cached"] += -int(acct.cached_balance)

    balances = []
    for slot in by_currency.values():
        slot["total"] = slot["available"] + slot["pending"]
        balances.append(slot)
    balances.sort(key=lambda b: b["currency"])

    log_event("balance.read", merchant_id=merchant.id,
              detail={"mode": g.api_mode, "currencies": [b["currency"] for b in balances]})

    return jsonify(
        mode=g.api_mode,
        balances=balances,
        # False means Samsoftpay's own cached balance disagrees with its journal.
        # The journal figures above are still authoritative; this is a signal that
        # something here needs investigating, not that the caller did anything wrong.
        consistent=consistent,
        as_of=int(time.time()),
    )


# ---------- webhook deliveries: log + self-service resend ----------
#
# Flutterwave lesson: a missed webhook must never need a support ticket.
# GET lists what we sent (or tried to); POST re-queues one delivery. Read-only
# plus re-notify — no money moves — so collections-scoped keys may use both
# (a kiosk that missed vending.dispensed needs the resend as much as anyone).

def _delivery_public(wh) -> dict:
    try:
        envelope = json.loads(wh.payload)
    except (ValueError, TypeError):
        envelope = {}
    return {
        "id": envelope.get("id"),               # evt_… — same id the receiver deduped on
        "event": envelope.get("event"),
        "url": wh.url,
        "status": wh.status,                    # pending | sent | failed
        "attempts": wh.attempts,
        "last_response_code": wh.last_response_code,
        "next_attempt_at": wh.next_attempt_at.isoformat() if wh.next_attempt_at and wh.status != "sent" else None,
        "created_at": wh.created_at.isoformat() if wh.created_at else None,
    }


@bp.get("/webhooks")
@limiter.limit("60 per minute")
def list_webhook_deliveries():
    merchant = _auth()
    from ..models import WebhookDelivery
    try:
        limit = min(max(int(request.args.get("limit", 25)), 1), 100)
    except ValueError:
        abort(400, description="limit must be an integer")
    q = WebhookDelivery.query.filter_by(merchant_id=merchant.id)
    status = request.args.get("status")
    if status:
        if status not in ("pending", "sent", "failed"):
            abort(400, description="status must be pending, sent or failed")
        q = q.filter_by(status=status)
    rows = q.order_by(WebhookDelivery.id.desc()).limit(limit).all()
    return jsonify(data=[_delivery_public(w) for w in rows], count=len(rows))


@bp.post("/webhooks/<event_id>/resend")
@limiter.limit("30 per minute")   # resend makes OUR worker POST outward — cap the amplification
def resend_webhook_delivery(event_id: str):
    _check_timestamp()
    merchant = _auth()
    from ..models import WebhookDelivery, utcnow
    from ..services.webhooks import resend_delivery
    import re as _re
    # Strict shape (hex only) — also keeps LIKE wildcards (%, _) out of the
    # payload match below.
    if not _re.fullmatch(r"evt_[0-9a-f]{1,32}", event_id):
        abort(400, description="event id must look like evt_…")
    # The envelope id is embedded in the signed payload (we sign exact bytes,
    # so it was never stored as a column). Scoped to this merchant's rows and
    # payloads are canonical JSON, so the quoted match is exact.
    wh = (WebhookDelivery.query
          .filter_by(merchant_id=merchant.id)
          .filter(WebhookDelivery.payload.contains(f'"id":"{event_id}"'))
          .first())
    if wh is None:
        abort(404, description="no webhook delivery with that event id "
                               "(deliveries are retained 30 days)")
    resend_delivery(wh)
    log_event("webhook.resend", merchant_id=merchant.id,
              detail={"event_id": event_id, "delivery_id": wh.id})
    return jsonify(ok=True, **_delivery_public(wh)), 202


# ---------- settlements (first-class: predictability is the product) ----------

@bp.get("/settlements")
def list_settlements():
    """Each row is one release of pending -> available for this merchant.

    Paystack lesson: merchants trust a settlement schedule they can set a
    watch by. Mode-scoped like /v1/balance — a test key sees sandbox sweeps.
    The published schedule: every charge becomes available 24h after it
    completes; the sweep runs hourly. If a settlement looks late by more than
    2 hours, contact support with the charge id.
    """
    merchant = _auth()
    from ..models import Settlement
    limit, created_after, created_before = _parse_list_params()
    q = Settlement.query.filter_by(
        merchant_id=merchant.id,
        is_test=(g.api_mode == "test"),
    )
    if created_after:
        q = q.filter(Settlement.created_at > created_after)
    if created_before:
        q = q.filter(Settlement.created_at < created_before)
    rows = q.order_by(Settlement.id.desc()).limit(limit).all()
    return jsonify(
        mode=g.api_mode,
        data=[dict(
            id=s.public_id,
            amount=s.amount,
            currency=s.currency,
            transactions=s.txn_count,
            kind=s.kind,
            created_at=s.created_at.isoformat() if s.created_at else None,
        ) for s in rows],
        count=len(rows),
    )
