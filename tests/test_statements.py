"""Finance-grade reconciliation statements: reference-level + reconciles to balance.

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_statements.py

  [1] statement lists money-in/out with the merchant reference, our id, MTN rail ref
  [2] the math reconciles: opening + collected - paid_out == closing
  [3] render_pdf produces a real PDF
  [4] GET /v1/statements/YYYY-MM (JSON) and .pdf (PDF) work through the API
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import (Merchant, Transaction, TxnStatus, Channel, Payout, PayoutStatus,
                        AccountType)
from app.services import ledger
from app.services.statements import build_statement, render_pdf


def main():
    app = create_app({"WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    Y, M = 2026, 7
    mid_month = datetime(Y, M, 15, tzinfo=timezone.utc)
    with app.app_context():
        db.create_all()
        m = Merchant(name="Kampala Coffee Ltd", email="k@x.com", public_key="pk",
                     secret_key="sk_live_k", handle="kc", kyc_status="verified",
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        mode = False   # live ledger

        # Fund available before the period so opening balance is non-zero.
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX", is_test=mode)
        av = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id, currency="UGX", is_test=mode)
        before = datetime(Y, M, 1, tzinfo=timezone.utc)
        # post an opening credit dated before the period
        e = ledger.post([(rail, +50_000), (av, -50_000)], currency="UGX", memo="opening")
        from app.models import JournalEntry
        for je in JournalEntry.query.all():
            je.created_at = datetime(Y, M - 1, 20, tzinfo=timezone.utc)   # prior month
        db.session.commit()

        # A collection and a payout inside the period (with references + rail refs).
        db.session.add(Transaction(public_id="txn_a", merchant_id=m.id, amount=100_000, fee_amount=1_500,
                                   currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                                   is_test=mode, customer_phone="256780000001", merchant_reference="INV-1",
                                   rail_reference="mtn_abc", completed_at=mid_month))
        db.session.add(Payout(public_id="pout_a", merchant_id=m.id, amount=20_000, fee_amount=300,
                              currency="UGX", channel=Channel.MTN_MOMO, status=PayoutStatus.SUCCEEDED,
                              is_test=mode, recipient_phone="256780000009", reference="SAL-9",
                              rail_reference="disb_xyz", completed_at=mid_month))
        db.session.commit()

        # Post the matching journal entries the real charge/payout would (balances
        # come from the journal): charge credits net 98,500 to available; payout
        # debits amount+fee (20,300) from available. Date them inside the period.
        _n0 = JournalEntry.query.count()
        ledger.post([(rail, +98_500), (av, -98_500)], currency="UGX", memo="charge txn_a")
        ledger.post([(av, +20_300), (rail, -20_300)], currency="UGX", memo="payout pout_a")
        for je in JournalEntry.query.all():
            if je.id > _n0 and je.created_at.replace(tzinfo=timezone.utc) < before:
                pass
        for je in JournalEntry.query.filter(JournalEntry.id > _n0).all():
            je.created_at = mid_month
        db.session.commit()

        st = build_statement(m, Y, M, is_test=mode)

        # [1] reference-level mapping present
        assert len(st["money_in"]) == 1 and st["money_in"][0]["reference"] == "INV-1", "[1] charge ref"
        assert st["money_in"][0]["id"] == "txn_a" and st["money_in"][0]["rail_reference"] == "mtn_abc", "[1] ids"
        assert len(st["money_out"]) == 1 and st["money_out"][0]["reference"] == "SAL-9", "[1] payout ref"
        assert st["money_out"][0]["rail_reference"] == "disb_xyz", "[1] payout rail ref"
        print("[1] PASS — money-in/out carry your reference + our id + MTN rail ref")

        # [2] reconciliation math — the presentation must now FOOT by construction,
        # not via an amount-only fudge. The payout debits amount+fee (20,300) from
        # available, so paid_out_total is the money-out figure the journal closing
        # foots against; charge fees are already inside collected_net.
        t = st["totals"]
        assert t["collected_net"] == 98_500 and t["paid_out"] == 20_000, f"[2] totals {t}"
        assert t["paid_out_total"] == 20_300, f"[2] paid_out_total should be amount+fee {t}"
        assert (t["payout_fees"] == 300 and t["charge_fees"] == 1_500
                and t["fees"] == 1_800), f"[2] fee split {t}"
        # The identity the statement prints: opening + collected_net - paid_out_total
        # == closing, EXACTLY (no manual '- payout_fee' correction).
        assert st["closing_balance"] == (
            st["opening_balance"] + t["collected_net"] - t["paid_out_total"]), \
            f"[2] closing {st['closing_balance']} must foot to opening+net-paid_out_total"
        print(f"[2] PASS — statement foots: opening {st['opening_balance']:,} + collected "
              f"{t['collected_net']:,} - paid out incl. fees {t['paid_out_total']:,} = closing "
              f"{st['closing_balance']:,}")

        # [3] PDF renders
        pdf = render_pdf(st)
        assert isinstance(pdf, (bytes, bytearray)) and pdf[:4] == b"%PDF", "[3] not a PDF"
        print(f"[3] PASS — render_pdf produced a {len(pdf):,}-byte PDF")

        # [3b] HTML summary uses the footing figure + discloses fees as a memo
        from app.services.statements import render_html
        html = render_html(st)
        assert "Paid out (incl. fees)" in html, "[3b] html money-out label"
        assert "20,300" in html, "[3b] html shows amount+fee, not amount-only"
        assert "Memo" in html and "1,800" in html, "[3b] html fee memo"
        print("[3b] PASS — HTML summary foots and discloses fees as a memo")

    # [4] API
    c = app.test_client()
    H = {"Authorization": "Bearer sk_live_k", "X-Timestamp": str(int(time.time()))}
    j = c.get(f"/v1/statements/{Y}-{M:02d}", headers=H)
    assert j.status_code == 200 and j.get_json()["totals"]["collected_net"] == 98_500, f"[4] json {j.status_code}"
    p = c.get(f"/v1/statements/{Y}-{M:02d}.pdf", headers=H)
    assert p.status_code == 200 and p.mimetype == "application/pdf" and p.data[:4] == b"%PDF", f"[4] pdf {p.status_code}"
    bad = c.get("/v1/statements/nonsense", headers=H)
    assert bad.status_code == 400, "[4] bad period should 400"
    print("[4] PASS — GET /v1/statements/YYYY-MM (JSON) + .pdf (PDF) + 400 on bad period")

    print("\nALL STATEMENT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
