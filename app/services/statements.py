"""Finance-grade monthly reconciliation statements.

A merchant's finance team can tie every shilling to their own books AND to MTN:
each line carries the merchant's OWN reference, our id (txn_/pout_), and the MTN
rail reference. Opening/closing balances come from the JOURNAL (the same source
of truth as GET /v1/balance), so the statement reconciles to the penny.

build_statement() assembles the data; render_pdf() draws it with fpdf2 (pure
Python, no system deps — works on Render); render_html() is the same content for
the dashboard/email. Live ledger by default (is_test=False).
"""
from __future__ import annotations

from datetime import datetime, timezone


def _period_bounds(year: int, month: int):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
           else datetime(year, month + 1, 1, tzinfo=timezone.utc))
    return start, end


def _balance_before(merchant_id, cutoff, is_test, currency="UGX"):
    """Merchant-facing balance (available + pending) from the journal, as of the
    instant BEFORE `cutoff`. Journal is credit-normal (negative) so we flip."""
    from ..extensions import db
    from ..models import Account, AccountType, JournalEntry
    from sqlalchemy import func as sf
    total = (db.session.query(sf.coalesce(sf.sum(JournalEntry.amount), 0))
             .join(Account, JournalEntry.account_id == Account.id)
             .filter(Account.merchant_id == merchant_id,
                     Account.is_test.is_(is_test),
                     Account.currency == currency,
                     Account.type.in_([AccountType.MERCHANT_AVAILABLE,
                                       AccountType.MERCHANT_PENDING]),
                     JournalEntry.created_at < cutoff)
             .scalar() or 0)
    return -int(total)


def build_statement(merchant, year: int, month: int, *, is_test: bool = False,
                    currency: str = "UGX") -> dict:
    """Assemble the statement for a merchant + calendar month."""
    from ..extensions import db
    from ..models import Transaction, TxnStatus, Payout, PayoutStatus
    start, end = _period_bounds(year, month)

    opening = _balance_before(merchant.id, start, is_test, currency)
    closing = _balance_before(merchant.id, end, is_test, currency)

    charges = (Transaction.query
               .filter(Transaction.merchant_id == merchant.id,
                       Transaction.is_test.is_(is_test),
                       Transaction.currency == currency,
                       Transaction.status == TxnStatus.SUCCEEDED,
                       Transaction.completed_at >= start,
                       Transaction.completed_at < end)
               .order_by(Transaction.completed_at).all())
    payouts = (Payout.query
               .filter(Payout.merchant_id == merchant.id,
                       Payout.is_test.is_(is_test),
                       Payout.currency == currency,
                       Payout.status.in_([PayoutStatus.SUCCEEDED, PayoutStatus.AUTHORIZED]),
                       Payout.completed_at >= start,
                       Payout.completed_at < end)
               .order_by(Payout.completed_at).all())

    money_in = [{
        "date": t.completed_at,
        "reference": t.merchant_reference or "",
        "id": t.public_id,
        "rail_reference": t.rail_reference or "",
        "party": t.customer_phone or "",
        "gross": int(t.amount),
        "fee": int(t.fee_amount or 0),
        "net": int(t.amount) - int(t.fee_amount or 0),
    } for t in charges]
    money_out = [{
        "date": p.completed_at,
        "reference": p.reference or "",
        "id": p.public_id,
        "rail_reference": p.rail_reference or "",
        "party": p.recipient_phone or "",
        "amount": int(p.amount),
        "fee": int(p.fee_amount or 0),
        "total": int(p.amount) + int(p.fee_amount or 0),
    } for p in payouts]

    total_collected = sum(r["net"] for r in money_in)
    total_gross_in = sum(r["gross"] for r in money_in)
    total_charge_fees = sum(r["fee"] for r in money_in)
    total_payout_fees = sum(r["fee"] for r in money_out)
    total_fees = total_charge_fees + total_payout_fees
    total_paid_out = sum(r["amount"] for r in money_out)
    # What actually left the withdrawable balance: the ledger debits amount+fee
    # per payout (payouts.py), so this — not amount alone — is the money-out
    # figure the journal-derived closing balance foots against. Using amount-only
    # in the summary/footer made neither presentation tie in any month with a
    # payout (charge fees are already inside collected_net; payout fees were
    # dropped from the footer). Identity: closing == opening + collected_net -
    # total_out.
    total_out = total_paid_out + total_payout_fees

    cfg = None
    try:
        from . import tax
        cfg = tax.get_merchant_tax(merchant.id)
    except Exception:
        cfg = None

    return {
        "merchant_name": getattr(merchant, "name", "") or "",
        "merchant_id": merchant.id,
        "vat_number": getattr(cfg, "vat_number", None),
        "period_label": start.strftime("%B %Y"),
        "period_key": f"{year:04d}-{month:02d}",
        "currency": currency,
        "mode": "test" if is_test else "live",
        "generated_at": datetime.now(timezone.utc),
        "opening_balance": opening,
        "closing_balance": closing,
        "money_in": money_in,
        "money_out": money_out,
        "totals": {
            "gross_in": total_gross_in,
            "collected_net": total_collected,
            "paid_out": total_paid_out,          # payout amounts only (excl. fee)
            "paid_out_total": total_out,         # amount + fee — foots to closing
            "fees": total_fees,
            "charge_fees": total_charge_fees,
            "payout_fees": total_payout_fees,
            "count_in": len(money_in),
            "count_out": len(money_out),
        },
    }


