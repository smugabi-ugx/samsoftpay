"""Gift card management — create, list, deactivate."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Account, AccountType, GiftCard
from ..services.giftcards import create_gift_card
from ..utils import verified_required


def _available_balance(merchant_id: int) -> int:
    acct = Account.query.filter_by(
        merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE, is_test=False
    ).first()
    return -acct.cached_balance if acct else 0


def _outstanding_giftcard_liability(merchant_id: int) -> int:
    from sqlalchemy import func as safunc
    total = (db.session.query(safunc.coalesce(safunc.sum(GiftCard.balance), 0))
             .filter(GiftCard.merchant_id == merchant_id,
                     GiftCard.is_active.is_(True)).scalar())
    return int(total or 0)

bp = Blueprint("giftcards", __name__, url_prefix="/dashboard/gift-cards")


@bp.get("")
@login_required
@verified_required
def list_cards():
    from ..utils import merchant_or_admin
    cards = GiftCard.query.filter_by(merchant_id=current_user.id)\
        .order_by(GiftCard.created_at.desc()).limit(100).all()
    return render_template("giftcards.html", cards=cards)


@bp.post("/create")
@login_required
@verified_required
def create():
    try:
        face_value = int(request.form["face_value"])
        qty = max(1, min(int(request.form.get("qty", 1)), 50))
    except (KeyError, ValueError):
        flash("Enter a valid gift-card amount.", "error")
        return redirect(url_for("giftcards.list_cards"))

    if face_value <= 0:
        flash("Gift-card amount must be greater than zero.", "error")
        return redirect(url_for("giftcards.list_cards"))

    # BACKING CHECK (Sam's requirement + gold standard): a gift card is real
    # merchant liability — a redeemed card is settled against the merchant's
    # available balance. Issuing cards the balance can't cover mints unbacked
    # money. Require: available >= already-outstanding card liability + the new
    # cards' total face value. Zero cards created if it doesn't cover.
    new_liability = face_value * qty
    available = _available_balance(current_user.id)
    outstanding = _outstanding_giftcard_liability(current_user.id)
    if available < outstanding + new_liability:
        room = max(0, available - outstanding)
        flash(
            f"Not enough available balance to back these gift cards. Issuing "
            f"{qty} × UGX {face_value:,} needs UGX {new_liability:,}, but only "
            f"UGX {room:,} of your UGX {available:,} available balance is "
            f"un-committed (UGX {outstanding:,} already backs active cards). "
            f"Top up or fund your balance first.", "error")
        return redirect(url_for("giftcards.list_cards"))

    notes = request.form.get("notes", "").strip() or None
    expires_str = request.form.get("expires_at", "").strip()
    expires_at = None
    if expires_str:
        from datetime import datetime
        try:
            expires_at = datetime.fromisoformat(expires_str)
        except ValueError:
            pass
    if expires_at is None:
        # Gold-standard gift cards expire. Default 1 year if unspecified.
        from datetime import timedelta
        from ..models import utcnow
        expires_at = utcnow() + timedelta(days=365)

    for _ in range(qty):
        create_gift_card(
            merchant_id=current_user.id,
            face_value=face_value,
            notes=notes,
            expires_at=expires_at,
        )
    flash(f"Issued {qty} gift card{'s' if qty != 1 else ''} "
          f"(UGX {face_value:,} each).", "success")
    return redirect(url_for("giftcards.list_cards"))


@bp.post("/<int:card_id>/deactivate")
@login_required
@verified_required
def deactivate(card_id: int):
    card = GiftCard.query.filter_by(id=card_id, merchant_id=current_user.id).first_or_404()
    card.is_active = False
    db.session.commit()
    return redirect(url_for("giftcards.list_cards"))
