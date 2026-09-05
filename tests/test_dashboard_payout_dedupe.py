"""Dashboard payout single-submit guard (audit: money-out double-pay).

The API payout endpoints require an Idempotency-Key; the dashboard single + bulk
payout forms had no such protection, so a double-click / resubmit / CSV re-upload
paid every recipient again. This adds a DB-atomic form-token claim.

Proves:
  [1] The forms render a hidden form_token.
  [2] Single payout: two submits with the SAME token create ONE payout; a
      different token is allowed (control).
  [3] Bulk payout: re-posting the same CSV with the same token pays each
      recipient once, not twice.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "999"   # keep payouts AUTHORIZED during the test
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="dashdedupe_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try:
        os.unlink(_P)
    except OSError:
        pass


from io import BytesIO
from app import create_app
from app.extensions import db
from app.models import Account, AccountType, Merchant, Payout
from app.services import ledger

import app.tasks.webhooks_task as _wt
_wt.deliver_webhook.delay = lambda *a, **k: None

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def payouts(mid):
    return Payout.query.filter_by(merchant_id=mid).count()


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="Payer Co", email="pay@x.com", public_key="pk",
                     secret_key="sk_live", kyc_status="verified", handle="payer",
                     is_active=True, password_hash=generate_password_hash("x"))
        db.session.add(m)
        db.session.commit()
        mid = m.id
        av = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                          merchant_id=mid, currency="UGX", is_test=False)
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                            merchant_id=None, currency="UGX", is_test=False)
        ledger.post([(rail, +1_000_000), (av, -1_000_000)], currency="UGX", memo="fund")
        db.session.commit()

    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(mid)
        s["_fresh"] = True

    # [1] the form renders a token
    r = c.get(f"/dashboard/{mid}/new-payout")
    check("[1] single-payout form renders a form_token field",
          b'name="form_token"' in r.data)

    # [2] single payout dedupe
    c.post(f"/dashboard/{mid}/new-payout",
           data={"amount": "10000", "phone": "256780000011", "form_token": "tokS"})
    with app.app_context():
        check("[2] first single submit creates one payout", payouts(mid) == 1)
    r = c.post(f"/dashboard/{mid}/new-payout",
               data={"amount": "10000", "phone": "256780000011", "form_token": "tokS"})
    with app.app_context():
        check("[2] resubmit with the SAME token creates NO second payout", payouts(mid) == 1)
    check("[2] the duplicate shows an 'already submitted' message",
          b"already submitted" in r.data.lower())
    # control: a fresh token is allowed
    c.post(f"/dashboard/{mid}/new-payout",
           data={"amount": "10000", "phone": "256780000011", "form_token": "tokS2"})
    with app.app_context():
        check("[2] a different token is allowed (not over-deduped)", payouts(mid) == 2)

    # [3] bulk payout dedupe
    csv_bytes = b"name,phone,amount\nJane,256780000021,10000\nJohn,256780000022,10000\n"
    c.post(f"/dashboard/{mid}/bulk-payout",
           data={"csv": (BytesIO(csv_bytes), "payees.csv"), "form_token": "tokB"},
           content_type="multipart/form-data")
    with app.app_context():
        after_first = payouts(mid)
        check("[3] bulk submit paid the 2 CSV rows", after_first == 4)   # 2 prior + 2
    r = c.post(f"/dashboard/{mid}/bulk-payout",
               data={"csv": (BytesIO(csv_bytes), "payees.csv"), "form_token": "tokB"},
               content_type="multipart/form-data")
    with app.app_context():
        check("[3] re-posting the same batch+token pays NO one twice", payouts(mid) == 4)
    check("[3] the duplicate batch shows an 'already submitted' message",
          b"already submitted" in r.data.lower())

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
    else:
        print(f"ALL DASHBOARD PAYOUT DEDUPE TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")
    sys.stdout.flush()
    os._exit(1 if failed else 0)   # mock rail Timer thread would hold the process open


if __name__ == "__main__":
    main()
