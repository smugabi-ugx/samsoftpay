"""Fee engine."""
from ..models import Channel

COLLECTION_RATE = 0.015     # 1.5%
COLLECTION_MIN  = 200       # UGX — floor so micro-txns are worth processing
COLLECTION_CAP  = 5_000     # UGX — ceiling to stay competitive on large amounts
PAYOUT_FLAT_FEE = 750       # UGX flat per payout (industry norm for disbursements)


def calculate_fee(*, amount: int, channel: Channel, currency: str = "UGX") -> int:
    """Collection fee: 1.5% of amount, min UGX 200, capped at UGX 5,000."""
    if currency != "UGX":
        raise ValueError(f"unsupported currency: {currency}")
    if channel in (Channel.MTN_MOMO, Channel.AIRTEL_MONEY):
        return min(max(COLLECTION_MIN, int(amount * COLLECTION_RATE)), COLLECTION_CAP)
    if channel in (Channel.CARD, Channel.VISA):
        return int(amount * 0.029) + 200
    if channel == Channel.CRYPTO:
        # ChangeNow charges ~0.5% network fee; we pass it through + 0.5% margin
        return max(COLLECTION_MIN, int(amount * 0.01))
    raise ValueError(f"no fee rule for channel {channel}")


def calculate_payout_fee(*, currency: str = "UGX") -> int:
    """Flat fee charged per payout regardless of amount."""
    if currency != "UGX":
        raise ValueError(f"unsupported currency: {currency}")
    return PAYOUT_FLAT_FEE
