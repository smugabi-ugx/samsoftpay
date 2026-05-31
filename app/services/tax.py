"""Tax calculation engine for Uganda.

All amounts in integer UGX (minor units — UGX has no sub-unit).
Rates stored in basis points (bps): 1 bps = 0.01%, so 1800 bps = 18%.

Uganda tax reference:
  VAT:             18%  (URA, on taxable supplies, threshold UGX 150M/year)
  MoMo Levy:       0.5% (Financial Transactions Levy 2021 — shown on receipts)
  Withholding Tax: 6%   (on payments to suppliers by large taxpayers)
  Samsoftpay fee:  1.5% (our collection charge, min UGX 200, cap UGX 5,000)
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class TaxBreakdown:
    principal: int      # pre-tax amount
    vat: int            # VAT charged
    levy: int           # MoMo levy (informational)
    platform_fee: int   # Samsoftpay 1.5%
    total: int          # what customer pays
    vat_rate_pct: float
    levy_rate_pct: float


def calculate(
    *,
    amount: int,
    vat_rate_bps: int = 0,
    tax_inclusive: bool = False,
    levy_rate_bps: int = 50,
    show_levy: bool = True,
    platform_fee_bps: int = 150,   # 1.5%
    platform_fee_min: int = 200,
    platform_fee_cap: int = 5_000,
) -> TaxBreakdown:
    """Return a full tax breakdown for a transaction amount.

    Args:
        amount:           gross amount in UGX
        vat_rate_bps:     VAT in basis points (0 if merchant not VAT-registered)
        tax_inclusive:    True if VAT is already included in amount
        levy_rate_bps:    MoMo levy rate (50 = 0.5%)
        show_levy:        include levy line in breakdown
        platform_fee_bps: Samsoftpay collection fee in bps
    """
    if tax_inclusive and vat_rate_bps > 0:
        # Extract VAT from amount: vat = amount * rate / (1 + rate)
        rate = vat_rate_bps / 10_000
        vat = int(amount * rate / (1 + rate))
        principal = amount - vat
    else:
        principal = amount
        vat = int(amount * vat_rate_bps / 10_000) if vat_rate_bps else 0

    total = principal + vat   # what customer pays

    # MoMo levy is borne by the rail / customer — shown for transparency
    levy = int(total * levy_rate_bps / 10_000) if show_levy else 0

    # Platform fee on the principal (before VAT)
    raw_fee = int(principal * platform_fee_bps / 10_000)
    platform_fee = min(max(platform_fee_min, raw_fee), platform_fee_cap)

    return TaxBreakdown(
        principal=principal,
        vat=vat,
        levy=levy,
        platform_fee=platform_fee,
        total=total,
        vat_rate_pct=vat_rate_bps / 100,
        levy_rate_pct=levy_rate_bps / 100,
    )


def get_merchant_tax(merchant_id: int):
    """Load TaxConfiguration for a merchant, or return defaults (VAT off)."""
    from ..models import TaxConfiguration
    cfg = TaxConfiguration.query.filter_by(merchant_id=merchant_id).first()
    if cfg:
        return cfg
    # Return a default object with VAT disabled
    class _Default:
        vat_enabled   = False
        vat_rate_bps  = 1800
        vat_number    = None
        tax_inclusive = False
        show_levy     = True
        levy_rate_bps = 50
        business_name = None
        business_address = None
    return _Default()


def format_breakdown(breakdown: TaxBreakdown, currency: str = "UGX") -> list[dict]:
    """Return a list of line items suitable for rendering in a receipt."""
    lines = [{"label": "Subtotal", "amount": breakdown.principal, "currency": currency}]
    if breakdown.vat:
        lines.append({
            "label": f"VAT ({breakdown.vat_rate_pct:.0f}%)",
            "amount": breakdown.vat,
            "currency": currency,
        })
    if breakdown.levy:
        lines.append({
            "label": f"MoMo Levy ({breakdown.levy_rate_pct:.1f}%)",
            "amount": breakdown.levy,
            "currency": currency,
            "note": "Applied by mobile money provider",
        })
    lines.append({
        "label": "Total",
        "amount": breakdown.total,
        "currency": currency,
        "bold": True,
    })
    return lines
