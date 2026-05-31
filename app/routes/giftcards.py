"""Gift card management — create, list, deactivate."""
from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import GiftCard
from ..services.giftcards import create_gift_card
from ..utils import verified_required

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

    for _ in range(qty):
        create_gift_card(
            merchant_id=current_user.id,
            face_value=face_value,
            notes=notes,
            expires_at=expires_at,
        )
    return redirect(url_for("giftcards.list_cards"))


@bp.post("/<int:card_id>/deactivate")
@login_required
@verified_required
def deactivate(card_id: int):
    card = GiftCard.query.filter_by(id=card_id, merchant_id=current_user.id).first_or_404()
    card.is_active = False
    db.session.commit()
    return redirect(url_for("giftcards.list_cards"))
