"""Gift card / voucher service."""
import random
import string
import uuid
from datetime import datetime, timezone

from ..extensions import db
from ..models import GiftCard


_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/I/1 ambiguity


def generate_code() -> str:
    """Generate a unique gift card code like SAMF-X4K2-9WQP."""
    while True:
        parts = ["".join(random.choices(_CHARS, k=4)) for _ in range(3)]
        code = "-".join(parts)
        if not GiftCard.query.filter_by(code=code).first():
            return code


def create_gift_card(
    *,
    merchant_id: int,
    face_value: int,
    currency: str = "UGX",
    notes: str | None = None,
    expires_at=None,
) -> GiftCard:
    card = GiftCard(
        public_id=f"gc_{uuid.uuid4().hex[:16]}",
        merchant_id=merchant_id,
        code=generate_code(),
        face_value=face_value,
        balance=face_value,
        currency=currency,
        notes=notes,
        expires_at=expires_at,
    )
    db.session.add(card)
    db.session.commit()
    return card


def redeem_gift_card(code: str, amount: int) -> tuple[bool, str, GiftCard | None]:
    """Attempt to redeem `amount` from a gift card code.

    Returns (success, message, card).
    """
    card = GiftCard.query.filter_by(code=code.upper().strip()).first()
    if not card:
        return False, "Gift card code not found.", None
    if not card.is_active:
        return False, "This gift card has been deactivated.", None
    if card.balance <= 0:
        return False, "This gift card has no remaining balance.", None
    if card.expires_at:
        if datetime.now(timezone.utc) > card.expires_at.replace(tzinfo=timezone.utc):
            return False, "This gift card has expired.", None
    if amount > card.balance:
        return False, f"Insufficient balance. Available: {card.balance} {card.currency}.", None

    card.balance -= amount
    if card.balance == 0:
        card.redeemed_at = datetime.now(timezone.utc)
        card.is_active = False
    db.session.commit()
    return True, "Gift card redeemed successfully.", card
