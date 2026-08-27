"""GET /v1/charges query-by-customer filters (Pesapal-style reconciliation).

Run: MOMO_USE_REAL=0 .venv\Scripts\python.exe tests\test_charges_query_filters.py

phone matches on the LAST 9 digits (0780…/256780…/+256 780… all resolve to the
same subscriber); email is case-insensitive exact. Both mode-scoped and never
another merchant's rows.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import Merchant, Transaction, TxnStatus, Channel, utcnow


def main():
    app = create_app({"WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="K", email="k@x.com", public_key="pk", secret_key="sk_live_kx",
                     handle="k", kyc_status="verified", password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        # A different merchant whose rows must NEVER appear.
        other = Merchant(name="O", email="o@x.com", public_key="pk2", secret_key="sk_live_ox",
                         handle="o", kyc_status="verified", password_hash=generate_password_hash("x"))
        db.session.add(other); db.session.commit()
        rows = [
            (m.id, "256780111222", "a@x.com", "R1", False),
            (m.id, "0780111222",   "a@x.com", "R2", False),
            (m.id, "256700999888", "b@x.com", "R3", False),
            (m.id, "256780111222", "a@x.com", "T1", True),   # sandbox — hidden from a live key
            (other.id, "256780111222", "a@x.com", "X1", False),  # another merchant — never visible
        ]
        for mid, ph, em, ref, is_test in rows:
            db.session.add(Transaction(
                public_id="ch_" + ref, merchant_id=mid, amount=1000, fee_amount=200,
                currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                is_test=is_test, customer_phone=ph, customer_email=em,
                merchant_reference=ref, completed_at=utcnow()))
        db.session.commit()

    c = app.test_client()
    H = {"Authorization": "Bearer sk_live_kx", "X-Timestamp": str(int(time.time()))}

    def refs(url):
        return sorted(t["reference"] for t in c.get(url, headers=H).get_json()["data"])

    assert refs("/v1/charges?phone=256780111222") == ["R1", "R2"], "[phone] exact/256 form"
    assert refs("/v1/charges?phone=0780111222") == ["R1", "R2"], "[phone] leading-0 form"
    assert refs("/v1/charges?phone=%2B256%20780%20111%20222") == ["R1", "R2"], "[phone] +256 spaced"
    assert refs("/v1/charges?phone=256700999888") == ["R3"], "[phone] distinct number"
    assert refs("/v1/charges?email=A@X.COM") == ["R1", "R2"], "[email] case-insensitive"
    assert refs("/v1/charges?email=b@x.com") == ["R3"], "[email] exact"
    assert refs("/v1/charges?phone=780111222&email=a@x.com") == ["R1", "R2"], "[combined]"
    assert refs("/v1/charges") == ["R1", "R2", "R3"], "[unfiltered] live only, own rows only"
    # Live key never sees sandbox (T1) or another merchant (X1) — proven by their absence above.
    print("PASS — phone(last-9, all formats) + email(ci) + combined; mode-scoped; own-rows-only")
    print("\nALL CHARGES-QUERY-FILTER ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
