"""Public-route hardening from the KarlPOS-class audit.

  [1] Bill lookup (GET /pay/@<handle>/bills?ref=) no longer leaks a full customer
      name — it is masked to first-name + last-initial (e.g. "Jane D.").
  [2] Bill lookup is rate-limited, so it can't be used as an enumeration oracle
      to harvest a merchant's whole customer roster by iterating references.
  [3] profile_pay (GET /pay/@<handle>/pay) is rate-limited — an unauthenticated
      GET that WRITES a PaymentLink row must not be an unbounded junk-row cannon.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="pubhard_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, Bill, BillCategory

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    with app.app_context():
        db.create_all()
        m = Merchant(name="Kampala High", email="kh@x.com", public_key="pk",
                     secret_key="sk", kyc_status="verified", handle="khs")
        db.session.add(m)
        db.session.commit()
        db.session.add(Bill(public_id="bill_look1", merchant_id=m.id,
                            category=BillCategory.SCHOOL_FEES, title="Term 3",
                            account_ref="STU-0001", customer_name="Jane Doe",
                            amount=200000, currency="UGX", status="active"))
        db.session.commit()

    c = app.test_client()

    # [1] masking
    r = c.get("/pay/@khs/bills?ref=STU-0001")
    body = r.get_data(as_text=True)
    check("[1] lookup renders the masked name 'Jane D.'", "Jane D." in body)
    check("[1] the full customer name is NOT exposed", "Jane Doe" not in body)

    # [2] bill lookup is rate-limited (enumeration oracle closed)
    saw_429 = False
    for _ in range(30):
        if c.get("/pay/@khs/bills?ref=STU-0001").status_code == 429:
            saw_429 = True
            break
    check("[2] bill lookup returns 429 under rapid enumeration", saw_429)

    # [3] profile_pay (unauth write) is rate-limited
    saw_429 = False
    for _ in range(30):
        if c.get("/pay/@khs/pay?amount=500").status_code == 429:
            saw_429 = True
            break
    check("[3] profile_pay returns 429 under a rapid loop", saw_429)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL PUBLIC-ROUTE HARDENING TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
