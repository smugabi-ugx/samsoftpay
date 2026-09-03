"""Per-merchant Compliance Audit Pack — a formal PDF an admin hands to a bank,
BoU, or the police on request.

Bundles, for ONE merchant, from the source-of-truth data:
  - Merchant register (identity + KYC status)
  - Directors / beneficial owners with their ID numbers (NIN)
  - The uploaded KYC document list
  - Current ledger balances (live)
  - Flow of funds (collected / disbursed / refunded / fees)
  - The append-only ledger journal (the arithmetic trail)

Rendered with fpdf2 (same lib as statements). Returns PDF bytes so a route can
stream it without touching disk. The CLI `flask regulator-pack` remains the
platform-wide/complete dump; this is the on-demand, per-merchant, UI version.
"""
from __future__ import annotations

from datetime import datetime, timezone

BRAND = (46, 49, 146)
INK = (24, 26, 44)
MUTED = (88, 92, 115)
LINE = (223, 227, 238)
WHITE = (255, 255, 255)
JOURNAL_CAP = 1200   # rows; beyond this, note to use flask regulator-pack


def _a(s):
    return (str(s).replace("’", "'").replace("–", "-").replace("—", "-")
            .replace("“", '"').replace("”", '"').replace("·", "-")
            .encode("latin-1", "replace").decode("latin-1"))


