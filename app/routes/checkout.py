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

from ..extensions import db, limiter
from ..models import Channel, Merchant, PaymentLink, Transaction, TxnStatus, utcnow
from ..services.orchestrator import OrchestratorError, create_charge

bp = Blueprint("checkout", __name__)

# Public profile shows a short first page of items; the rest load on demand.
_PROFILE_PAGE = 6


@bp.get("/pay/@<handle>")
def merchant_profile(handle: str):
    """Public merchant profile — goes in TikTok bio, Instagram link, etc."""
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    # Show a short first page (6) and let the visitor load more — a merchant
    # with a long product history rendered a wall of items nobody scrolled.
    rows = (
        PaymentLink.query
        .filter_by(merchant_id=merchant.id, is_active=True)
        .order_by(PaymentLink.created_at.desc())
        .limit(_PROFILE_PAGE + 1)
        .all()
    )
    return render_template("merchant_profile.html", merchant=merchant,
                           links=rows[:_PROFILE_PAGE],
                           has_more=len(rows) > _PROFILE_PAGE)


@bp.get("/pay/@<handle>/items.json")
def profile_items(handle: str):
    """Next page of a merchant's public items (cursor = before_id, id-desc).

    Database-driven so the profile stays current; the page requests more only
    when the visitor asks for it.
    """
    from flask import jsonify
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    try:
        before_id = int(request.args.get("before_id", 0))
    except ValueError:
        abort(400)
    q = PaymentLink.query.filter_by(merchant_id=merchant.id, is_active=True)
    if before_id:
        q = q.filter(PaymentLink.id < before_id)
    rows = q.order_by(PaymentLink.id.desc()).limit(_PROFILE_PAGE + 1).all()
    has_more = len(rows) > _PROFILE_PAGE
    rows = rows[:_PROFILE_PAGE]
    html = render_template("_profile_items.html", links=rows, merchant=merchant)
    return jsonify(html=html, has_more=has_more,
                   next_before=(rows[-1].id if rows else None))


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
    channels = _channel_options(include_crypto=True)
    return render_template(
        "checkout.html",
        link=link,
        merchant=merchant,
        channels=channels,
        # Pre-select the first method so the phone field is visible on load
        # instead of a dead form until the customer taps a radio (esp. the
        # MTN-only case, which should read as a plain "enter your number").
        selected_channel=channels[0][0] if channels else None,
        momo_unavailable=_mobile_money_unavailable(),
        crypto_url=url_for("checkout.crypto_checkout", public_id=link.public_id),
        voucher_applied=bool(voucher_data),
        voucher_discount=voucher_data.get("discount", 0),
        prefill=prefill,
    )


