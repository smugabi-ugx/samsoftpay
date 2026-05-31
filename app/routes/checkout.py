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
from ..models import Channel, Merchant, PaymentLink, Transaction, TxnStatus
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

    return render_template(
        "checkout.html",
        link=link,
        merchant=merchant,
        channels=[
            ("mtn_momo",     "MTN Mobile Money",       "phone"),
            ("airtel_money", "Airtel Money",            "phone"),
            ("card",         "Visa / Mastercard",       "card"),
            ("crypto",       "Crypto (BTC/ETH/USDT…)", "crypto"),
        ],
        crypto_url=url_for("checkout.crypto_checkout", public_id=link.public_id),
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

    try:
        channel = Channel(request.form.get("channel", ""))
    except ValueError:
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=[
                ("mtn_momo", "MTN Mobile Money", "phone"),
                ("airtel_money", "Airtel Money", "phone"),
                ("card", "Card (Visa/Mastercard)", "card"),
            ],
            error="Please choose a payment method.",
        )

    customer_phone = (request.form.get("phone") or "").strip()
    customer_email = (request.form.get("email") or "").strip() or None

    # Minimal validation — channel-appropriate identifier present
    if channel in (Channel.MTN_MOMO, Channel.AIRTEL_MONEY) and not customer_phone:
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=_channel_options(),
            error="Phone number is required for mobile money.",
            selected_channel=channel.value,
        )

    from flask import g
    g.api_mode = "live"

    try:
        txn = create_charge(
            merchant=merchant,
            amount=link.amount,
            currency=link.currency,
            channel=channel,
            customer_phone=customer_phone or None,
            customer_email=customer_email,
            merchant_reference=link.reference or link.public_id,
        )
    except OrchestratorError as exc:
        return render_template(
            "checkout.html", link=link, merchant=merchant,
            channels=_channel_options(),
            error=f"Could not start payment: {exc}",
            selected_channel=channel.value,
        )

    # Attach the transaction to the link (so we can show status on revisit)
    if not link.transaction_id:
        link.transaction_id = txn.id
        db.session.commit()

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


def _channel_options():
    return [
        ("mtn_momo", "MTN Mobile Money", "phone"),
        ("airtel_money", "Airtel Money", "phone"),
        ("card", "Card (Visa/Mastercard)", "card"),
    ]


# ── Crypto checkout (ChangeNow) ────────────────────────────────────────

@bp.get("/pay/<public_id>/crypto")
def crypto_checkout(public_id: str):
    from ..services.changenow import SUPPORTED_COINS
    link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
    if link is None or not link.is_active:
        abort(404)
    merchant = db.session.get(Merchant, link.merchant_id)
    # Check if an exchange is already in progress (stored in session)
    from flask import session
    order_data = session.get(f"cn_order_{public_id}")
    order = type("O", (), order_data)() if order_data else None
    return render_template(
        "checkout_crypto.html",
        link=link, merchant=merchant,
        coins=SUPPORTED_COINS,
        order=order,
    )


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
        return redirect(url_for("checkout.checkout_page", public_id=public_id))

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
    # If finished, create the transaction record
    if status == "finished" and not session.get(f"cn_settled_{public_id}"):
        link = PaymentLink.query.filter_by(public_id=public_id).one_or_none()
        if link and link.is_active:
            merchant = db.session.get(Merchant, link.merchant_id)
            from flask import g
            g.api_mode = "live"
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
                pass
    return jsonify(status=status)
