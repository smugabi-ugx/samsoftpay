"""Celery beat task: email each merchant their monthly reconciliation statement.

Runs on the 1st of every month and emails the PRIOR month's statement (HTML body
with the reference-level line items + a link to download the PDF) to every
merchant that had live activity that month. Best-effort per merchant — one
merchant's failure never aborts the run.
"""
from ..celery_app import celery


@celery.task(name="app.tasks.statements.email_monthly_statements")
def email_monthly_statements() -> dict:
    from datetime import datetime, timezone
    from ..extensions import db
    from ..models import Merchant, Transaction, Payout
    from ..services.statements import build_statement, render_html
    from ..services.email_service import send_email
    from flask import current_app

    now = datetime.now(timezone.utc)
    # prior month
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
           else datetime(year, month + 1, 1, tzinfo=timezone.utc))

    # merchants with LIVE activity in the period
    ids = set()
    for mid, in db.session.query(Transaction.merchant_id).filter(
            Transaction.is_test.is_(False),
            Transaction.completed_at >= start, Transaction.completed_at < end).distinct():
        ids.add(mid)
    for mid, in db.session.query(Payout.merchant_id).filter(
            Payout.is_test.is_(False),
            Payout.completed_at >= start, Payout.completed_at < end).distinct():
        ids.add(mid)

    base = (current_app.config.get("BASE_URL") or "https://api.samsoftpay.com").rstrip("/")
    sent = 0
    for mid in ids:
        try:
            m = db.session.get(Merchant, mid)
            if not m or not m.email:
                continue
            st = build_statement(m, year, month, is_test=False)
            html = render_html(st)
            pdf_url = f"{base}/dashboard/wallet/statement/{st['period_key']}.pdf"
            body = (html + f'<p><a href="{pdf_url}">Download this statement as a PDF</a> '
                    f'(sign in to your Samsoftpay dashboard).</p>')
            send_email(m.email, f"Your Samsoftpay statement — {st['period_label']}", body,
                       plain=f"Your Samsoftpay statement for {st['period_label']} is ready. "
                             f"Download the PDF: {pdf_url}")
            sent += 1
        except Exception as exc:   # best-effort per merchant
            print(f"monthly statement email failed for merchant {mid}: {exc}")
    print(f"monthly statements: emailed {sent} of {len(ids)} active merchant(s) for {year}-{month:02d}")
    return {"period": f"{year}-{month:02d}", "active": len(ids), "sent": sent}
