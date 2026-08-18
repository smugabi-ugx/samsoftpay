"""Wallet top-up mode scoping — the pre-production sandbox top-up path.

Reported live: the wallet's "Get QR" led to a checkout page showing only
Crypto — the top-up link was created LIVE, and guardrail 14 correctly hides
mock MTN on live links (production MTN is still sandbox). The fix creates the
top-up link in SANDBOX mode while MTN is simulated, so the flow is testable —
but that is only safe if the settlement is scoped by the transaction's mode.

What this proves (the money case above all):
  1. While MTN is simulated, topup_momo creates a TEST-mode link, and its
     checkout page offers MTN (under RENDER=true, where live links hide it).
  2. Completing the sandbox top-up credits the SANDBOX available balance.
  3. THE MONEY CASE: the LIVE available balance is completely untouched —
     a mock payment must never mint real withdrawable money.
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
# RENDER is enabled AFTER create_app (guardrail 8 refuses sqlite on Render at
# boot; the rail guard reads the env var fresh per request).

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="topup_mode_test_")
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
from app.models import Account, AccountType, Merchant, PaymentLink, Transaction, TxnStatus

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def balance(app, merchant_id, is_test):
    with app.app_context():
        acct = Account.query.filter_by(
            merchant_id=merchant_id, type=AccountType.MERCHANT_AVAILABLE,
            is_test=is_test).first()
        return -acct.cached_balance if acct else 0


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False   # dashboard POST under test; CSRF itself isn't the subject
    os.environ["RENDER"] = "true"   # now safe — see note at top
    try:
        with app.app_context():
            db.create_all()
            m = Merchant(name="Topup Shop", email="topup@x.com",
                         public_key="pk_tu", secret_key="sk_tu",
                         test_public_key="pk_test_tu", test_secret_key="sk_test_tu",
                         kyc_status="verified")
            db.session.add(m); db.session.commit()
            mid = m.id

        client = app.test_client()
        # log in via the test client session (topup routes are login_required)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(mid)
            sess["_fresh"] = True

        live_before = balance(app, mid, False)

        # ---- 1. create the top-up; link must be TEST mode with MTN offered ----
        r = client.post("/dashboard/wallet/topup/momo", data={"topup_amount": "10000"},
                        follow_redirects=False)
        check("topup_momo accepted", r.status_code in (302, 303))
        with app.app_context():
            link = (PaymentLink.query.filter_by(merchant_id=mid)
                    .order_by(PaymentLink.id.desc()).first())
            check("top-up link created in SANDBOX mode while MTN is simulated",
                  link is not None and link.is_test is True)
            link_id = link.public_id

        page = client.get(f"/pay/{link_id}").data.decode()
        check("checkout page for the top-up OFFERS MTN (even with RENDER=true)",
              "MTN Mobile Money" in page)

        # ---- 2. pay it via the deterministic mock and settle ----
        client.post(f"/pay/{link_id}/submit",
                    data={"channel": "mtn_momo", "phone": "256783647260"})
        deadline = time.time() + 8
        settled = False
        while time.time() < deadline:
            with app.app_context():
                db.session.expire_all()
                l2 = PaymentLink.query.filter_by(public_id=link_id).one()
                if l2.transaction_id:
                    txn = db.session.get(Transaction, l2.transaction_id)
                    if txn.status == TxnStatus.SUCCEEDED:
                        settled = True
                        break
            time.sleep(0.25)
        check("sandbox top-up charge succeeded", settled)

        # trigger the settlement poll (what the QR page's JS calls)
        with app.app_context():
            from app.models import TopUpRequest
            topup = TopUpRequest.query.filter_by(merchant_id=mid).order_by(
                TopUpRequest.id.desc()).first()
            tid = topup.public_id
        client.get(f"/dashboard/wallet/topup/{tid}/status.json")

        # ---- 3. the ledgers ----
        sandbox_avail = balance(app, mid, True)
        live_after = balance(app, mid, False)
        check("sandbox available balance credited (amount minus fee)",
              sandbox_avail > 0)
        check("THE MONEY CASE: live available balance completely untouched",
              live_after == live_before)
    finally:
        os.environ.pop("RENDER", None)

    print()
    failed = [label for label, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for label in failed:
            print("  - " + label)
        sys.exit(1)
    print(f"All {len(CHECKS)} top-up mode checks passed.")


if __name__ == "__main__":
    main()
