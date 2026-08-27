"""Customer receipts: email + SMS with the URA tax breakdown, best-effort.

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_receipts.py

  [1] a VAT-registered merchant's receipt shows the VAT portion WITHIN the amount
      paid (total == amount), via both email and SMS.
  [2] a merchant with no tax config gets a plain receipt (no VAT line).
  [3] send_receipt NEVER raises (a bad/None txn must not break the money flow).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from app import create_app
from app.extensions import db
from app.models import Merchant, Transaction, TxnStatus, Channel, TaxConfiguration, utcnow
from app.services import email_service, sms_service, receipts

EMAILS = []
SMS = []


def main():
    email_service.send_email = lambda to, subj, html, plain=None: EMAILS.append((to, subj, html, plain))
    sms_service.send_sms = lambda to, msg: (SMS.append((to, msg)) or True)
    receipts.send_email = email_service.send_email     # rebind (imported lazily inside)

    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="Kampala Coffee", email="k@x.com", public_key="pk",
                     secret_key="sk_live_k", handle="k", kyc_status="verified")
        db.session.add(m); db.session.commit()
        # VAT-registered, inclusive pricing
        db.session.add(TaxConfiguration(merchant_id=m.id, vat_enabled=True,
                                        vat_rate_bps=1800, tax_inclusive=True,
                                        vat_number="1000123456", business_name="Kampala Coffee Ltd"))
        db.session.commit()

        # amount 11,800 incl 18% VAT -> VAT = 11800*0.18/1.18 = 1800, total 11800
        t = Transaction(public_id="ch_r1", merchant_id=m.id, amount=11_800, fee_amount=200,
                        currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                        is_test=False, customer_phone="256780000001",
                        customer_email="buyer@example.com", merchant_reference="ORDER-1",
                        completed_at=utcnow())
        db.session.add(t); db.session.commit()

        EMAILS.clear(); SMS.clear()
        receipts.send_receipt(t)

        # [1] email + SMS sent, VAT shown, total == amount paid
        assert len(EMAILS) == 1, f"[1] expected 1 email, got {len(EMAILS)}"
        to, subj, html, plain = EMAILS[0]
        assert to == "buyer@example.com"
        assert "Kampala Coffee Ltd" in html
        assert "1,800" in html and "11,800" in html, f"[1] VAT/total missing: {plain}"
        assert "1000123456" in html, "[1] VAT number should appear"
        assert len(SMS) == 1 and "incl. VAT" in SMS[0][1] and "11,800" in SMS[0][1], f"[1] sms: {SMS}"
        print("[1] PASS — VAT-registered receipt: email+SMS, VAT 1,800 within total 11,800")

        # [2] merchant with no tax config -> plain receipt, no VAT line
        m2 = Merchant(name="Plain Shop", email="p@x.com", public_key="pk2",
                      secret_key="sk_live_p", handle="p", kyc_status="verified")
        db.session.add(m2); db.session.commit()
        t2 = Transaction(public_id="ch_r2", merchant_id=m2.id, amount=5_000, fee_amount=200,
                         currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                         is_test=False, customer_email="b2@example.com",
                         merchant_reference="ORDER-2", completed_at=utcnow())
        db.session.add(t2); db.session.commit()
        EMAILS.clear(); SMS.clear()
        receipts.send_receipt(t2)
        assert len(EMAILS) == 1 and "VAT" not in EMAILS[0][2], "[2] no-VAT merchant should have no VAT line"
        assert "5,000" in EMAILS[0][2], "[2] total should be the amount"
        print("[2] PASS — no-tax-config merchant gets a plain receipt (no VAT line)")

        # [3] never raises
        receipts.send_receipt(None)
        broken = Transaction(public_id="ch_x", merchant_id=999999, amount=1000,
                             currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED)
        receipts.send_receipt(broken)
        print("[3] PASS — send_receipt never raises (None + orphan txn handled)")

    print("\nALL RECEIPT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
