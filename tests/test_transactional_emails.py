"""Transactional emails for the events that had none: merchant payment-received,
refund issued (to customer), KYC decision (approved / rejected / reverify).

Proves each fires to the right recipient with the right content, is a no-op when
there's no address, and — critically — is BEST-EFFORT: a send failure never
raises (these hang off the money path).
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="txn_emails_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, Transaction, TxnStatus, Channel
from app.services import email_service, emails

CHECKS = []
SENT = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def _capture(to, subject, html, plain=None):
    SENT.append({"to": to, "subject": subject, "html": html, "plain": plain})


def _boom(*a, **k):
    raise RuntimeError("SMTP down")


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        # Route send_email to our capture (emails.py imports it lazily each call).
        email_service.send_email = _capture

        m = Merchant(name="Store Co", email="merchant@x.com", public_key="pk_test_e",
                     secret_key="sk_live_e", test_secret_key="sk_test_e",
                     kyc_status="verified")
        db.session.add(m); db.session.commit()

        txn = Transaction(public_id=f"txn_{uuid.uuid4().hex[:12]}", merchant_id=m.id,
                          amount=100000, fee_amount=1500, currency="UGX",
                          channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                          is_test=False, customer_phone="256700000001",
                          customer_email="buyer@x.com", merchant_reference="ORDER-9")
        db.session.add(txn); db.session.commit()

        # 1. merchant payment-received -> merchant, shows net (100000-1500)
        SENT.clear()
        ok = emails.email_merchant_payment_received(txn)
        check("1. merchant payment-received sent to merchant", ok and SENT and SENT[0]["to"] == "merchant@x.com")
        check("   shows net UGX 98,500", SENT and "98,500" in SENT[0]["html"])

        # 2. refund issued -> customer
        SENT.clear()
        ok = emails.email_refund_issued(txn, 100000)
        check("2. refund email sent to the customer", ok and SENT and SENT[0]["to"] == "buyer@x.com")
        check("   subject mentions the refund amount", SENT and "100,000" in SENT[0]["subject"])

        # 3. KYC decisions
        for decision, needle in [("approved", "verified"), ("rejected", "changes"),
                                 ("reverify", "re-verification")]:
            SENT.clear()
            ok = emails.email_kyc_decision(m, decision, "please fix your TIN")
            check(f"3. KYC '{decision}' email sent to merchant",
                  ok and SENT and SENT[0]["to"] == "merchant@x.com")
            check(f"   '{decision}' subject/body reflects the outcome",
                  SENT and needle in (SENT[0]["subject"] + SENT[0]["html"]).lower())

        # 4. no recipient -> silent no-op
        SENT.clear()
        m.email = None
        check("4a. no merchant email -> no send, returns False",
              emails.email_merchant_payment_received(txn) is False and not SENT)
        txn.customer_email = None
        check("4b. no customer email -> refund no-op", emails.email_refund_issued(txn) is False)

        # 5. BEST-EFFORT: a send failure must NOT raise
        m.email = "merchant@x.com"
        email_service.send_email = _boom
        try:
            r = emails.email_merchant_payment_received(txn)
            check("5. send failure is swallowed (returns False, no raise)", r is False)
        except Exception as e:
            check("5. send failure is swallowed (returns False, no raise)", False)
            print("   raised:", e)

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
