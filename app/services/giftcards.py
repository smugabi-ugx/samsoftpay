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


def redeem_gift_card(
    code: str, amount: int, *, merchant_id: int | None = None, dry_run: bool = False
) -> tuple[bool, str, GiftCard | None]:
    """Attempt to redeem `amount` from a gift card code.

    `merchant_id`, if given, scopes the lookup — a checkout for merchant A must
    never be able to redeem merchant B's code even though codes are globally
    unique (defense in depth, not load-bearing on its own).

    `dry_run=True` runs every check WITHOUT deducting or committing — this is
    what powers the checkout-page preview (apply_voucher), so the preview and
    the real redemption can never disagree about whether a code is valid.

    Row-locked (`with_for_update`) on a real redemption: two concurrent
    redemptions of the same code must serialise, the same reasoning as
    ledger.lock_account_for_update for merchant balances. A no-op on SQLite —
    fine for single-threaded local dev, matches the existing ledger pattern.

    Returns (success, message, card).
    """
    query = GiftCard.query.filter_by(code=code.upper().strip())
    if merchant_id is not None:
        query = query.filter_by(merchant_id=merchant_id)
    # populate_existing: with_for_update alone acquires the lock but serves a
    # STALE object if the card is already in the session identity map — the
    # balance check would then run against pre-lock data, defeating the lock.
    card = (query.with_for_update().populate_existing().first()
            if not dry_run else query.first())
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

    if dry_run:
        return True, "Gift card is valid.", card

    card.balance -= amount
    if card.balance == 0:
        card.redeemed_at = datetime.now(timezone.utc)
        card.is_active = False
    db.session.commit()
    return True, "Gift card redeemed successfully.", card
