"""GET /v1/charges and /v1/payouts: cursor pagination, filters, mode + tenant scoping.

Persona finding: every GET was fetch-by-id only, so a partner who missed a
webhook could never find a charge/payout. These add listing for reconciliation.

What this proves:
  1. Lists this merchant's charges, newest first, with cursor pagination.
  2. next_cursor + starting_after walk the full set without overlap/gaps.
  3. Mode-scoped: a test key sees only test rows.
  4. Tenant-scoped: never returns another merchant's rows.
  5. status filter works; bad status/limit/cursor -> 400.
  6. Payouts list works the same way.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="listep_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")
import atexit
@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass

from app import create_app
from app.extensions import db
from app.models import Channel, Merchant, Payout, PayoutStatus, Transaction, TxnStatus

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="A", email="a@x.com", public_key="pk_a", secret_key="sk_live_a",
                     test_secret_key="sk_test_a", handle="a")
        other = Merchant(name="B", email="b@x.com", public_key="pk_b", secret_key="sk_live_b",
                         test_secret_key="sk_test_b", handle="b")
        db.session.add_all([m, other])
        db.session.commit()
        mid, oid = m.id, other.id
        # 5 live + 3 test charges for A, plus 2 live charges for B (must never appear).
        for i in range(5):
            db.session.add(Transaction(public_id=f"txn_live_{i}", merchant_id=mid, amount=1000 + i,
                                       currency="UGX", channel=Channel.MTN_MOMO,
                                       status=TxnStatus.SUCCEEDED if i % 2 else TxnStatus.FAILED,
                                       is_test=False, merchant_reference=f"ord-{i}"))
        for i in range(3):
            db.session.add(Transaction(public_id=f"txn_test_{i}", merchant_id=mid, amount=50 + i,
                                       currency="UGX", channel=Channel.MTN_MOMO,
                                       status=TxnStatus.SUCCEEDED, is_test=True))
        for i in range(2):
            db.session.add(Transaction(public_id=f"txn_other_{i}", merchant_id=oid, amount=999,
                                       currency="UGX", channel=Channel.MTN_MOMO,
                                       status=TxnStatus.SUCCEEDED, is_test=False))
        for i in range(3):
            db.session.add(Payout(public_id=f"po_{i}", merchant_id=mid, amount=2000, currency="UGX",
                                  channel=Channel.MTN_MOMO, status=PayoutStatus.SUCCEEDED,
                                  is_test=False, recipient_phone="256780000001"))
        db.session.commit()

    c = app.test_client()

    def H(key):
        return {"Authorization": f"Bearer {key}"}

    # 1/3/4. Live key lists only A's 5 live charges (not test, not B's).
    r = c.get("/v1/charges?limit=100", headers=H("sk_live_a"))
    ids = [d["id"] for d in r.json["data"]]
    check("live key lists exactly A's 5 live charges",
          len(ids) == 5 and all(i.startswith("txn_live_") for i in ids))
    check("no test rows leaked to a live key", not any("test" in i for i in ids))
    check("no other merchant's rows leaked", not any("other" in i for i in ids))
    check("newest first (txn_live_4 before txn_live_0)", ids[0] == "txn_live_4" and ids[-1] == "txn_live_0")

    # 3. Test key sees only the 3 test charges.
    rt = c.get("/v1/charges?limit=100", headers=H("sk_test_a"))
    tids = [d["id"] for d in rt.json["data"]]
    check("test key lists exactly A's 3 test charges",
          len(tids) == 3 and all(i.startswith("txn_test_") for i in tids))

    # 2. Cursor pagination walks the full live set without gaps/overlap.
    seen = []
    cursor = None
    for _ in range(10):
        url = "/v1/charges?limit=2" + (f"&starting_after={cursor}" if cursor else "")
        page = c.get(url, headers=H("sk_live_a")).json
        seen += [d["id"] for d in page["data"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    check("pagination returns all 5 live charges exactly once",
          sorted(seen) == sorted([f"txn_live_{i}" for i in range(5)]))

    # 5. status filter + bad inputs.
    rs = c.get("/v1/charges?status=succeeded&limit=100", headers=H("sk_live_a"))
    check("status filter returns only succeeded", all(d["status"] == "succeeded" for d in rs.json["data"]))
    check("bad status -> 400", c.get("/v1/charges?status=bogus", headers=H("sk_live_a")).status_code == 400)
    check("bad limit -> 400", c.get("/v1/charges?limit=abc", headers=H("sk_live_a")).status_code == 400)
    check("bad cursor -> 400", c.get("/v1/charges?starting_after=nope", headers=H("sk_live_a")).status_code == 400)

    # 6. Payouts list.
    rp = c.get("/v1/payouts?limit=100", headers=H("sk_live_a"))
    check("payouts list returns A's 3 payouts", len(rp.json["data"]) == 3)
    check("payout list item has the payout shape", rp.json["data"][0]["id"].startswith("po_"))

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL LIST-ENDPOINT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
