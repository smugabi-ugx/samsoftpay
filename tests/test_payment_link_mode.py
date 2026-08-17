"""A test-mode payment link must behave like a test-mode charge, end to end.

The bug: PaymentLink had no is_test column, so the public checkout page/submit
handler (no auth — a customer's browser, not a merchant's API key) had nothing
to read and hardcoded g.api_mode = "live" for every link, regardless of which
key created it. Two consequences, both checked here:

  1. Money: a link made with an sk_test_ key posted its charge to the LIVE
     ledger, not sandbox — the same class of bug guardrail 12 fixed for
     Transaction/Payout/Account, just missed on PaymentLink.
  2. The simulated-rail guard (rail_guard, PR #16) had no way to exempt a
     test-mode link either, so MTN's mock — the entire point of a sandbox —
     was blocked exactly like a real live charge would be.

Run with RENDER=true to actually exercise the guard (it is a no-op otherwise).
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
# NOT set yet: create_app()'s boot guard (guardrail 8) correctly refuses to
# boot with RENDER=true against sqlite. The rail guard we're testing reads
# os.environ["RENDER"] fresh on every request, not just at boot — so RENDER
# is turned on further down, AFTER the app (and its sqlite DB) already exist.

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="link_mode_test_")
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
from app.models import Merchant, PaymentLink, Transaction

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    os.environ["RENDER"] = "true"   # now safe to turn on — see note above
    with app.app_context():
        db.create_all()
        db.session.add(Merchant(
            name="Mode Test Shop", email="modetest@x.com",
            public_key="pk_mt", secret_key="sk_mt",
            test_public_key="pk_test_mt", test_secret_key="sk_test_mt",
            kyc_status="verified",
        ))
        db.session.commit()

    client = app.test_client()
    ts = str(int(time.time()))

    # ---- 1. a link made with the TEST key ----
    r = client.post("/v1/payment-links",
                    headers={"Authorization": "Bearer sk_test_mt",
                             "Content-Type": "application/json", "X-Timestamp": ts},
                    json={"amount": 2500, "description": "Coca-Cola 300ml"})
    assert r.status_code == 201, r.data
    test_link_id = r.json["id"]

    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=test_link_id).one()
        check("PaymentLink created via sk_test_ is stored is_test=True", link.is_test is True)

    # ---- 2. its checkout page offers MTN (the guard exempts test mode) ----
    page = client.get(f"/pay/{test_link_id}").data.decode()
    check("test-mode checkout page offers MTN even with RENDER=true",
          "MTN Mobile Money" in page)

    # ---- 3. paying it settles in the SANDBOX ledger, not live ----
    r2 = client.post(f"/pay/{test_link_id}/submit",
                     data={"channel": "mtn_momo", "phone": "256783647260"})
    assert r2.status_code in (302, 303), r2.status_code

    deadline = time.time() + 8
    txn = None
    while time.time() < deadline:
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=test_link_id).one()
            if link.transaction_id:
                txn = db.session.get(Transaction, link.transaction_id)
                if txn.status.value in ("succeeded", "failed"):
                    break
        time.sleep(0.25)
    check("charge from a test-mode link settled", txn is not None and txn.status.value == "succeeded")
    check("...and is tagged is_test — NOT posted to the live ledger",
          txn is not None and txn.is_test is True)

    # ---- 4. a link made with the LIVE key still gets the guard's full force ----
    r3 = client.post("/v1/payment-links",
                     headers={"Authorization": "Bearer sk_mt",
                              "Content-Type": "application/json", "X-Timestamp": ts},
                     json={"amount": 2500, "description": "Coca-Cola 300ml"})
    live_link_id = r3.json["id"]
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=live_link_id).one()
        check("PaymentLink created via sk_live_ is stored is_test=False", link.is_test is False)

    live_page = client.get(f"/pay/{live_link_id}").data.decode()
    check("live-mode checkout page does NOT offer MTN (no real rail active)",
          "MTN Mobile Money" not in live_page)
    check("...and does not offer Airtel/Card either — the guard is unweakened",
          "Airtel Money" not in live_page and ">Card<" not in live_page.replace("Card (", "("))

    print()
    failed = [label for label, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for label in failed:
            print("  - " + label)
        sys.exit(1)
    print(f"All {len(CHECKS)} payment-link mode checks passed.")


if __name__ == "__main__":
    main()
