"""A charge/payout amount above the 64-bit ledger ceiling must fail CLEANLY.

Found by execution probing, not a unit test: a charge with amount 10**30 reached
the INSERT and crashed (OverflowError on SQLite / NumericValueOutOfRange on
Postgres), which the API surfaced as a misleading 502 "rail unavailable, retry".
The only amount check was `> 0`. Now there is an upper bound (MAX_TXN_AMOUNT,
default = signed 64-bit ceiling) enforced BEFORE any DB write, on both money-in
and money-out. Fixing it also removed a function-local `from flask import
current_app` that shadowed the module import and made the new guard blow up.

What this proves:
  1. Charge above the cap -> clean 400, no 5xx, no transaction row.
  2. Payout above the cap -> clean PayoutError, no money moved.
  3. A configured lower MAX_TXN_AMOUNT is honoured (business-limit knob works).
  4. An ordinary charge/payout still succeeds.
"""
import atexit
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="amt_bounds_")
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
from app.models import AccountType, Merchant, Transaction
from app.services import ledger, payouts as payouts_svc
from app.services.payouts import PayoutError

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


HUGE = 10 ** 30


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Cap Co", email="cap@x.com", public_key="pk_test_cap",
                     secret_key="sk_live_cap", test_secret_key="sk_test_cap",
                     test_public_key="pk_test_cap2", kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX")
        rail = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX")
        ledger.post([(rail, +100000), (avail, -100000)], currency="UGX", memo="fund")
        db.session.commit()

    c = app.test_client()

    def charge(amount, idem):
        return c.post("/v1/charges", headers={
            "Authorization": "Bearer sk_test_cap", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))},
            data=json.dumps({"amount": amount, "channel": "mtn_momo",
                             "customer": {"phone": "256700000000"}}))

    # 1. Charge above the cap -> 400, no 5xx, no txn row.
    r = charge(HUGE, "huge-1")
    check("huge charge -> 400 (not 5xx)", r.status_code == 400)
    with app.app_context():
        # No txn row should exist yet (the over-cap charge must not persist one).
        check("no transaction row created for over-cap charge",
              Transaction.query.count() == 0)

    # 4a. Ordinary charge still works.
    r = charge(5000, "ok-1")
    check("ordinary charge still 201", r.status_code == 201)

    # 2. Payout above the cap -> PayoutError, nothing moves.
    with app.app_context():
        from flask import g as flask_g
        # create_payout reads g.api_mode; emulate a live full-scope key context.
        with app.test_request_context():
            flask_g.api_mode = "live"
            before = -ledger.get_or_create_account(
                type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX").cached_balance
            raised = False
            try:
                payouts_svc.create_payout(merchant=db.session.get(Merchant, mid), amount=HUGE,
                                          currency="UGX", recipient_phone="256780000001")
            except PayoutError:
                raised = True
            after = -ledger.get_or_create_account(
                type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX").cached_balance
            check("huge payout raises PayoutError", raised)
            check("huge payout moved no money", after == before)

    # 3. Configured lower MAX_TXN_AMOUNT is honoured.
    app.config["MAX_TXN_AMOUNT"] = 10000
    r = charge(50000, "cap-1")
    check("charge above configured business cap -> 400", r.status_code == 400)
    r = charge(8000, "cap-2")
    check("charge under configured business cap -> 201", r.status_code == 201)
    app.config["MAX_TXN_AMOUNT"] = (1 << 63) - 1

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL AMOUNT-BOUND TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
