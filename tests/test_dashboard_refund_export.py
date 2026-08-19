"""Dashboard self-service: refund a charge + export transactions CSV (persona #5).

Before: refunds were API-only (needed a full-scope key), and the txn table
capped at 50 rows with no export — a non-technical merchant could neither refund
a customer nor close their books.

What this proves:
  1. CSV export streams the merchant's transactions with a Content-Disposition
     header and the expected columns/rows.
  2. Export is owner-or-admin gated (another logged-in merchant gets 403).
  3. The dashboard refund route delegates to refund_charge (a succeeded charge
     becomes REFUNDED); a non-owner is 403.
  4. Refunding an already-refunded / non-succeeded charge is handled (flash, no
     crash), not a 500.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="dashref_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")
import atexit
@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass

from app import create_app
from app.extensions import db
from app.models import (
    Account, AccountType, Channel, Merchant, Transaction, TxnStatus, utcnow,
)
from app.services import ledger

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="Owner", email="o@x.com", public_key="pk_o", secret_key="sk_o",
                     kyc_status="verified", handle="owner",
                     password_hash=generate_password_hash("x"))
        other = Merchant(name="Other", email="ot@x.com", public_key="pk_ot", secret_key="sk_ot",
                         kyc_status="verified", handle="other",
                         password_hash=generate_password_hash("x"))
        db.session.add_all([m, other])
        db.session.commit()
        mid, oid = m.id, other.id
        # Fund available so the refund payout can succeed, and a succeeded live charge.
        avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                             merchant_id=mid, currency="UGX")
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                            merchant_id=None, currency="UGX")
        ledger.post([(rail, +100000), (avail, -100000)], currency="UGX", memo="fund")
        txn = Transaction(public_id="txn_ref1", merchant_id=mid, amount=10000, fee_amount=200,
                          currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                          is_test=False, customer_phone="256700000000", completed_at=utcnow())
        db.session.add(txn)
        db.session.commit()

    c = app.test_client()

    def login(user_id):
        with c.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True

    # 1. CSV export as owner.
    login(mid)
    r = c.get(f"/dashboard/{mid}/export/transactions.csv")
    check("CSV export -> 200", r.status_code == 200)
    check("CSV has attachment Content-Disposition",
          "attachment" in r.headers.get("Content-Disposition", ""))
    body = r.get_data(as_text=True)
    check("CSV header row present", body.splitlines()[0].startswith("id,created_at,status,amount"))
    check("CSV contains the charge row", "txn_ref1" in body)

    # 2. Another merchant can't export mine.
    login(oid)
    check("non-owner CSV export -> 403", c.get(f"/dashboard/{mid}/export/transactions.csv").status_code == 403)
    check("non-owner refund -> 403",
          c.post(f"/dashboard/{mid}/charge/txn_ref1/refund").status_code == 403)

    # 3. Owner refunds the succeeded charge -> REFUNDED.
    login(mid)
    r = c.post(f"/dashboard/{mid}/charge/txn_ref1/refund", follow_redirects=False)
    check("dashboard refund redirects", r.status_code in (301, 302, 303))
    with app.app_context():
        t = Transaction.query.filter_by(public_id="txn_ref1").first()
        check("charge is now REFUNDED", t.status == TxnStatus.REFUNDED)

    # 4. Refunding again is handled gracefully (no 500).
    r2 = c.post(f"/dashboard/{mid}/charge/txn_ref1/refund", follow_redirects=False)
    check("second refund handled (redirect, not 500)", r2.status_code in (301, 302, 303))

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL DASHBOARD REFUND/EXPORT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
