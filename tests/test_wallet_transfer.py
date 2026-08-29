"""Wallet v1 — on-us transfer (pay another Samsoftpay account from your balance).

Money-correctness is the point. Proves:
  1. a transfer moves the exact amount: payer down, payee up
  2. the ledger stays ZERO-SUM and cached balances match the journal
  3. a payee Transaction is recorded (channel=WALLET, SUCCEEDED, settled_at set)
  4. insufficient balance is refused with NO movement (overdraft-safe)
  5. self-transfer is refused
  6. an unverified payer is refused on LIVE money (KYC gate)
  7. TEST and LIVE ledgers are separate (a sandbox transfer never touches live)
  8. resolve_payee finds by handle/email/id and refuses managed/inactive
  9. the dashboard route performs a transfer end-to-end
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="wallet_xfer_")
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
from app.models import Merchant, Transaction, TxnStatus, Channel, AccountType
from app.services import ledger
from app.services.wallet_transfer import transfer, resolve_payee

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def _fund(merchant_id, amount, is_test=False):
    rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None,
                                        currency="UGX", is_test=is_test)
    avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                         merchant_id=merchant_id, currency="UGX", is_test=is_test)
    ledger.post([(rail, +amount), (avail, -amount)], currency="UGX", memo="fund")
    db.session.commit()


def _avail(merchant_id, is_test=False):
    a = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                     merchant_id=merchant_id, currency="UGX", is_test=is_test)
    db.session.refresh(a)
    return -a.cached_balance


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False   # the dashboard-route check posts without a browser token
    with app.app_context():
        db.create_all()
        payer = Merchant(name="Payer Co", email="payer@x.com", public_key="pk_test_p",
                         secret_key="sk_live_p", test_secret_key="sk_test_p",
                         handle="payer", kyc_status="verified")
        payee = Merchant(name="Payee Co", email="payee@x.com", public_key="pk_test_e",
                         secret_key="sk_live_e", test_secret_key="sk_test_e",
                         handle="payee", kyc_status="verified")
        db.session.add_all([payer, payee]); db.session.commit()
        pid, eid = payer.id, payee.id
        _fund(pid, 100000)          # live available
        _fund(pid, 50000, is_test=True)   # sandbox available

        # 1 + 2 + 3: a valid transfer
        r = transfer(payer=payer, payee=payee, amount=30000, is_test=False, reference="inv-1")
        check("1. transfer ok", r.get("ok") is True)
        check("   payer available 100000 -> 70000", _avail(pid) == 70000)
        check("   payee available 0 -> 30000", _avail(eid) == 30000)
        mism = ledger.assert_balances_match()
        check("2. ledger zero-sum + caches match journal", not mism)
        txn = r["transaction"]
        got = db.session.get(Transaction, txn.id)
        check("3. payee Transaction recorded (WALLET, SUCCEEDED, settled)",
              got.merchant_id == eid and got.channel == Channel.WALLET
              and got.status == TxnStatus.SUCCEEDED and got.settled_at is not None)

        # 4. insufficient balance — no movement
        r = transfer(payer=payer, payee=payee, amount=999999, is_test=False)
        check("4. insufficient refused", r.get("error") == "insufficient_balance")
        check("   balances unchanged after refusal", _avail(pid) == 70000 and _avail(eid) == 30000)

        # 5. self-transfer refused
        r = transfer(payer=payer, payee=payer, amount=1000, is_test=False)
        check("5. self-transfer refused", r.get("error") == "cannot_send_to_yourself")

        # 6. KYC gate on live money
        payer.kyc_status = "pending"; db.session.commit()
        r = transfer(payer=payer, payee=payee, amount=1000, is_test=False)
        check("6. unverified payer refused on LIVE", r.get("error") == "payer_not_verified")
        # ...but sandbox is open (building never needs approval)
        r = transfer(payer=payer, payee=payee, amount=5000, is_test=True)
        check("7. sandbox transfer works while unverified", r.get("ok") is True)
        payer.kyc_status = "verified"; db.session.commit()

        # 7. mode isolation — the sandbox transfer above did NOT touch live
        check("   live balances untouched by sandbox transfer",
              _avail(pid) == 70000 and _avail(eid) == 30000)
        check("   sandbox moved: payer 50000->45000, payee 0->5000",
              _avail(pid, True) == 45000 and _avail(eid, True) == 5000)

        # 8. resolve_payee
        check("8a. resolve by @handle", resolve_payee("@payee") is not None and resolve_payee("@payee").id == eid)
        check("8b. resolve by email", resolve_payee("payee@x.com").id == eid)
        check("8c. resolve by id", resolve_payee(str(eid)).id == eid)
        check("8d. unknown -> None", resolve_payee("nobody") is None)
        # managed subaccount is not a valid payee
        managed = Merchant(name="Sub", email="sub@x.com", public_key="pk_test_s",
                           secret_key="sk_live_s", test_secret_key="sk_test_s",
                           handle="subacct", is_managed=True, parent_merchant_id=eid)
        db.session.add(managed); db.session.commit()
        check("8e. managed subaccount refused as payee", resolve_payee("@subacct") is None)

    # 9. dashboard route end-to-end
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(pid)
        sess["_fresh"] = True
    r = client.post("/dashboard/wallet/send",
                    data={"payee": "@payee", "amount": "10000", "reference": "route-test"},
                    follow_redirects=False)
    check("9. dashboard /wallet/send transfers (redirect)", r.status_code in (302, 303))
    with app.app_context():
        check("   payer 70000 -> 60000 after route send", _avail(pid) == 60000)

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
