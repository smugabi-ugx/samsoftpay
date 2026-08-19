"""Public-facing hosted checkout pages.

This is the customer experience: a merchant creates a PaymentLink, sends the URL
to a customer, customer arrives at /pay/<link_id>, fills in their phone, picks a
channel, pays. We then poll for completion and show a success/failure page.

These routes are PUBLIC — no API key required. They're how non-technical
merchants (small shops, schools, churches) use the gateway by just sharing a link.
"""
from __future__ import annotations

import json
import uuid

from flask import Blueprint, abort, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Channel, Merchant, PaymentLink, Transaction, TxnStatus, utcnow
from ..services.orchestrator import OrchestratorError, create_charge

bp = Blueprint("checkout", __name__)


@bp.get("/pay/@<handle>")
def merchant_profile(handle: str):
    """Public merchant profile — goes in TikTok bio, Instagram link, etc."""
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    links = (
        PaymentLink.query
        .filter_by(merchant_id=merchant.id, is_active=True)
        .order_by(PaymentLink.created_at.desc())
        .limit(12)
        .all()
    )
    return render_template("merchant_profile.html", merchant=merchant, links=links)


@bp.get("/pay/@<handle>/qr.png")
def profile_qr_png(handle: str):
    """QR of a merchant's public profile URL, rendered in-house with segno —
    the profile page's QR modal used a third-party QR service before."""
    from flask import Response
    from ..services.vending import qr_image
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    target = url_for("checkout.merchant_profile", handle=handle, _external=True)
    data = qr_image(target, kind="png", scale=6)
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@bp.get("/pay/@<handle>/pay")
def profile_pay(handle: str):
    """Create a one-shot payment link from the profile page custom-amount form."""
    import uuid as _uuid
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    try:
        amount = int(request.args.get("amount", 0))
    except ValueError:
        amount = 0
    if amount < 500:
        return redirect(url_for("checkout.merchant_profile", handle=handle))

    link = PaymentLink(
        public_id=f"lnk_{_uuid.uuid4().hex[:16]}",
        merchant_id=merchant.id,
        amount=amount,
        currency="UGX",
        description=f"Payment to {merchant.name}",
        allow_multiple_uses=False,
        is_active=True,
    )
    db.session.add(link)
    db.session.commit()
    return redirect(url_for("checkout.checkout_page", public_id=link.public_id))


@bp.get("/pay/<public_id>")
def checkout_page(public_id: str):
    """The customer-facing payment page."""
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None:
        abort(404)

    merchant = db.session.get(Merchant, link.merchant_id)

    # If a single-use link has already been paid, redirect to status.
    if link.transaction_id and not link.allow_multiple_uses:
        return redirect(url_for("checkout.status_page", public_id=public_id))

    if not link.is_active:
        return render_template(
            "checkout_inactive.html", link=link, merchant=merchant
        )

    from flask import g, session
    # No auth on this route — link.is_test (set at creation from the key that
    # made it) is the only record of which mode this checkout runs in. Set it
    # BEFORE listing channels so the page never offers a method that
    # checkout_submit would then refuse — see orchestrator._simulated_rail_forbidden.
    g.api_mode = "test" if link.is_test else "live"
    voucher_data = session.get(f"voucher_{public_id}", {})
    prefill = session.pop(f"checkout_prefill_{public_id}", {})
    return render_template(
        "checkout.html",
        link=link,
        merchant=merchant,
        channels=_channel_options(include_crypto=True),
        crypto_url=url_for("checkout.crypto_checkout", public_id=link.public_id),
        voucher_applied=bool(voucher_data),
        voucher_discount=voucher_data.get("discount", 0),
        prefill=prefill,
    )