def _fmt(n, cur="UGX"):
    return f"{cur} {int(n):,}"


def _a(s) -> str:
    """Latin-1-safe text for the PDF core fonts (helvetica is latin-1 only).
    Merchant names / references are user data and may contain unicode; map any
    unsupported char to '?' rather than crash the whole statement."""
    return str(s).encode("latin-1", "replace").decode("latin-1")


def render_pdf(st: dict) -> bytes:
    """Draw the statement as a PDF (fpdf2, pure Python)."""
    from fpdf import FPDF
    cur = st["currency"]
    pdf = FPDF(orientation="L", unit="mm", format="A4")  # landscape for the line tables
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 8, _a("Samsoftpay - Statement of Account"), ln=1)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _a(f"{st['merchant_name']}   |   Period: {st['period_label']}   |   {st['mode'].upper()} {cur}"), ln=1)
    if st.get("vat_number"):
        pdf.cell(0, 5, _a(f"VAT No: {st['vat_number']}"), ln=1)
    pdf.cell(0, 5, f"Generated: {st['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}", ln=1)
    pdf.ln(2)

    # Summary box
    t = st["totals"]
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Summary", ln=1)
    pdf.set_font("Helvetica", "", 10)
    # These four lines are the running balance and MUST foot exactly:
    # opening + collected_net - paid_out_total = closing. Collection fees are
    # already inside collected_net; payout fees are inside paid_out_total. Fees
    # are disclosed as a memo below, NOT as a separately-summed line (which used
    # to double-subtract the collection fees and break the arithmetic).
    for label, val in [
        ("Opening balance", st["opening_balance"]),
        (f"Collected (net of fees) - {t['count_in']} charge(s)", t["collected_net"]),
        (f"Paid out (incl. fees) - {t['count_out']} payout(s)", -t["paid_out_total"]),
        ("Closing balance", st["closing_balance"]),
    ]:
        pdf.cell(90, 5, _a(label), border=0)
        pdf.cell(0, 5, _fmt(val, cur), ln=1)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, _a(
        f"Memo - fees deducted this period (already reflected above): "
        f"collections {_fmt(t['charge_fees'], cur)} + payouts {_fmt(t['payout_fees'], cur)} "
        f"= {_fmt(t['fees'], cur)}."))
    pdf.ln(3)

    def _table(title, headers, widths, rows):
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 6, _a(title), ln=1)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(238, 244, 241)
        for h, w in zip(headers, widths):
            pdf.cell(w, 6, _a(h), border=1, fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        if not rows:
            pdf.cell(sum(widths), 6, _a("No entries this period."), border=1, ln=1)
        for r in rows:
            for val, w in zip(r, widths):
                pdf.cell(w, 6, _a(str(val)[:int(w / 1.7)]), border=1)
            pdf.ln()
        pdf.ln(3)

    _table("Money in (collections)",
           ["Date", "Your reference", "Charge id", "MTN rail ref", "Payer", "Gross", "Fee", "Net"],
           [24, 45, 40, 45, 30, 25, 20, 26],
           [[r["date"].strftime("%Y-%m-%d") if r["date"] else "", r["reference"], r["id"],
             r["rail_reference"], r["party"], f"{r['gross']:,}", f"{r['fee']:,}", f"{r['net']:,}"]
            for r in st["money_in"]])

    _table("Money out (payouts & refunds)",
           ["Date", "Your reference", "Payout id", "MTN rail ref", "Recipient", "Amount", "Fee", "Total"],
           [24, 45, 40, 45, 30, 25, 20, 26],
           [[r["date"].strftime("%Y-%m-%d") if r["date"] else "", r["reference"], r["id"],
             r["rail_reference"], r["party"], f"{r['amount']:,}", f"{r['fee']:,}", f"{r['total']:,}"]
            for r in st["money_out"]])

    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4, _a(
        f"Reconciliation: opening {_fmt(st['opening_balance'], cur)} + collected "
        f"{_fmt(t['collected_net'], cur)} - paid out incl. fees {_fmt(t['paid_out_total'], cur)} = closing "
        f"{_fmt(st['closing_balance'], cur)}. This closing balance equals what Samsoftpay holds "
        f"for you (GET /v1/balance) at period end. Every line shows your reference, our id, and "
        f"MTN's rail reference so it ties to both your books and MTN's records."))
    out = pdf.output()
    return bytes(out)


