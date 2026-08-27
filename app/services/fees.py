"""Fee engine."""
from ..models import Channel

COLLECTION_RATE = 0.015     # 1.5%
COLLECTION_MIN  = 200       # UGX — floor so micro-txns are worth processing
COLLECTION_CAP  = 5_000     # UGX — ceiling to stay competitive on large amounts
PAYOUT_RATE     = 0.015     # 1.5% — payouts priced the same as collections (no flat fee)


def calculate_fee(*, amount: int, channel: Channel, currency: str = "UGX",
                  bps_override: int | None = None) -> int:
    """Collection fee: 1.5% of amount, min UGX 200, capped at UGX 5,000.

    bps_override (basis points, e.g. 100 = 1.00%) replaces ONLY the default
    percentage rate for the mobile-money rails, while the UGX min/cap floor
    and ceiling still apply — so a negotiated per-merchant rate can never mint
    money nor drop the floor below what a micro-txn costs us. None = standard
    rate. Card/crypto keep their own pricing (no override path).
    """
    if currency != "UGX":
        raise ValueError(f"unsupported currency: {currency}")
    if channel in (Channel.MTN_MOMO, Channel.AIRTEL_MONEY):
        rate = (bps_override / 10000) if bps_override is not None else COLLECTION_RATE
        return min(max(COLLECTION_MIN, int(amount * rate)), COLLECTION_CAP)
    if channel in (Channel.CARD, Channel.VISA):
        return int(amount * 0.029) + 200
    if channel == Channel.CRYPTO:
        # ChangeNow charges ~0.5% network fee; we pass it through + 0.5% margin
        return max(COLLECTION_MIN, int(amount * 0.01))
    raise ValueError(f"no fee rule for channel {channel}")


def calculate_payout_fee(*, amount: int, currency: str = "UGX") -> int:
    """Payout fee: 1.5% of amount, min UGX 200, cap UGX 5,000 — the SAME rate as
    collections. There is no flat fee. The only other deduction is URA taxation
    where it applies (see services/tax.py)."""
    if currency != "UGX":
        raise ValueError(f"unsupported currency: {currency}")
    return min(max(COLLECTION_MIN, int(amount * PAYOUT_RATE)), COLLECTION_CAP)