@bp.post("/pay/<public_id>/submit")
def checkout_submit(public_id: str):
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None:
        abort(404)
    if not link.is_active:
        abort(400, description="payment link is not active")
    if link.transaction_id and not link.allow_multiple_uses:
        return redirect(url_for("checkout.status_page", public_id=public_id))

    merchant = db.session.get(Merchant, link.merchant_id)

    from flask import g
    # Scope this whole submission to the mode the link was CREATED under, not
    # a hardcoded "live" — see PaymentLink.is_test. Set before anything below
    # touches channel filtering or create_charge.
    g.api_mode = "test" if link.is_test else "live"

    try:
        channel = Channel(request.form.get("channel", ""))
    except ValueError:
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=_channel_options(include_crypto=True),
            error="Please choose a payment method.",
        )

    customer_phone = (request.form.get("phone") or "").strip()
    customer_email = (request.form.get("email") or "").strip() or None

    # Minimal validation — channel-appropriate identifier present
    if channel in (Channel.MTN_MOMO, Channel.AIRTEL_MONEY) and not customer_phone:
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=_channel_options(include_crypto=True),
            error="Phone number is required for mobile money.",
            selected_channel=channel.value,
        )

    from flask import session

    # Gift card discount — VALIDATED here (dry_run), REDEEMED only once we
    # know a charge attempt will actually go through. Redeeming eagerly and
    # only then trying to charge left a real gap: if create_charge is
    # rejected below (a blocked channel, an inactive merchant), the gift
    # card's balance was already gone with nothing delivered for it.
    charge_amount = link.amount
    voucher_code = None
    discount = 0
    voucher_data = session.get(f"voucher_{public_id}", {})
    # A sandbox checkout can never redeem a real gift card — see apply_voucher.
    # This should already be unreachable (apply_voucher refuses to store one),
    # but a voucher placed in the session before that guard existed, or a
    # differently-crafted request, must not get a second chance here.
    if voucher_data and not link.is_test:
        from ..services.giftcards import redeem_gift_card
        voucher_code = voucher_data.get("code")
        discount = voucher_data.get("discount", 0)
        ok, msg, _ = redeem_gift_card(voucher_code, discount, merchant_id=merchant.id, dry_run=True)
        if not ok:
            # Balance moved since the preview (race, or deactivated meanwhile)
            # — drop the stale voucher rather than silently charge full price.
            voucher_code = None
            discount = 0
            session.pop(f"voucher_{public_id}", None)
        else:
            charge_amount = max(0, link.amount - discount)

    if charge_amount == 0:
        # Fully covered by gift card — no rail is ever involved, so there is
        # no "the charge might still fail" risk; safe to redeem for real now.
        from ..services.giftcards import redeem_gift_card
        ok, msg, _ = redeem_gift_card(voucher_code, discount, merchant_id=merchant.id)
        if not ok:
            session.pop(f"voucher_{public_id}", None)
            return render_template(
                "checkout.html", link=link, merchant=merchant,
                channels=_channel_options(include_crypto=True), voucher_error=msg,
            )
        session.pop(f"voucher_{public_id}", None)

        txn_obj = Transaction(
            public_id=f"txn_{uuid.uuid4().hex[:16]}",
            merchant_id=merchant.id,
            amount=link.amount, fee_amount=0,
            currency=link.currency, channel=channel,
            status=TxnStatus.SUCCEEDED, is_test=link.is_test,
            completed_at=utcnow(),
            # settled_at MUST be set: a fully-gift-card-covered charge collects
            # NO rail money and posts NOTHING to the ledger, but it is a SUCCEEDED
            # txn with settled_at=None. The 24h settlement sweep selects exactly
            # (SUCCEEDED, settled_at IS NULL, aged) and would then post
            # (pending +total, available -total) for it — minting a withdrawable
            # balance that no rail ever funded (real money out via /v1/payouts).
            # Marking it settled at creation keeps the sweep's hands off it; the
            # gift-card value is a merchant liability tracked on GiftCard.balance,
            # not real collected money that belongs in the withdrawable ledger.
            settled_at=utcnow(),
            merchant_reference=link.reference or link.public_id,
        )
        db.session.add(txn_obj)
        db.session.flush()   # txn_obj.id is only assigned by the DB on flush —
                              # reading it immediately after add() without one is
                              # not reliably safe. This bug existed in the code
                              # before this change too, just intermittently.
        link.transaction_id = txn_obj.id
        db.session.commit()

        # Same side effects a rail-collected SUCCEEDED charge gets — a fully
        # gift-card-covered vending order must still dispense, and the
        # merchant's webhook consumer must still see it, exactly as if MTN
        # had actually collected the money.
        from ..services.vending import maybe_dispense_on_success
        maybe_dispense_on_success(txn_obj)
        from ..services.webhooks import enqueue
        enqueue(merchant, "charge.succeeded", {
            "id": txn_obj.public_id, "amount": txn_obj.amount, "fee": txn_obj.fee_amount,
            "currency": txn_obj.currency, "channel": txn_obj.channel.value,
            "status": txn_obj.status.value, "merchant_reference": txn_obj.merchant_reference,
            "failure_reason": None, "completed_at": txn_obj.completed_at.isoformat(),
        }, transaction_id=txn_obj.id)

        return redirect(url_for("checkout.status_page", public_id=public_id))

    # Vending orders always carry the link id as the reference: it is how a
    # succeeded charge finds its way back to the machine that must dispense.
    from ..services.vending import is_vending_order
    charge_reference = (
        link.public_id if is_vending_order(link) else (link.reference or link.public_id)
    )

    try:
        txn = create_charge(
            merchant=merchant,
            amount=charge_amount,
            currency=link.currency,
            channel=channel,
            customer_phone=customer_phone or None,
            customer_email=customer_email,
            merchant_reference=charge_reference,
        )
    except OrchestratorError as exc:
        # The gift card, if any, was only validated (dry_run) above — never
        # redeemed — so there is nothing to roll back here.
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=_channel_options(include_crypto=True),
            error=f"Could not start payment: {exc}",
            selected_channel=channel.value,
        )

    # Attach the transaction to the link ATOMICALLY (WHERE transaction_id IS
    # NULL) — the plain read-then-set let two concurrent submits on a
    # single-use link both attach, with the loser's payment invisible on the
    # status page. The loser's charge still exists as a Transaction record
    # for support/reconciliation; exactly one owns the link.
    if not link.transaction_id:
        db.session.query(PaymentLink).filter(
            PaymentLink.id == link.id,
            PaymentLink.transaction_id.is_(None),
        ).update({"transaction_id": txn.id}, synchronize_session=False)
        db.session.commit()

    # Only now — after create_charge has NOT raised — actually redeem the
    # discount. create_charge can also return normally with status FAILED
    # (a synchronous rail rejection, e.g. a malformed phone number) without
    # raising OrchestratorError at all, so that must be checked too, or the
    # gift card would still be burned for a charge that never went anywhere.
    if voucher_code and discount and txn.status != TxnStatus.FAILED:
        from ..services.giftcards import redeem_gift_card
        ok, redeem_msg, _ = redeem_gift_card(voucher_code, discount, merchant_id=merchant.id)
        if ok:
            session.pop(f"voucher_{public_id}", None)
        else:
            # Extremely rare: the balance changed between the dry_run check
            # above and now. The charge itself already succeeded/is in
            # flight — never fail a real payment over a discount-bookkeeping
            # race; log it for manual follow-up instead.
            from flask import current_app
            current_app.logger.error(
                "gift card %s could not be redeemed after charge %s: %s",
                voucher_code, txn.public_id, redeem_msg,
            )

    return redirect(url_for("checkout.status_page", public_id=public_id))


