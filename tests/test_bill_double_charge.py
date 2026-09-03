"""A fixed bill must not fire a second MoMo prompt while its first charge is
still in flight.

Bug (multi-agent hunt, medium): pay_bill_submit checked only
`bill.status in ('active',)` before create_charge. A bill flips to 'paid' only
when the rail CONFIRMS (_maybe_mark_bill_paid), so between prompt-sent and
confirmation the bill is still 'active' with a charge already in flight. A
reload / double-tap in that window fired a SECOND prompt and, on the payer
approving both, charged them twice for one bill — the route never looked at
bill.transaction_id.

What this proves:
  1. First submit fires exactly one charge and links it to the bill.
  2. A second submit while that charge is in flight (AUTHORIZED) does NOT create
     a second charge — it shows the existing one.
  3. If the prior charge FAILED, the bill re-opens: a fresh submit is allowed to
     fire a new charge (the dedupe is not a permanent lock).
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
# Keep the mock rail from completing during the test: the first charge stays
# AUTHORIZED (in flight) so the in-flight dedupe is exercised deterministically.
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "999"

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="billdc_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, Bill, BillCategory, Transaction, TxnStatus

import app.tasks.webhooks_task as _wt
_wt.deliver_webhook.delay = lambda *a, **k: None

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def txn_count(merchant_id):
    return Transaction.query.filter_by(merchant_id=merchant_id).count()


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False   # bills bp isn't CSRF-exempt
    with app.app_context():
        db.create_all()
        m = Merchant(name="Kampala High School", email="bills@x.com",
                     public_key="pk", secret_key="sk_live_b",
                     handle="khs", is_active=True, kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        m_id = m.id
        bill = Bill(public_id="bill_dc01", merchant_id=m_id,
                    category=BillCategory.SCHOOL_FEES, title="Term 3 fees",
                    account_ref="STU-100", amount=200_000, is_variable=False,
                    currency="UGX", status="active")
        db.session.add(bill)
        db.session.commit()
        bill_pub = bill.public_id

    client = app.test_client()

    # 1. First submit -> one charge, linked to the bill.
    r1 = client.post(f"/pay/@khs/bill/{bill_pub}",
                     data={"phone": "256783647260", "channel": "mtn_momo"})
    check("first submit accepted", r1.status_code == 200)
    with app.app_context():
        check("exactly one charge exists after the first submit",
              txn_count(m_id) == 1)
        b = Bill.query.filter_by(public_id=bill_pub).one()
        check("...and it is linked to the bill", b.transaction_id is not None)

    # 2. Second submit while the first charge is still in flight (AUTHORIZED).
    r2 = client.post(f"/pay/@khs/bill/{bill_pub}",
                     data={"phone": "256783647260", "channel": "mtn_momo"})
    check("second submit is accepted (shows existing, no error)", r2.status_code == 200)
    with app.app_context():
        check("NO second charge was created for the in-flight bill",
              txn_count(m_id) == 1)

    # 3. If the prior charge FAILED, the bill re-opens for a genuine retry.
    with app.app_context():
        b = Bill.query.filter_by(public_id=bill_pub).one()
        prior = db.session.get(Transaction, b.transaction_id)
        prior.status = TxnStatus.FAILED
        db.session.commit()
    r3 = client.post(f"/pay/@khs/bill/{bill_pub}",
                     data={"phone": "256783647260", "channel": "mtn_momo"})
    check("retry after a FAILED charge is accepted", r3.status_code == 200)
    with app.app_context():
        check("a new charge WAS fired after the prior one failed (now 2 total)",
              txn_count(m_id) == 2)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
    else:
        print(f"ALL BILL DOUBLE-CHARGE TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")
    # The mock rail scheduled a long (999s) non-daemon completion Timer to hold
    # the first charge in flight; it would keep the interpreter alive well past
    # the assertions. All checks are done — flush and hard-exit so the script
    # terminates instead of blocking on that thread.
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