@bp.post("/pay/<public_id>/submit")
# UNAUTHENTICATED and fires a real MoMo prompt at an attacker-supplied phone —
# throttle to stop prompt/SMS-bombing, matching bills/subscribe (guardrail 22).
@limiter.limit("10 per minute;60 per hour")
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

    # SERVER-SIDE channel allowlist. _channel_options() is only the UI half; a
    # forged POST could otherwise submit channel=crypto/visa, which route to the
    # PASSTHROUGH adapter (NOT _MockRail, so orchestrator._simulated_rail_forbidden
    # — which only recognises _MockRail — does NOT block them). The passthrough
    # then auto-succeeds the charge with NO money collected and (for a vending
    # order) dispenses a product for free. CRYPTO/VISA are externally settled and
    # must ONLY be charged via their own confirmed flow (the page redirects a
    # crypto pick to /pay/<id>/crypto), never from this generic submit. Reject
    # anything not currently offered, and crypto/visa unconditionally.
    _allowed = {v for v, _, _ in _channel_options(include_crypto=True)}
    if channel in (Channel.CRYPTO, Channel.VISA) or channel.value not in _allowed:
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=_channel_options(include_crypto=True),
            error="That payment method isn't available here.",
            selected_channel=channel.value,
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

    # CLAIM the single-use link BEFORE any money moves — this MUST cover both the
    # $0 (fully gift-card-covered) path below AND the rail path. It used to sit
    # only in front of create_charge, so the $0 branch redeemed the gift card and
    # booked a SUCCEEDED charge entirely OUTSIDE the claim: a concurrent double-
    # submit on a fully-covered single-use link double-debited the card and
    # produced two SUCCEEDED charges (redeem_gift_card's row lock serialises the
    # two redemptions but does NOT stop the second one succeeding when the card
    # still has balance). Atomic compare-and-set on checkout_claimed_at; a claim
    # older than 5 min is reclaimable so a crashed submit never wedges the link.
    # Multi-use links are exempt (meant to be paid repeatedly).
    if not link.allow_multiple_uses:
        from datetime import timedelta
        from sqlalchemy import or_
        _stale = utcnow() - timedelta(minutes=5)
        _claimed = db.session.query(PaymentLink).filter(
            PaymentLink.id == link.id,
            PaymentLink.transaction_id.is_(None),
            or_(PaymentLink.checkout_claimed_at.is_(None),
                PaymentLink.checkout_claimed_at < _stale),
        ).update({"checkout_claimed_at": utcnow()}, synchronize_session=False)
        db.session.commit()
        if not _claimed:
            # Another submit is already mid-charge on this single-use link (or it
            # is already paid) — do NOT redeem a gift card or fire a second
            # prompt; show the status page.
            return redirect(url_for("checkout.status_page", public_id=public_id))

    def _release_claim():
        # Give the claim back so a legitimate retry isn't blocked for 5 min. Only
        # release while transaction_id is still NULL (no charge attached yet).
        if not link.allow_multiple_uses:
            db.session.query(PaymentLink).filter(
                PaymentLink.id == link.id,
                PaymentLink.transaction_id.is_(None),
            ).update({"checkout_claimed_at": None}, synchronize_session=False)
            db.session.commit()

    if charge_amount == 0:
        # Fully covered by gift card — no rail is ever involved, so there is
        # no "the charge might still fail" risk; safe to redeem for real now.
        # The single-use claim above guarantees only ONE submit reaches here.
        from ..services.giftcards import redeem_gift_card
        ok, msg, _ = redeem_gift_card(voucher_code, discount, merchant_id=merchant.id)
        if not ok:
            session.pop(f"voucher_{public_id}", None)
            _release_claim()   # redeem failed AFTER the dry_run passed (rare) —
                               # nothing was debited; free the link for a retry.
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
        from ..services.webhooks import charge_event_data, enqueue
        # Same shared charge payload builder as the orchestrator — no hand-built
        # duplicate that can drift.
        enqueue(merchant, "charge.succeeded", charge_event_data(txn_obj),
                transaction_id=txn_obj.id)

        return redirect(url_for("checkout.status_page", public_id=public_id))

    # Vending orders always carry the link id as the reference: it is how a
    # succeeded charge finds its way back to the machine that must dispense.
    from ..services.vending import is_vending_order
    charge_reference = (
        link.public_id if is_vending_order(link) else (link.reference or link.public_id)
    )

    # The single-use claim was already taken above (before the $0 branch) so it
    # now protects this rail path too — no second claim here.
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
        # redeemed — so there is nothing to roll back here. Release the checkout
        # claim so a legitimate retry (e.g. corrected phone) isn't blocked; no
        # charge is in flight because create_charge raised at/ before initiation.
        _release_claim()
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


@bp.post("/pay/<public_id>/retry")
def retry_payment(public_id: str):
    """Re-open a single-use link whose only attempt FAILED, so the customer can
    try again instead of hitting a dead-end status page.

    MoMo failures (wrong PIN, ignored prompt, momentary insufficient funds) are
    usually retryable, but a single-use link stays bound to its failed txn and
    /pay/<id> just redirects back to the failed status forever. Detaching the
    FAILED txn lets the checkout form render again; the failed txn itself is kept
    as a record. Only a FAILED attempt is detached — never a succeeded or
    in-flight one — and only if it is still the txn bound to the link (so a
    concurrent success that just attached can't be undone).
    """
    from flask import session
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or link.transaction_id is None:
        abort(404)
    txn = db.session.get(Transaction, link.transaction_id)
    if txn is not None and txn.status == TxnStatus.FAILED:
        if txn.customer_phone:
            # Prefill the retried form with the phone they already typed.
            session[f"checkout_prefill_{public_id}"] = {"phone": txn.customer_phone}
        db.session.query(PaymentLink).filter(
            PaymentLink.id == link.id,
            PaymentLink.transaction_id == txn.id,
        ).update({"transaction_id": None, "checkout_claimed_at": None},
                 synchronize_session=False)
        db.session.commit()
    return redirect(url_for("checkout.checkout_page", public_id=public_id))


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
            # Crypto uses the passthrough adapter, so the simulated-rail guard
            # above does NOT catch it. Hide it in live-on-Render unless a real
            # ChangeNow key + receiving wallet are configured — otherwise the
            # mock would phantom-settle a live charge (see changenow._mock_forbidden).
            if value == "crypto":
                from ..services.changenow import _mock_forbidden
                from flask import current_app as _ca
                configured = (_ca.config.get("CHANGENOW_API_KEY")
                              and _ca.config.get("CHANGENOW_RECEIVING_ADDRESS"))
                if not configured and _mock_forbidden():
                    continue
        except Exception:      # unknown channel / no app context — show it
            pass
        available.append((value, label, kind))
    return available