@bp.get("/pay/<public_id>/status")
def status_page(public_id: str):
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or link.transaction_id is None:
        abort(404)
    txn = db.session.get(Transaction, link.transaction_id)
    merchant = db.session.get(Merchant, link.merchant_id)
    return render_template(
        "checkout_status.html", link=link, txn=txn, merchant=merchant
    )


@bp.get("/pay/<public_id>/status.json")
def status_json(public_id: str):
    """JSON endpoint the status page polls every couple of seconds."""
    from flask import jsonify
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or link.transaction_id is None:
        abort(404)
    txn = db.session.get(Transaction, link.transaction_id)
    return jsonify(
        status=txn.status.value,
        amount=txn.amount,
        currency=txn.currency,
        channel=txn.channel.value,
        failure_reason=txn.failure_reason,
    )


# ── QR codes + vending machine display ─────────────────────────────────

def _qr_response(public_id: str, kind: str):
    """Render the checkout URL for a link as a QR image.

    Public on purpose: it encodes nothing secret, just the same /pay/<id> URL
    the customer would type. Cached hard — a link's URL never changes.
    """
    from flask import Response
    from ..services.vending import qr_image

    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None:
        abort(404)
    target = url_for("checkout.checkout_page", public_id=public_id, _external=True)
    data = qr_image(target, kind=kind, scale=10 if kind == "png" else 8)
    mime = "image/png" if kind == "png" else "image/svg+xml"
    return Response(data, mimetype=mime,
                    headers={"Cache-Control": "public, max-age=86400"})


@bp.get("/pay/<public_id>/qr.svg")
def order_qr_svg(public_id: str):
    return _qr_response(public_id, "svg")


@bp.get("/pay/<public_id>/qr.png")
def order_qr_png(public_id: str):
    return _qr_response(public_id, "png")


@bp.get("/vending/display/<public_id>")
def vending_display(public_id: str):
    """Full-screen 'scan to pay' page for the vending machine's own screen.

    The machine opens this URL for the order. It shows the QR, then flips to a
    paid/dispensing/collect state on its own by polling — so the machine's UI
    needs no logic beyond opening a browser at this address.
    """
    from ..services.vending import is_vending_order, read_meta

    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or not is_vending_order(link):
        abort(404)
    merchant = db.session.get(Merchant, link.merchant_id)
    return render_template(
        "vending_display.html",
        link=link,
        merchant=merchant,
        meta=read_meta(link) or {},
        pay_url=url_for("checkout.checkout_page", public_id=public_id, _external=True),
    )


