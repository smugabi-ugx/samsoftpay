"""GET /v1/balance — what Samsoftpay is holding for a merchant.

This endpoint exists so a platform built on Samsoftpay (KarlPOS, TK Vending) can
reconcile its own sub-ledger against the cash actually held here. Without it the
asset side of that reconciliation has to be typed in by hand.

What this proves:
  1. It reports what the merchant is owed as a POSITIVE number (the accounts are
     credit-normal and store it negative).
  2. available + pending = total.
  3. An sk_test_ key sees the sandbox ledger ONLY. Reconciling real liabilities
     against sandbox money would be worse than having no endpoint at all.
  4. An sk_live_ key sees the live ledger only.
  5. It sums the JOURNAL, not cached_balance — so it still reports the truth when
     the cache has drifted, and says `consistent: false` when it has.
  6. Multiple currencies come back separately, never summed together.
  7. It requires auth, and one merchant cannot read another's balance.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="balance_ep_")
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

LIVE_UGX = 250_000
TEST_UGX = 40_000
LIVE_KES = 7_500


def fund(mid, amount, is_test, currency="UGX", acct_type=AccountType.MERCHANT_AVAILABLE):
    acct = ledger.get_or_create_account(
        type=acct_type, merchant_id=mid, currency=currency, is_test=is_test)
    rail = ledger.get_or_create_account(
        type=AccountType.RAIL_CLEARING, merchant_id=None, currency=currency, is_test=is_test)
    ledger.post([(rail, +amount), (acct, -amount)], currency=currency, memo="funding")
    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Balance Co", email="b@x.com", public_key="pk_b",
                     secret_key="sk_live_b", test_secret_key="sk_test_b",
                     kyc_status="verified")
        other = Merchant(name="Other Co", email="o@x.com", public_key="pk_o",
                         secret_key="sk_live_o", test_secret_key="sk_test_o",
                         kyc_status="verified")
        db.session.add_all([m, other]); db.session.commit()
        mid, oid = m.id, other.id

        fund(mid, LIVE_UGX, False)
        fund(mid, TEST_UGX, True)
        fund(mid, LIVE_KES, False, currency="KES")
        fund(mid, 12_000, False, acct_type=AccountType.MERCHANT_PENDING)
        fund(oid, 999_999, False)   # another merchant's money

    client = app.test_client()

    def balance(key):
        return client.get("/v1/balance", headers={"Authorization": f"Bearer {key}"})

    # 1 + 2. Live key: positive numbers, and the parts add up.
    r = balance("sk_live_b")
    assert r.status_code == 200, r.data
    body = r.json
    assert body["mode"] == "live", body
    ugx = next(b for b in body["balances"] if b["currency"] == "UGX")
    assert ugx["available"] == LIVE_UGX, ugx
    assert ugx["pending"] == 12_000, ugx
    assert ugx["total"] == LIVE_UGX + 12_000, ugx
    print(f"[1] live UGX: available={ugx['available']:,} pending={ugx['pending']:,} "
          f"total={ugx['total']:,}")

    # 3. Sandbox key sees sandbox money only.
    rt = balance("sk_test_b")
    assert rt.status_code == 200, rt.data
    assert rt.json["mode"] == "test", rt.json
    tugx = next(b for b in rt.json["balances"] if b["currency"] == "UGX")
    assert tugx["available"] == TEST_UGX, tugx
    assert tugx["total"] == TEST_UGX, tugx
    assert all(b["currency"] != "KES" for b in rt.json["balances"]), \
        "sandbox key leaked a live-only currency"
    print(f"[3] sandbox UGX: {tugx['available']:,} (live {LIVE_UGX:,} not visible)")

    # 4. Live key does not see sandbox money.
    assert ugx["available"] != TEST_UGX
    assert ugx["available"] == LIVE_UGX, "live balance contaminated by sandbox"
    print("[4] live key does not see sandbox money")

    # 6. Currencies are reported separately, never summed.
    kes = next(b for b in body["balances"] if b["currency"] == "KES")
    assert kes["total"] == LIVE_KES, kes
    assert {b["currency"] for b in body["balances"]} == {"UGX", "KES"}, body["balances"]
    print(f"[6] KES reported separately: {kes['total']:,}")

    # 5. Journal is the source of truth. Corrupt the cache and the endpoint must
    #    still report the real figure, and flag the disagreement.
    assert body["consistent"] is True, "should be consistent before tampering"
    with app.app_context():
        acct = Account.query.filter_by(
            merchant_id=mid, type=AccountType.MERCHANT_AVAILABLE,
            currency="UGX", is_test=False).one()
        acct.cached_balance = acct.cached_balance + 99_999   # cache now lies
        db.session.commit()

    r2 = balance("sk_live_b")
    ugx2 = next(b for b in r2.json["balances"] if b["currency"] == "UGX")
    assert ugx2["available"] == LIVE_UGX, \
        f"endpoint trusted the corrupted cache: {ugx2['available']}"
    assert r2.json["consistent"] is False, "drift not reported"
    print(f"[5] cache corrupted by 99,999 — still reports {ugx2['available']:,}, "
          f"consistent=false")

    # 7. Auth is required, and merchants are isolated.
    assert client.get("/v1/balance").status_code == 401
    assert balance("sk_live_nonsense").status_code == 401
    ro = balance("sk_live_o")
    o_ugx = next(b for b in ro.json["balances"] if b["currency"] == "UGX")
    assert o_ugx["available"] == 999_999, o_ugx
    assert o_ugx["available"] != LIVE_UGX, "merchant isolation broken"
    print(f"[7] no auth -> 401, bad key -> 401, other merchant sees only its own "
          f"{o_ugx['available']:,}")

    print("\nAll balance endpoint checks passed.")


if __name__ == "__main__":
    main()
