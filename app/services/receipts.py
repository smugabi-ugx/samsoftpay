"""Customer payment receipts — emailed and SMS'd to the FINAL CONSUMER.

After a successful charge we send the payer a receipt showing exactly what they
paid, with the URA tax breakdown (VAT and the 0.5% MoMo levy) per the merchant's
tax configuration. Delivery is best-effort and NEVER raises — a receipt failure
must never affect a payment already taken (same rule as the dispense hook).

Delivery activates when its channel is configured: email via MAIL_* (send_email),
SMS via AT_API_KEY (Africa's Talking). Until then it logs and no-ops.
"""
from __future__ import annotations


def send_receipt(txn) -> None:
    """Email + SMS a receipt to the customer. NEVER raises."""
    try:
        _send(txn)
    except Exception:
        try:
            from flask import current_app
            current_app.logger.warning(
                "receipt send failed for %s", getattr(txn, "public_id", "?"))
        except Exception:
            pass


def _send(txn) -> None:
    from . import tax
    from ..extensions import db
    from ..models import Merchant

    merchant = db.session.get(Merchant, txn.merchant_id)
    cfg = tax.get_merchant_tax(txn.merchant_id)
    cur = txn.currency
    amount = int(txn.amount)
    vat_bps = getattr(cfg, "vat_rate_bps", 1800) if getattr(cfg, "vat_enabled", False) else 0

    # The customer PAID txn.amount — so the receipt total is always txn.amount and
    # any VAT is the portion WITHIN it (inclusive), never added on top.
    breakdown = tax.calculate(
        amount=amount,
        vat_rate_bps=vat_bps,
        tax_inclusive=True,
        levy_rate_bps=getattr(cfg, "levy_rate_bps", 50),
        show_levy=getattr(cfg, "show_levy", True),
    )
    lines = tax.format_breakdown(breakdown, cur)
    mname = (getattr(cfg, "business_name", None)
             or getattr(merchant, "name", None) or "the merchant")
    ref = txn.merchant_reference or txn.public_id

    # ---- Email ----
    email = getattr(txn, "customer_email", None)
    if email:
        rows = "".join(
            f"<tr><td style='padding:6px 14px;{'font-weight:700' if l.get('bold') else ''}'>{l['label']}</td>"
            f"<td style='padding:6px 14px;text-align:right;{'font-weight:700' if l.get('bold') else ''}'>"
            f"{cur} {int(l['amount']):,}</td></tr>"
            for l in lines)
        vat_line = (f"<br>VAT No: {cfg.vat_number}"
                    if getattr(cfg, "vat_number", None) else "")
        html = (
            f"<h2 style='font-family:sans-serif'>Payment receipt</h2>"
            f"<p style='font-family:sans-serif'>Thank you for your payment to <strong>{mname}</strong>.</p>"
            f"<table style='border-collapse:collapse;font-family:sans-serif;min-width:280px'>{rows}</table>"
            f"<p style='color:#666;font-size:13px;font-family:sans-serif'>Reference: {ref}{vat_line}"
            f"<br>Processed by Samsoftpay.</p>")
        plain = ("Payment receipt — " + mname + "\n"
                 + "\n".join(f"{l['label']}: {cur} {int(l['amount']):,}" for l in lines)
                 + f"\nReference: {ref}\nProcessed by Samsoftpay.")
        from .email_service import send_email
        send_email(email, f"Your payment receipt — {mname}", html, plain)

    # ---- SMS (short) ----
    phone = getattr(txn, "customer_phone", None)
    if phone:
        vat_note = f", incl. VAT {cur} {int(breakdown.vat):,}" if breakdown.vat else ""
        msg = (f"Payment of {cur} {int(breakdown.total):,} to {mname} confirmed"
               f"{vat_note}. Ref {ref}. -Samsoftpay")
        from .sms_service import send_sms
        send_sms(phone, msg[:300])
