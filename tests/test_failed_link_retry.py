"""A failed single-use payment link must be retryable, not a dead end (ux-design-1).

Before: a single-use link stays bound to its FAILED transaction, so /pay/<id>
redirects to the failed status page forever and "Try again" just reloaded it.
MoMo failures (wrong PIN, ignored prompt) are usually retryable — this was a
pure conversion leak.

What this proves:
  1. After a FAILED attempt, POST /retry re-opens the checkout form (200 at /pay/<id>).
  2. The retry pre-fills the phone the customer already typed.
  3. A fresh submit after retry succeeds and credits the merchant.
  4. /retry NEVER detaches a SUCCEEDED transaction (can't undo a real payment).
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="retry_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


import uuid

from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink, Transaction, TxnStatus

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def make_link(app, mid):
    with app.app_context():
        link = PaymentLink(public_id=f"lnk_{uuid.uuid4().hex[:12]}", merchant_id=mid,
                           amount=4000, currency="UGX", is_test=True, is_active=True,
                           allow_multiple_uses=False)
        db.session.add(link)
        db.session.commit()
        return link.public_id


def poll(c, pub, want, tries=40):
    for _ in range(tries):
        j = (c.get(f"/pay/{pub}/status.json").get_json() or {})
        s = (j.get("status") or "").lower()
        if s in ("succeeded", "failed"):
            return s
        time.sleep(0.3)
    return None


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Retry Co", email="retry@x.com", public_key="pk_r",
                     secret_key="sk_live_r", kyc_status="verified", handle="retry-co")
        db.session.add(m)
        db.session.commit()
        mid = m.id

    c = app.test_client()

    # 1. Fail a single-use link with the magic insufficient_funds number.
    pub = make_link(app, mid)
    c.post(f"/pay/{pub}/submit", data={"channel": "mtn_momo", "phone": "256700000001"})
    check("first attempt ends failed", poll(c, pub, "failed") == "failed")

    # Before retry: /pay/<id> redirects (single-use link bound to failed txn).
    r = c.get(f"/pay/{pub}", follow_redirects=False)
    check("failed single-use link redirects to status before retry", r.status_code in (301, 302, 303))

    # 2. Retry re-opens the form.
    rr = c.post(f"/pay/{pub}/retry", follow_redirects=False)
    check("retry POST redirects back to checkout", rr.status_code in (301, 302, 303))
    page = c.get(f"/pay/{pub}", follow_redirects=False)
    check("after retry, /pay/<id> renders the form again (200)", page.status_code == 200)
    check("retry pre-fills the typed phone", b"256700000001" in page.data)

    # 3. A fresh submit with a good number now succeeds.
    c.post(f"/pay/{pub}/submit", data={"channel": "mtn_momo", "phone": "256700000000"})
    check("retried payment succeeds", poll(c, pub, "succeeded") == "succeeded")

    # 4. Retry must NEVER detach a SUCCEEDED txn.
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=pub).first()
        bound_before = link.transaction_id
        check("link is bound to the succeeded txn", bound_before is not None)
    c.post(f"/pay/{pub}/retry", follow_redirects=False)
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=pub).first()
        txn = db.session.get(Transaction, link.transaction_id) if link.transaction_id else None
        check("retry did NOT detach the succeeded txn",
              link.transaction_id == bound_before and txn.status == TxnStatus.SUCCEEDED)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL FAILED-LINK-RETRY TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