def _money(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def build_audit_pack(merchant, *, is_test: bool = False) -> bytes:
    from fpdf import FPDF
    from ..extensions import db
    from ..models import (Account, AccountType, JournalEntry, KYCApplication,
                          Transaction, TxnStatus, Payout, PayoutStatus)
    from sqlalchemy import func as sf

    mid = merchant.id
    app = KYCApplication.query.filter_by(merchant_id=mid).first()

    accounts = Account.query.filter_by(merchant_id=mid, is_test=is_test).all()
    acct_ids = [a.id for a in accounts]
    acct_type = {a.id: a.type for a in accounts}

    def _bal(t):
        a = next((x for x in accounts if x.type == t), None)
        return -int(a.cached_balance) if a else 0   # credit-normal -> positive

    available = _bal(AccountType.MERCHANT_AVAILABLE)
    pending = _bal(AccountType.MERCHANT_PENDING)

    collected = int(db.session.query(sf.coalesce(sf.sum(Transaction.amount), 0))
                    .filter(Transaction.merchant_id == mid, Transaction.is_test.is_(is_test),
                            Transaction.status == TxnStatus.SUCCEEDED).scalar() or 0)
    refunded = int(db.session.query(sf.coalesce(sf.sum(Transaction.amount), 0))
                   .filter(Transaction.merchant_id == mid, Transaction.is_test.is_(is_test),
                           Transaction.status == TxnStatus.REFUNDED).scalar() or 0)
    fees = int(db.session.query(sf.coalesce(sf.sum(Transaction.fee_amount), 0))
               .filter(Transaction.merchant_id == mid, Transaction.is_test.is_(is_test),
                       Transaction.status.in_([TxnStatus.SUCCEEDED, TxnStatus.REFUNDED])).scalar() or 0)
    disbursed = int(db.session.query(sf.coalesce(sf.sum(Payout.amount), 0))
                    .filter(Payout.merchant_id == mid, Payout.is_test.is_(is_test),
                            Payout.status.in_([PayoutStatus.SUCCEEDED, PayoutStatus.AUTHORIZED])).scalar() or 0)

    entries = []
    if acct_ids:
        entries = (JournalEntry.query.filter(JournalEntry.account_id.in_(acct_ids))
                   .order_by(JournalEntry.created_at.desc()).limit(JOURNAL_CAP + 1).all())
    truncated = len(entries) > JOURNAL_CAP
    entries = entries[:JOURNAL_CAP]

    # ── render ──
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.set_margins(16, 14, 16)
    pdf.add_page()

    # letterhead
    pdf.set_fill_color(*BRAND); pdf.rect(0, 0, pdf.w, 26, "F")
    pdf.set_xy(16, 7); pdf.set_text_color(*WHITE); pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, _a("SAM SOFTWARE CO. LTD"))
    pdf.set_xy(16, 15); pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 5, _a("Samsoftpay - Compliance Audit Pack   |   CONFIDENTIAL - for regulatory / law-enforcement use"))
    pdf.set_y(32); pdf.set_text_color(*INK)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, _a(f"Merchant: {merchant.name}")); pdf.ln(7)
    pdf.set_font("Helvetica", "", 8.5); pdf.set_text_color(*MUTED)
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pdf.cell(0, 5, _a(f"Generated {gen}   |   Ledger: {'SANDBOX (test)' if is_test else 'LIVE'}   |   All amounts in UGX (minor units)"))
    pdf.ln(8); pdf.set_text_color(*INK)

    def sec(title):
        pdf.ln(1); pdf.set_font("Helvetica", "B", 11); pdf.set_text_color(*BRAND)
        pdf.cell(0, 6, _a(title)); pdf.ln(7); pdf.set_text_color(*INK)

    def kv(k, v):
        pdf.set_font("Helvetica", "B", 9); pdf.cell(50, 5.4, _a(k))
        pdf.set_font("Helvetica", "", 9); pdf.cell(0, 5.4, _a(v)[:90]); pdf.ln(5.4)

    # A. register
    sec("A. Merchant register")
    kv("Business name", merchant.name)
    kv("Email", merchant.email or "-")
    kv("Handle", ("@" + merchant.handle) if merchant.handle else "-")
    kv("Merchant ID", str(mid))
    kv("TIN / NIN", (app.tin if app and app.tin else "-"))
    kv("KYC status", merchant.kyc_status or "-")
    kv("Account active", "yes" if merchant.is_active else "no (suspended)")
    kv("Registered", merchant.created_at.strftime("%d %b %Y") if merchant.created_at else "-")

    # B. directors
    sec("B. Directors / beneficial owners")
    dirs = list(app.directors) if app else []
    if not dirs:
        pdf.set_font("Helvetica", "I", 9); pdf.set_text_color(*MUTED)
        pdf.cell(0, 5.4, _a("No directors on file.")); pdf.ln(6); pdf.set_text_color(*INK)
    else:
        pdf.set_font("Helvetica", "B", 8); pdf.set_text_color(*MUTED)
        for h, w in [("Name", 55), ("ID type", 28), ("ID number (NIN)", 42), ("Phone", 30), ("Primary", 0)]:
            pdf.cell(w, 6, _a(h.upper()), border="B")
        pdf.ln(6); pdf.set_text_color(*INK); pdf.set_font("Helvetica", "", 8.5)
        for d in dirs:
            pdf.cell(55, 5.2, _a(d.full_name or "-"))
            pdf.cell(28, 5.2, _a((d.id_type or "-").replace("_", " ")))
            pdf.cell(42, 5.2, _a(d.id_number or "-"))
            pdf.cell(30, 5.2, _a(d.contact_phone or "-"))
            pdf.cell(0, 5.2, _a("yes" if d.is_primary else ""))
            pdf.ln(5.2)

    # C. documents
    sec("C. KYC documents on file")
    docs = list(app.documents) if app else []
    if not docs:
        pdf.set_font("Helvetica", "I", 9); pdf.set_text_color(*MUTED)
        pdf.cell(0, 5.4, _a("No documents uploaded.")); pdf.ln(6); pdf.set_text_color(*INK)
    else:
        pdf.set_font("Helvetica", "", 8.5)
        for doc in docs:
            up = doc.uploaded_at.strftime("%d %b %Y") if getattr(doc, "uploaded_at", None) else "-"
            pdf.cell(0, 5.2, _a(f"- {doc.doc_type}: {doc.original_filename}  (uploaded {up})")); pdf.ln(5.2)

    # D. balances + flow
    sec("D. Balances & flow of funds")
    kv("Available balance", "UGX " + _money(available))
    kv("Pending settlement", "UGX " + _money(pending))
    kv("Total collected", "UGX " + _money(collected))
    kv("Total disbursed", "UGX " + _money(disbursed))
    kv("Total refunded", "UGX " + _money(refunded))
    kv("Total fees", "UGX " + _money(fees))

    # E. journal
    sec("E. Ledger journal (append-only)")
    pdf.set_font("Helvetica", "I", 8); pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 4.4, _a("Immutable double-entry postings for this merchant's accounts. "
                              "Merchant accounts are credit-normal (a negative amount is a credit / "
                              "money owed to the merchant). This is the arithmetic trail behind every balance."))
    pdf.ln(1); pdf.set_text_color(*MUTED); pdf.set_font("Helvetica", "B", 7.5)
    for h, w in [("Date (UTC)", 30), ("Account", 40), ("Amount", 28), ("Txn", 24), ("Memo", 0)]:
        pdf.cell(w, 5.5, _a(h.upper()), border="B")
    pdf.ln(5.5); pdf.set_text_color(*INK); pdf.set_font("Helvetica", "", 7.5)
    for e in entries:
        dt = e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "-"
        at = acct_type.get(e.account_id)
        atn = (at.value if at else "-")
        pdf.cell(30, 4.6, _a(dt))
        pdf.cell(40, 4.6, _a(atn))
        pdf.cell(28, 4.6, _a(_money(e.amount)))
        pdf.cell(24, 4.6, _a(("txn:" + str(e.transaction_id)) if e.transaction_id else "-"))
        pdf.cell(0, 4.6, _a((e.memo or "")[:42])); pdf.ln(4.6)
    if truncated:
        pdf.ln(1); pdf.set_font("Helvetica", "I", 8); pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 4.4, _a(f"Showing the most recent {JOURNAL_CAP} entries. For the complete "
                                  f"journal, run: flask regulator-pack <YYYY-MM>."))

    return bytes(pdf.output())