def _mobile_money_unavailable() -> bool:
    """True when MTN is hidden because it is running as a mock on Render.

    The simulated-rail guard (guardrail 14) is right to hide a fake rail from
    a live payment page, but it hid MTN — the ONLY way most Ugandan customers
    can pay — leaving a checkout that silently offered crypto alone. The page
    now says so instead of pretending mobile money was never an option.
    """
    import os
    if not os.environ.get("RENDER"):
        return False
    from flask import current_app, g
    if g.get("api_mode") == "test":
        return False
    return not current_app.config.get("MOMO_USE_REAL")


# ── Crypto checkout (ChangeNow) ────────────────────────────────────────

@bp.post("/pay/<public_id>/apply-voucher")
# Unauthenticated + returns distinct valid/invalid messages: throttle to stop
# brute-force gift-card code guessing.
@limiter.limit("10 per minute;60 per hour")
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
# Unauthenticated + calls an external exchange API: throttle to prevent abuse.
@limiter.limit("10 per minute;60 per hour")
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


# ---------- dispute front-door (public recourse; never moves money) ----------

@bp.get("/pay/<public_id>/report")
def report_problem_form(public_id: str):
    """The permanent 'something went wrong' door on every payment receipt.

    M-Pesa lesson: people trust holding money in a system where the recourse
    path is public and time-boxed BEFORE anything goes wrong. Filing a dispute
    records it for the merchant (dashboard + dispute.opened webhook); refunds
    remain a merchant action through the existing tooling.
    """
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or link.transaction_id is None:
        abort(404)
    txn = db.session.get(Transaction, link.transaction_id)
    merchant = db.session.get(Merchant, link.merchant_id)
    return render_template("report_problem.html", link=link, txn=txn,
                           merchant=merchant, submitted=False)


@bp.post("/pay/<public_id>/report")
@limiter.limit("5 per hour")   # public + unauthenticated — keep it deliberate
def report_problem_submit(public_id: str):
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or link.transaction_id is None:
        abort(404)
    txn = db.session.get(Transaction, link.transaction_id)
    merchant = db.session.get(Merchant, link.merchant_id)

    reason = (request.form.get("reason") or "").strip()
    if reason not in ("not_delivered", "wrong_amount", "double_charge", "other"):
        abort(400)
    details = (request.form.get("details") or "").strip()[:2000] or None
    contact = (request.form.get("contact") or "").strip()[:160] or None

    from ..models import Dispute
    # One OPEN dispute per payment: a second submit updates nothing and still
    # lands on the confirmation (no error surface to farm, no duplicates).
    existing = Dispute.query.filter_by(
        transaction_id=txn.id, status="open").first()

    # Flood ceiling (sovereign-audit finding): the per-IP limit doesn't stop a
    # drive-by page auto-submitting from many visitors' browsers across many
    # links. Past the daily per-merchant cap we stop WRITING rows, page a
    # human once, and still show the confirmation — a flooder learns nothing.
    if existing is None:
        from datetime import timedelta
        from flask import current_app
        from ..models import utcnow as _now
        cap = int(current_app.config.get("DISPUTE_MERCHANT_DAILY_CAP", 50))
        recent = Dispute.query.filter(
            Dispute.merchant_id == link.merchant_id,
            Dispute.created_at >= _now() - timedelta(hours=24)).count()
        if recent >= cap:
            try:
                from ..services.alerts import send_alert
                send_alert(
                    "Dispute flood ceiling hit",
                    f"merchant={link.merchant_id} received {recent} disputes in "
                    f"24h (cap {cap}) — further reports are being acknowledged "
                    f"but NOT recorded. Investigate for abuse.",
                    severity="critical",
                    key=f"dispute-flood-{link.merchant_id}",
                    dedupe_seconds=86400,
                )
            except Exception:
                pass
            return render_template("report_problem.html", link=link, txn=txn,
                                   merchant=merchant, submitted=True)

    if existing is None:
        import uuid as _uuid
        d = Dispute(public_id=f"dsp_{_uuid.uuid4().hex[:16]}",
                    transaction_id=txn.id, merchant_id=link.merchant_id,
                    reason=reason, details=details, contact=contact)
        db.session.add(d)
        db.session.commit()
        from ..services.audit import log_event
        log_event("dispute.opened", merchant_id=link.merchant_id,
                  detail={"dispute": d.public_id, "txn": txn.public_id,
                          "reason": reason})
        db.session.commit()
        # Tell the merchant the moment it happens — never raises, and a
        # merchant with no webhook still sees it on the dashboard.
        try:
            from ..services import webhooks as _wh
            _wh.enqueue(merchant, "dispute.opened", {
                "id": d.public_id,
                "charge_id": txn.public_id,
                "reason": reason,
                "details": details,
                "contact": contact,
                "amount": txn.amount,
                "currency": txn.currency,
                "opened_at": d.created_at.isoformat() if d.created_at else None,
            }, transaction_id=txn.id)
        except Exception:
            pass

    return render_template("report_problem.html", link=link, txn=txn,
                           merchant=merchant, submitted=True)
