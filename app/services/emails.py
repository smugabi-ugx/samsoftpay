"""Branded transactional emails for the events that had none.

Receipts (to the customer) and withdrawal-requested/paid emails already exist.
The gaps this fills:
  - email_merchant_payment_received  — the MERCHANT is told they collected money
                                       (send_receipt only ever emailed the customer)
  - email_refund_issued              — the CUSTOMER is told their refund went out
  - email_kyc_decision               — the merchant is told verified / needs-changes
                                       / re-verification-required (the KYC page used
                                       to say "we don't send email yet")

Every function is BEST-EFFORT: it wraps send_email (which RAISES on SMTP failure)
so a mail outage can never disrupt the money path it hangs off. No recipient
address -> silent no-op. With MAIL_* unset (dev), send_email logs instead.
"""
from __future__ import annotations


def _money(amount: int, currency: str = "UGX") -> str:
    try:
        return f"{currency} {int(amount):,}"
    except Exception:
        return f"{currency} {amount}"


def _shell(heading: str, intro: str, rows: list[tuple[str, str]],
           footnote: str = "") -> str:
    """One on-brand HTML wrapper for every transactional email — indigo header
    (#2e3192, the logo colour), a detail table, a muted footer."""
    row_html = "".join(
        f'<tr><td style="padding:8px 0;color:#585c73;font-size:14px;">{k}</td>'
        f'<td style="padding:8px 0;color:#181a2c;font-size:14px;font-weight:600;'
        f'text-align:right;">{v}</td></tr>'
        for k, v in rows)
    foot = (f'<p style="color:#8a8fa6;font-size:12px;line-height:1.6;margin:20px 0 0;">'
            f'{footnote}</p>') if footnote else ""
    return f"""\
<div style="background:#f4f4f8;padding:24px 0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:1px solid #e5e6ef;">
    <div style="background:#2e3192;padding:20px 28px;">
      <span style="color:#fff;font-size:18px;font-weight:700;letter-spacing:-.01em;">Samsoftpay</span>
    </div>
    <div style="padding:28px;">
      <h1 style="margin:0 0 8px;font-size:20px;color:#181a2c;">{heading}</h1>
      <p style="margin:0 0 20px;color:#585c73;font-size:15px;line-height:1.6;">{intro}</p>
      <table style="width:100%;border-collapse:collapse;border-top:1px solid #eef0f6;">
        {row_html}
      </table>
      {foot}
    </div>
    <div style="padding:16px 28px;border-top:1px solid #eef0f6;">
      <p style="margin:0;color:#8a8fa6;font-size:12px;">Sent by Samsoftpay · Sam Software Co Ltd</p>
    </div>
  </div>
</div>"""


def _safe_send(to_email: str | None, subject: str, html: str, plain: str) -> bool:
    """Send, but NEVER raise — these hang off money paths. Returns True if sent."""
    if not to_email:
        return False
    try:
        from .email_service import send_email
        send_email(to_email, subject, html, plain)
        return True
    except Exception:
        try:
            from flask import current_app
            current_app.logger.warning("transactional email '%s' to %s failed",
                                       subject, to_email, exc_info=True)
        except Exception:
            pass
        return False


def email_merchant_payment_received(txn) -> bool:
    """Tell the MERCHANT they collected money (send_receipt only emails the
    customer). Best-effort; caller need not guard."""
    try:
        from ..extensions import db
        from ..models import Merchant
        merchant = db.session.get(Merchant, txn.merchant_id)
        if merchant is None or not getattr(merchant, "email", None):
            return False
        gross = int(txn.amount)
        fee = int(getattr(txn, "fee_amount", 0) or 0)
        net = gross - fee
        cur = txn.currency
        ref = txn.merchant_reference or txn.public_id
        rows = [("Amount", _money(gross, cur)),
                ("Fee", _money(fee, cur)),
                ("Net to you", _money(net, cur)),
                ("Reference", ref)]
        if getattr(txn, "customer_phone", None):
            rows.append(("From", txn.customer_phone))
        heading = f"You received {_money(net, cur)}"
        intro = (f"A payment succeeded on your Samsoftpay account. Net proceeds "
                 f"become available to withdraw after the settlement hold.")
        html = _shell(heading, intro, rows)
        plain = (f"You received a payment of {_money(gross, cur)} "
                 f"(net {_money(net, cur)}) — reference {ref}.")
        return _safe_send(merchant.email, f"You received {_money(net, cur)}",
                          html, plain)
    except Exception:
        return False


def email_refund_issued(txn, refund_amount: int | None = None) -> bool:
    """Tell the CUSTOMER their refund was issued. No customer email -> no-op."""
    try:
        from ..extensions import db
        from ..models import Merchant
        email = getattr(txn, "customer_email", None)
        if not email:
            return False
        merchant = db.session.get(Merchant, txn.merchant_id)
        mname = getattr(merchant, "name", None) or "the merchant"
        amt = int(refund_amount if refund_amount is not None else txn.amount)
        cur = txn.currency
        ref = txn.merchant_reference or txn.public_id
        rows = [("Refund amount", _money(amt, cur)),
                ("Merchant", mname),
                ("Original reference", ref)]
        heading = f"Your refund of {_money(amt, cur)}"
        intro = (f"{mname} has issued a refund to your mobile money account. It "
                 f"may take a short while to reflect with your provider.")
        html = _shell(heading, intro, rows)
        plain = f"A refund of {_money(amt, cur)} from {mname} — reference {ref}."
        return _safe_send(email, f"Your refund of {_money(amt, cur)}", html, plain)
    except Exception:
        return False


def email_kyc_decision(merchant, decision: str, note: str = "") -> bool:
    """Tell the merchant the outcome of business verification.

    decision: 'approved' | 'rejected' | 'reverify'
    """
    try:
        if merchant is None or not getattr(merchant, "email", None):
            return False
        if decision == "approved":
            heading = "Your business is verified"
            intro = ("Your Samsoftpay account is now fully verified — you can "
                     "accept and withdraw live money.")
        elif decision == "reverify":
            heading = "Re-verification required"
            intro = ("We need to re-check your business details before you can "
                     "continue moving live money. Please update your verification.")
        else:  # rejected / needs changes
            heading = "Your verification needs changes"
            intro = ("We couldn't approve your business verification yet. Please "
                     "review the notes below and resubmit.")
        rows = [("Business", getattr(merchant, "name", "") or "")]
        if note:
            rows.append(("Notes", note))
        rows.append(("Status", decision))
        html = _shell(heading, intro, rows,
                      footnote="Manage verification from your Samsoftpay dashboard.")
        plain = f"{heading}. {intro} {('Notes: ' + note) if note else ''}"
        return _safe_send(merchant.email, f"Samsoftpay — {heading.lower()}",
                          html, plain)
    except Exception:
        return False
