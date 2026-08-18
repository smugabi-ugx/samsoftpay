"""Vending orders — the payment -> dispense bridge.

A vending order is an ordinary PaymentLink carrying machine context in
`vending_meta`. The customer scans the QR the machine displays, lands on the
normal hosted checkout, pays with Mobile Money. When that charge SUCCEEDS,
`maybe_dispense_on_success` fires and calls the supplier's ApplyExportGoods so
the machine drops the product.

Two rules this module exists to enforce:

1. **No payment, no dispense.** The only path to a dispense is a transaction in
   SUCCEEDED status that belongs to the order's merchant.
2. **A paid order dispenses exactly once.** The rail callback, the poller and a
   manual retry can all race. `_claim` is an atomic compare-and-set on
   `vending_status`, so exactly one caller ever wins the right to dispense.

The supplier's server is a third party over plain HTTP on their own infra. It
WILL be slow or down sometimes, and that must never corrupt a payment we have
already taken. Every entry point here is failure-tolerant: the money stays
recorded, the order is marked `failed` with the reason, and it can be retried.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import current_app

from ..extensions import db
from ..models import Merchant, PaymentLink, Transaction, TxnStatus, VendingMachine
from . import xy_vending

# vending_status lifecycle
PENDING = "pending"        # order created, awaiting payment
DISPENSING = "dispensing"  # claimed by one worker, supplier call in flight
DISPENSED = "dispensed"    # supplier accepted — product released
FAILED = "failed"          # supplier rejected/unreachable — retryable


class VendingError(Exception):
    pass


# ---------- order metadata ----------

def build_meta(*, machine: str, goods: list[dict], pay_account: str | None = None) -> str:
    """Normalise and serialise the machine context stored on the link.

    `goods` items are passed to the supplier as-is (spbh product number, spmc
    name, spdj unit price in the supplier's own unit) — we do not reinterpret
    their pricing, we only carry it.
    """
    clean = []
    for g in goods:
        spbh = str(g.get("spbh") or "").strip()
        if not spbh:
            raise VendingError("each goods item needs an spbh (product number)")
        clean.append({
            "spbh": spbh,
            "spmc": str(g.get("spmc") or "").strip(),
            "spdj": g.get("spdj", ""),
        })
    if not clean:
        raise VendingError("goods list is required")
    return json.dumps({
        "machine": str(machine),
        "goods": clean,
        "pay_account": (pay_account or "").strip() or None,
    }, separators=(",", ":"))


def read_meta(link: PaymentLink) -> dict | None:
    if not link or not link.vending_meta:
        return None
    try:
        return json.loads(link.vending_meta)
    except (TypeError, ValueError):
        return None


def is_vending_order(link: PaymentLink) -> bool:
    return read_meta(link) is not None


# ---------- machines ----------

def machines_for(merchant: Merchant) -> list[VendingMachine]:
    return (VendingMachine.query
            .filter_by(merchant_id=merchant.id)
            .order_by(VendingMachine.name, VendingMachine.jqbh)
            .all())


def owns_machine(merchant: Merchant, jqbh: str) -> bool:
    """True if the merchant may address this machine.

    A merchant who has never synced has no registry, and we do not want to lock
    them out of a working integration — so an empty registry permits anything.
    Once they have synced, the registry is authoritative.
    """
    if VendingMachine.query.filter_by(merchant_id=merchant.id).first() is None:
        return True
    return VendingMachine.query.filter_by(
        merchant_id=merchant.id, jqbh=str(jqbh)
    ).first() is not None


def sync_machines(merchant: Merchant) -> tuple[int, int]:
    """Pull this merchant's machines from the supplier into our registry.

    Returns (added, updated). Machines that vanish upstream are marked inactive
    rather than deleted, so historical orders still resolve to something.
    """
    creds = xy_vending.for_merchant(merchant)
    if not creds.configured:
        raise VendingError("no XY credentials configured for this merchant")

    resp = xy_vending.query_machines(creds=creds)
    if str(resp.get("code")) != "1":
        raise VendingError(f"supplier rejected the machine query: {resp.get('message') or resp}")

    data = resp.get("data") or []
    if isinstance(data, dict):        # the doc's example returns a bare object
        data = [data]

    now = datetime.now(timezone.utc)
    seen, added, updated = set(), 0, 0
    for m in data:
        jqbh = str(m.get("jqbh") or "").strip()
        if not jqbh:
            continue
        seen.add(jqbh)
        row = VendingMachine.query.filter_by(merchant_id=merchant.id, jqbh=jqbh).one_or_none()
        if row is None:
            row = VendingMachine(merchant_id=merchant.id, jqbh=jqbh)
            db.session.add(row)
            added += 1
        else:
            updated += 1
        row.name = str(m.get("jqmc") or "").strip()[:160] or None
        row.category = str(m.get("jqlb") or "").strip()[:40] or None
        row.machine_type = str(m.get("jqlx") or "").strip()[:40] or None
        row.address = str(m.get("dwmc") or "").strip()[:255] or None
        row.latitude = str(m.get("dwwd") or "").strip()[:40] or None
        row.longitude = str(m.get("dwjd") or "").strip()[:40] or None
        row.image_url = str(m.get("jqtp") or "").strip()[:500] or None
        row.is_active = True
        row.last_synced_at = now

    # Anything we knew about that the supplier no longer lists.
    for row in VendingMachine.query.filter_by(merchant_id=merchant.id).all():
        if row.jqbh not in seen:
            row.is_active = False

    db.session.commit()
    return added, updated


def machine_dict(m: VendingMachine) -> dict:
    return {
        "machine": m.jqbh,
        "name": m.name,
        "address": m.address,
        "category": m.category,
        "type": m.machine_type,
        "latitude": m.latitude,
        "longitude": m.longitude,
        "active": m.is_active,
        "last_synced_at": m.last_synced_at.isoformat() if m.last_synced_at else None,
    }


# ---------- order creation ----------

def create_order(*, merchant: Merchant, machine: str, goods: list[dict],
                 amount: int, currency: str = "UGX", reference: str | None = None,
                 pay_account: str | None = None, description: str | None = None,
                 success_url: str | None = None, is_test: bool = False) -> PaymentLink:
    """Create a vending order (a one-shot payment link + machine context)."""
    import uuid

    if not getattr(merchant, "vending_enabled", False):
        raise VendingError("vending is not enabled for this merchant")
    if not machine:
        raise VendingError("machine is required")
    if not isinstance(amount, int) or amount <= 0:
        raise VendingError("amount must be a positive integer")
    # If the merchant has synced their machines, the order must name one of
    # theirs. Catches a typo'd machine number before a customer pays for a
    # dispense that can never happen, and stops one operator addressing
    # another's machine. Merchants who have not synced are not blocked.
    if not owns_machine(merchant, machine):
        raise VendingError(f"machine {machine} is not registered to this merchant")

    meta = build_meta(machine=machine, goods=goods, pay_account=pay_account)
    names = ", ".join(g.get("spmc") or g.get("spbh") for g in json.loads(meta)["goods"])

    link = PaymentLink(
        public_id=f"lnk_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        currency=currency,
        description=description or (names[:255] if names else "Vending purchase"),
        reference=reference,
        success_url=success_url,
        allow_multiple_uses=False,   # one order, one dispense
        is_active=True,
        vending_meta=meta,
        vending_status=PENDING,
        is_test=is_test,
    )
    db.session.add(link)
    db.session.commit()
    return link


# ---------- dispense ----------

def _claim(link_id: int) -> bool:
    """Atomically move a link from `pending` to `dispensing`.

    Returns True only for the caller that won. This is the whole double-dispense
    guard: a single UPDATE ... WHERE vending_status='pending' is atomic in both
    Postgres and SQLite, so concurrent callbacks cannot both see `pending`.
    """
    updated = (
        db.session.query(PaymentLink)
        .filter(PaymentLink.id == link_id, PaymentLink.vending_status == PENDING)
        .update({"vending_status": DISPENSING}, synchronize_session=False)
    )
    db.session.commit()
    return bool(updated)


def _finish(link_id: int, *, ok: bool, error: str | None = None) -> None:
    link = db.session.get(PaymentLink, link_id)
    if link is None:
        return
    previous = link.vending_status
    if ok:
        link.vending_status = DISPENSED
        link.vending_error = None
        link.vending_dispensed_at = datetime.now(timezone.utc)
    else:
        link.vending_status = FAILED
        link.vending_error = (error or "unknown")[:255]
    db.session.commit()
    # Only announce a real change of outcome. The supplier's result callback can
    # confirm an order we already marked dispensed, and a platform does not want
    # the same "dispensed" event twice for one soda.
    if link.vending_status != previous:
        _emit_event(link)


def _emit_event(link: PaymentLink) -> None:
    """Tell the merchant's backend what the machine did. Never raises.

    This is the event a platform actually needs: `charge.succeeded` says the
    money arrived, which is NOT the same as the product coming out. A jammed
    tray produces a succeeded charge and a `vending.dispense_failed`.
    """
    try:
        from .webhooks import enqueue

        merchant = db.session.get(Merchant, link.merchant_id)
        meta = read_meta(link) or {}
        event = ("vending.dispensed" if link.vending_status == DISPENSED
                 else "vending.dispense_failed")
        enqueue(
            merchant,
            event,
            {
                "order_id": link.public_id,
                "machine": meta.get("machine"),
                "goods": meta.get("goods"),
                "amount": link.amount,
                "currency": link.currency,
                "charge_id": _charge_public_id(link),
                "reference": link.reference,
                "vending_status": link.vending_status,
                "error": link.vending_error,
                "dispensed_at": (link.vending_dispensed_at.isoformat()
                                 if link.vending_dispensed_at else None),
            },
            transaction_id=link.transaction_id,
        )
    except Exception as exc:  # pragma: no cover - defensive
        try:
            current_app.logger.warning("vending webhook enqueue failed: %s", exc)
        except Exception:
            pass


def _charge_public_id(link: PaymentLink) -> str | None:
    if not link.transaction_id:
        return None
    txn = db.session.get(Transaction, link.transaction_id)
    return txn.public_id if txn else None


def dispense_for_link(link: PaymentLink, txn: Transaction) -> bool:
    """Call the supplier for an already-paid order. Returns True if dispensed.

    Assumes the caller has verified the payment. Safe to call concurrently —
    only the caller that wins `_claim` talks to the supplier.
    """
    meta = read_meta(link)
    if meta is None:
        return False
    if not _claim(link.id):
        # Someone else is dispensing (or it already dispensed). Not an error.
        return False

    link_id = link.id
    # The payer account the supplier records: prefer whoever actually paid.
    pay_account = (txn.customer_phone or meta.get("pay_account")
                   or f"merchant-{txn.merchant_id}")
    # Always the OWNING merchant's own supplier credentials — never a global
    # account. This is what keeps two operators on the platform isolated.
    merchant = db.session.get(Merchant, txn.merchant_id)
    try:
        xy_vending.apply_export_goods(
            jqbh=str(meta["machine"]),
            order_id=link.public_id,
            third_party_txn_id=txn.public_id,
            pay_account=str(pay_account),
            goods=meta["goods"],
            creds=xy_vending.for_merchant(merchant),
        )
    except Exception as exc:  # supplier down, rejected, non-JSON, anything
        _finish(link_id, ok=False, error=str(exc))
        _log("vending.dispense_failed", txn, link_id, error=str(exc))
        return False

    _finish(link_id, ok=True)
    _log("vending.dispensed", txn, link_id, machine=str(meta["machine"]))
    return True


def maybe_dispense_on_success(txn: Transaction) -> None:
    """Hook called from the charge-completion path. Never raises.

    A failure here must not roll back or obscure a payment that really happened,
    so everything is swallowed and recorded on the order instead.
    """
    try:
        if txn is None or txn.status != TxnStatus.SUCCEEDED:
            return
        link = PaymentLink.query.filter_by(transaction_id=txn.id).one_or_none()
        if link is None and txn.merchant_reference:
            # The checkout attaches the transaction to the link a moment after
            # create_charge returns. If a rail callback beats that commit, fall
            # back to the reference the charge was tagged with.
            link = PaymentLink.query.filter_by(
                public_id=txn.merchant_reference, merchant_id=txn.merchant_id
            ).one_or_none()
        if link is None or not is_vending_order(link):
            return
        merchant = db.session.get(Merchant, txn.merchant_id)
        if not merchant or not getattr(merchant, "vending_enabled", False):
            _finish(link.id, ok=False, error="vending disabled for this merchant")
            return
        dispense_for_link(link, txn)
    except Exception as exc:  # pragma: no cover - defensive
        try:
            current_app.logger.exception("vending auto-dispense failed: %s", exc)
        except Exception:
            pass


def retry_dispense(link: PaymentLink) -> bool:
    """Put a failed order back to `pending` and try again.

    Only for orders whose charge actually succeeded — the payment guard is
    re-checked here so a retry can never become a free dispense.
    """
    if link.transaction_id is None:
        raise VendingError("order has no payment yet")
    txn = db.session.get(Transaction, link.transaction_id)
    if txn is None or txn.status != TxnStatus.SUCCEEDED:
        raise VendingError("cannot dispense: charge has not succeeded")
    if link.vending_status == DISPENSED:
        return True
    if link.vending_status == DISPENSING:
        # A supplier call is in flight RIGHT NOW. Resetting to PENDING here
        # would bypass the _claim compare-and-set and allow a second
        # concurrent dispense of the same paid order.
        raise VendingError("a dispense attempt is already in progress — wait for it to finish")
    link.vending_status = PENDING
    db.session.commit()
    return dispense_for_link(link, txn)


def _log(event: str, txn: Transaction, link_id: int, **detail) -> None:
    try:
        from .audit import log_event
        log_event(event, merchant_id=txn.merchant_id, resource_id=txn.public_id,
                  detail={"link_id": link_id, **detail})
    except Exception:
        pass


# ---------- QR rendering ----------

def qr_image(content: str, *, kind: str = "svg", scale: int = 8,
             border: int = 2, dark: str = "#0f0c29") -> bytes:
    """Render `content` as a QR code.

    Used for the code the machine screen shows. segno is pure Python (no Pillow,
    no C extension), so this works on Render's slim image with nothing to build.
    Error correction M survives a scratched or partly-glared machine screen.
    """
    import io

    import segno

    buf = io.BytesIO()
    segno.make(content, error="m").save(
        buf, kind=kind, scale=scale, border=border, dark=dark
    )
    return buf.getvalue()


# ---------- read model for the machine / dashboard ----------

def order_state(link: PaymentLink) -> dict:
    """What the machine polls: is it paid, and did it dispense?"""
    meta = read_meta(link) or {}
    txn = db.session.get(Transaction, link.transaction_id) if link.transaction_id else None
    return {
        "order_id": link.public_id,
        "machine": meta.get("machine"),
        "amount": link.amount,
        "currency": link.currency,
        "payment_status": txn.status.value if txn else "unpaid",
        "charge_id": txn.public_id if txn else None,
        "vending_status": link.vending_status,
        "vending_error": link.vending_error,
        "dispensed_at": link.vending_dispensed_at.isoformat() if link.vending_dispensed_at else None,
    }