@bp.get("/vending/display/<public_id>/state.json")
def vending_display_state(public_id: str):
    """State feed for the machine screen — payment + dispense in one call.

    Public (no API key): it exposes only this one order's status, which the
    person standing at the machine can already see. It also nudges a missed
    dispense, so the screen recovers on its own if the hook was skipped.
    """
    from flask import jsonify
    from ..models import TxnStatus
    from ..services import vending

    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or not vending.is_vending_order(link):
        abort(404)

    if link.vending_status == vending.PENDING and link.transaction_id:
        txn = db.session.get(Transaction, link.transaction_id)
        if txn is not None and txn.status == TxnStatus.SUCCEEDED:
            vending.dispense_for_link(link, txn)
            db.session.refresh(link)

    state = vending.order_state(link)
    return jsonify(
        payment_status=state["payment_status"],
        vending_status=state["vending_status"],
        vending_error=state["vending_error"],
    )


def _channel_options(include_crypto: bool = False):
    """Payment methods to show a customer — simulated rails removed.

    Airtel and Card have no real rail yet. create_charge refuses them for live
    payments, so offering them here would only walk the customer into an error
    (and at a vending machine, into a product that never drops). Filtering the
    list is the UX half of that guard; the orchestrator remains the real one.
    """
    options = [
        ("mtn_momo",     "MTN Mobile Money",       "phone"),
        ("airtel_money", "Airtel Money",            "phone"),
        ("card",         "Visa / Mastercard",       "card"),
    ]
    if include_crypto:
        options.append(("crypto", "Crypto (BTC/ETH/USDT…)", "crypto"))

    from ..services.orchestrator import _simulated_rail_forbidden

    available = []
    for value, label, kind in options:
        try:
            if _simulated_rail_forbidden(Channel(value)):
                continue
        except Exception:      # unknown channel / no app context — show it
            pass
        available.append((value, label, kind))
    return available


# ── Crypto checkout (ChangeNow) ────────────────────────────────────────

@bp.post("/pay/<public_id>/apply-voucher")
def apply_voucher(public_id: str):
    """Validate a gift card code and store the discount in the session."""
    from flask import session
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or not link.is_active:
        abort(404)
    code = request.form.get("code", "").strip().upper()
    merchant = db.session.get(Merchant, link.merchant_id)

    # Gift cards are dashboard-issued real merchant liability — they carry no
    # test/live mode of their own (unlike PaymentLink.is_test). A sandbox
    # checkout must therefore never be allowed to touch one at all, rather
    # than trying to give gift cards their own sandbox split.
    if link.is_test:
        return render_template("checkout.html", link=link, merchant=merchant,
                               channels=_channel_options(include_crypto=True),
                               voucher_error="Gift cards can't be used on a sandbox (test-mode) checkout.")

    from datetime import datetime, timezone

    # Peek at the card without redeeming yet — dry_run shares the exact same
    # validation the real redemption uses, so this preview can never approve
    # a code that redemption later rejects.
    from ..services.giftcards import redeem_gift_card
    ok, msg, card = redeem_gift_card(code, 0, merchant_id=link.merchant_id, dry_run=True)
    error = None if ok else msg

    if error:
        return render_template("checkout.html", link=link, merchant=merchant,
                               channels=_channel_options(include_crypto=True),
                               crypto_url=url_for("checkout.crypto_checkout", public_id=public_id),
                               voucher_error=error)
    discount = min(card.balance, link.amount)
    session[f"voucher_{public_id}"] = {"code": code, "discount": discount}
    # Carry the customer's already-typed phone/email through the voucher round
    # trip (the voucher form posts them as hidden fields) so they aren't wiped
    # when we redirect back to the checkout page.
    session[f"checkout_prefill_{public_id}"] = {
        "phone": request.form.get("phone", "").strip(),
        "email": request.form.get("email", "").strip(),
    }
    return redirect(url_for("checkout.checkout_page", public_id=public_id))