def render_html(st: dict) -> str:
    """Same statement as an HTML fragment (dashboard view / email body)."""
    cur = st["currency"]
    t = st["totals"]

    def rows_in():
        if not st["money_in"]:
            return "<tr><td colspan=8 style='color:#888'>No collections this period.</td></tr>"
        return "".join(
            f"<tr><td>{r['date'].strftime('%Y-%m-%d') if r['date'] else ''}</td><td>{r['reference']}</td>"
            f"<td><code>{r['id']}</code></td><td>{r['rail_reference']}</td><td>{r['party']}</td>"
            f"<td style='text-align:right'>{r['gross']:,}</td><td style='text-align:right'>{r['fee']:,}</td>"
            f"<td style='text-align:right'>{r['net']:,}</td></tr>" for r in st["money_in"])

    def rows_out():
        if not st["money_out"]:
            return "<tr><td colspan=8 style='color:#888'>No payouts this period.</td></tr>"
        return "".join(
            f"<tr><td>{r['date'].strftime('%Y-%m-%d') if r['date'] else ''}</td><td>{r['reference']}</td>"
            f"<td><code>{r['id']}</code></td><td>{r['rail_reference']}</td><td>{r['party']}</td>"
            f"<td style='text-align:right'>{r['amount']:,}</td><td style='text-align:right'>{r['fee']:,}</td>"
            f"<td style='text-align:right'>{r['total']:,}</td></tr>" for r in st["money_out"])

    thead_in = "<tr><th>Date</th><th>Your reference</th><th>Charge id</th><th>MTN rail ref</th><th>Payer</th><th>Gross</th><th>Fee</th><th>Net</th></tr>"
    thead_out = "<tr><th>Date</th><th>Your reference</th><th>Payout id</th><th>MTN rail ref</th><th>Recipient</th><th>Amount</th><th>Fee</th><th>Total</th></tr>"
    return f"""
<div style="font-family:sans-serif;max-width:900px">
  <h2 style="margin:0">Statement of Account — {st['period_label']}</h2>
  <p style="color:#555;margin:.25rem 0 1rem">{st['merchant_name']} · {st['mode'].upper()} · {cur}
    {('· VAT No: ' + st['vat_number']) if st.get('vat_number') else ''}</p>
  <table style="border-collapse:collapse;margin-bottom:1rem">
    <tr><td style="padding:3px 12px">Opening balance</td><td style="text-align:right">{_fmt(st['opening_balance'], cur)}</td></tr>
    <tr><td style="padding:3px 12px">Collected (net of fees) — {t['count_in']}</td><td style="text-align:right">{_fmt(t['collected_net'], cur)}</td></tr>
    <tr><td style="padding:3px 12px">Paid out (incl. fees) — {t['count_out']}</td><td style="text-align:right">-{_fmt(t['paid_out_total'], cur)}</td></tr>
    <tr style="font-weight:700"><td style="padding:3px 12px">Closing balance</td><td style="text-align:right">{_fmt(st['closing_balance'], cur)}</td></tr>
    <tr><td style="padding:3px 12px;color:#888;font-size:12px" colspan=2>Memo — fees deducted this period (already reflected above): collections {_fmt(t['charge_fees'], cur)} + payouts {_fmt(t['payout_fees'], cur)} = {_fmt(t['fees'], cur)}</td></tr>
  </table>
  <h3>Money in (collections)</h3>
  <table border=1 cellpadding=4 style="border-collapse:collapse;font-size:12px;width:100%">{thead_in}{rows_in()}</table>
  <h3>Money out (payouts &amp; refunds)</h3>
  <table border=1 cellpadding=4 style="border-collapse:collapse;font-size:12px;width:100%">{thead_out}{rows_out()}</table>
  <p style="color:#666;font-size:12px">The closing balance equals what Samsoftpay holds for you at period end
  (GET /v1/balance). Every line carries your reference, our id, and MTN's rail reference.</p>
</div>"""
