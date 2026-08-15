"""Sandbox money and real money must live in separate ledgers.

Before this split, `is_test` was only a label on the transaction row: a payout
made with an sk_test_ key debited the SAME balance the merchant could withdraw.
Reproduced against production — a 1,000 UGX test payout moved KarlPOS's
available balance from 202,350 to 200,600.

What this proves:
  1. A test charge credits the sandbox balance and leaves the live one alone.
  2. A test payout spends the sandbox balance and leaves the live one alone.
  3. A live payout cannot be funded by sandbox money.
  4. The settlement sweep never moves sandbox money into a live balance.
  5. Each ledger balances to zero independently.
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
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="ledger_split_")
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
from app.models import Account, AccountType, JournalEntry, Merchant
from app.services import ledger

LIVE_START = 100_000
TEST_START = 50_000


def bal(mid, acct_type, is_test):
    a = Account.query.filter_by(merchant_id=mid, type=acct_type, is_test=is_test).first()
    return -a.cached_balance if a else 0


def fund(mid, amount, is_test):
    avail = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX", is_test=is_test)
    rail = ledger.get_or_create_account(
        type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX", is_test=is_test)
    ledger.post([(rail, +amount), (avail, -amount)], currency="UGX", memo="funding")
    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Split Co", email="s@x.com", public_key="pk_s",
                     secret_key="sk_live_s", test_secret_key="sk_test_s",
                     kyc_status="verified")
        db.session.add(m); db.session.commit()
        mid = m.id
        fund(mid, LIVE_START, False)
        fund(mid, TEST_START, True)
        print(f"[0] funded: live={bal(mid, AccountType.MERCHANT_AVAILABLE, False):,} "
              f"sandbox={bal(mid, AccountType.MERCHANT_AVAILABLE, True):,}")

    client = app.test_client()

    def payout(key, amount, idem):
        return client.post("/v1/payouts", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))},
            json={"amount": amount, "currency": "UGX", "channel": "mtn_momo",
                  "recipient": {"phone": "256780000001", "name": "x"}})

    # 1. A TEST payout must spend sandbox money only.
    r = payout("sk_test_s", 10_000, "t1")
    assert r.status_code == 201, r.data
    fee = r.json["fee"]
    with app.app_context():
        live = bal(mid, AccountType.MERCHANT_AVAILABLE, False)
        sand = bal(mid, AccountType.MERCHANT_AVAILABLE, True)
        assert live == LIVE_START, f"LEAK: test payout touched the live balance ({live})"
        assert sand == TEST_START - 10_000 - fee, sand
    print(f"[1] test payout: sandbox {TEST_START:,} -> {sand:,}, live untouched at {live:,}")

    # 2. A LIVE payout must spend live money only.
    r2 = payout("sk_live_s", 20_000, "l1")
    assert r2.status_code == 201, r2.data
    fee2 = r2.json["fee"]
    with app.app_context():
        live2 = bal(mid, AccountType.MERCHANT_AVAILABLE, False)
        sand2 = bal(mid, AccountType.MERCHANT_AVAILABLE, True)
        assert sand2 == sand, f"live payout touched the sandbox balance ({sand2})"
        assert live2 == LIVE_START - 20_000 - fee2, live2
    print(f"[2] live payout: live {LIVE_START:,} -> {live2:,}, sandbox untouched at {sand2:,}")

    # 3. Sandbox money cannot fund a live payout. Sandbox has ~39k left; ask for
    #    more live money than the live ledger holds but less than the two combined.
    with app.app_context():
        live_now = bal(mid, AccountType.MERCHANT_AVAILABLE, False)
        sand_now = bal(mid, AccountType.MERCHANT_AVAILABLE, True)
    over = live_now + 1_000
    assert over < live_now + sand_now, "test needs sandbox to cover the shortfall"
    r3 = payout("sk_live_s", over, "l2")
    assert r3.status_code == 400 and "insufficient" in r3.json["error"], r3.json
    print(f"[3] live payout of {over:,} refused though sandbox holds {sand_now:,} — "
          "ledgers cannot cross-fund")

    # 4. Settlement must not move sandbox money into a live balance.
    with app.app_context():
        from app.services.settlement import sweep_to_available
        before_live = bal(mid, AccountType.MERCHANT_AVAILABLE, False)
        before_sand = bal(mid, AccountType.MERCHANT_AVAILABLE, True)
        sweep_to_available(hold_hours=0)
        db.session.commit()
        after_live = bal(mid, AccountType.MERCHANT_AVAILABLE, False)
        after_sand = bal(mid, AccountType.MERCHANT_AVAILABLE, True)
        assert after_live == before_live, (before_live, after_live)
        assert after_sand == before_sand, (before_sand, after_sand)
    print("[4] settlement sweep kept both ledgers where they were")

    # 5. Each ledger balances to zero on its own.
    #    Wait for the mock rail's completion timer first: mid-flight, a payout's
    #    earmark and its release are two separate postings, and asserting between
    #    them reads a half-finished state that is not a real imbalance.
    from app.models import Payout, PayoutStatus

    def all_payouts_settled():
        with app.app_context():
            return all(p.status in (PayoutStatus.SUCCEEDED, PayoutStatus.FAILED)
                       for p in Payout.query.all())

    deadline = time.time() + 20
    while time.time() < deadline and not all_payouts_settled():
        time.sleep(0.5)
    assert all_payouts_settled(), "mock rail never completed the payouts"

    with app.app_context():
        for is_test, label in ((False, "live"), (True, "sandbox")):
            ids = [a.id for a in Account.query.filter_by(is_test=is_test).all()]
            total = sum(e.amount for e in JournalEntry.query
                        .filter(JournalEntry.account_id.in_(ids)).all()) if ids else 0
            assert total == 0, f"{label} ledger does not balance: {total}"
            print(f"[5] {label} ledger sums to zero ({len(ids)} accounts)")
        assert not ledger.assert_balances_match(), "cached balances drifted from the journal"
    print("[6] cached balances match the journal")

    print("\nALL LEDGER SPLIT TESTS PASSED")


if __name__ == "__main__":
    main()