@bp.get("/pay/<public_id>/crypto")
def crypto_checkout(public_id: str):
    from ..services.changenow import SUPPORTED_COINS
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or not link.is_active:
        abort(404)
    merchant = db.session.get(Merchant, link.merchant_id)
    # Check if an exchange is already in progress (stored in session)
    from flask import session
    if request.args.get("restart"):
        # The rate-lock expiry escape hatch. Without this, "start over" simply
        # re-served the SAME expired session order — an infinite loop.
        session.pop(f"cn_order_{public_id}", None)
        session.pop(f"cn_status_{public_id}", None)
        return redirect(url_for("checkout.crypto_checkout", public_id=public_id))
    order_data = session.get(f"cn_order_{public_id}")
    order = type("O", (), order_data)() if order_data else None
    return render_template(
        "checkout_crypto.html",
        link=link, merchant=merchant,
        coins=SUPPORTED_COINS,
        order=order,
        # Set when crypto_initiate bounced back after ChangeNow rejected the
        # exchange — without it the failure was completely silent.
        error="Could not start the exchange. Please try again in a moment." if request.args.get("err") else None,
    )


@bp.get("/pay/<public_id>/crypto/qr.png")
def crypto_qr_png(public_id: str):
    """QR of THIS session's deposit address, rendered in-house with segno.

    Replaces a third-party QR service: a payment QR must not depend on
    someone else's uptime, and the deposit address should not be shipped to
    an external host. Session-scoped, so it can only render the address that
    was already shown to this same browser.
    """
    from flask import Response, session
    from ..services.vending import qr_image

    order_data = session.get(f"cn_order_{public_id}")
    if not order_data or not order_data.get("deposit_address"):
        abort(404)
    data = qr_image(order_data["deposit_address"], kind="png", scale=6)
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=1200"})


@bp.post("/pay/<public_id>/crypto/initiate")
def crypto_initiate(public_id: str):
    from flask import session
    from ..services.changenow import create_exchange
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or not link.is_active:
        abort(404)
    from_coin = request.form.get("coin", "usdtbsc")
    result = create_exchange(
        from_coin=from_coin,
        amount_ugx=link.amount,
        public_id=public_id,
    )
    if not result.accepted:
        # Back to the CRYPTO page with an error flag — bouncing silently to the
        # main checkout left the customer with no idea anything went wrong.
        return redirect(url_for("checkout.crypto_checkout", public_id=public_id, err=1))

    session[f"cn_order_{public_id}"] = {
        "exchange_id": result.exchange_id,
        "deposit_address": result.deposit_address,
        "deposit_coin": result.deposit_coin,
        "deposit_amount_estimate": result.deposit_amount_estimate,
    }
    session[f"cn_status_{public_id}"] = "waiting"
    return redirect(url_for("checkout.crypto_checkout", public_id=public_id))


@bp.get("/pay/<public_id>/crypto/status.json")
def crypto_status_json(public_id: str):
    from flask import jsonify, session
    from ..services.changenow import get_status
    order_data = session.get(f"cn_order_{public_id}")
    if not order_data:
        return jsonify(status="waiting")
    exchange_id = order_data.get("exchange_id", "")
    status = get_status(exchange_id)
    session[f"cn_status_{public_id}"] = status
    # If finished, create the transaction record.
    # The settle-once guard is SERVER-SIDE: link.transaction_id under a row
    # lock. The old guard was a client-held session cookie (cn_settled_*) —
    # replaying an older cookie, or two concurrent polls, re-ran create_charge
    # and credited the ledger again for the same exchange.
    if status == "finished" and not session.get(f"cn_settled_{public_id}"):
        link = (PaymentLink.query.filter_by(public_id=public_id)
                .with_for_update().one_or_none())
        if link and link.transaction_id:
            # Already settled (by this session or any other) — just mirror it.
            session[f"cn_settled_{public_id}"] = True
            db.session.commit()
        elif link:
            # Deliberately NOT gated on link.is_active: the exchange is
            # FINISHED — real crypto arrived. A merchant deactivating the
            # link must not make received money vanish unrecorded.
            merchant = db.session.get(Merchant, link.merchant_id)
            from flask import g
            g.api_mode = "test" if link.is_test else "live"
            try:
                txn = create_charge(
                    merchant=merchant,
                    amount=link.amount,
                    currency=link.currency,
                    channel=Channel.CRYPTO,
                    customer_phone=None,
                    customer_email=None,
                    merchant_reference=exchange_id,
                )
                link.transaction_id = txn.id
                db.session.commit()
                session[f"cn_settled_{public_id}"] = True
            except Exception:
                # The customer has sent REAL crypto by this point. Swallowing
                # this silently left link.transaction_id None and the status
                # page 404ing after payment. Log loudly, roll back cleanly,
                # and report "exchanging" so the page keeps polling — the next
                # poll retries charge creation instead of dead-ending.
                db.session.rollback()
                from flask import current_app
                current_app.logger.exception(
                    "crypto settle failed for link %s exchange %s — will retry on next poll",
                    public_id, exchange_id,
                )
                return jsonify(status="exchanging")
    return jsonify(status=status)
