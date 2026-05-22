"""Fee engine.

Real PSPs have complex fee structures: per-rail, tiered by volume, per-merchant
overrides, fixed + percentage components, MNO interchange passthrough, etc.

Demo rules (matching roughly what's typical in Uganda):
- Mobile money: 1.5% with a minimum of UGX 500 and cap UGX 5,000
- Card: 2.9% + UGX 200
"""
from ..models import Channel


def calculate_fee(*, amount: int, channel: Channel, currency: str = "UGX") -> int:
    if currency != "UGX":  # demo only handles UGX
        raise ValueError(f"unsupported currency in demo: {currency}")

    if channel in (Channel.MTN_MOMO, Channel.AIRTEL_MONEY):
        fee = max(500, int(amount * 0.015))
        return min(fee, 5_000)
    if channel == Channel.CARD:
        return int(amount * 0.029) + 200
    raise ValueError(f"no fee rule for channel {channel}")
