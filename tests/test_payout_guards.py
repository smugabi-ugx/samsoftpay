"""A rejected payout must not move a single shilling.

Regression test for a real leak: `_get_disbursement_adapter` raised for any
non-MTN channel, but only AFTER the ledger earmark had been staged. The API
route caught PayoutError and called idempotency.store(), which committed the
same session — so the merchant was debited amount+fee for a payout that never
existed, the amount stranded in SUSPENSE, and the payout stuck PENDING.

KarlPOS triggers exactly this: its detectChannel() sends channel='airtel_money'
for 70/75/74/20 numbers.

The journal still summed to zero throughout, so no integrity check caught it —
which is why these assertions are on BALANCES, not on the journal.
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
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="payout_guard_")
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
from app.models import Account, AccountType, Merchant, Payout
from app.services import ledger, payouts as payouts_service

START = 100_000


def balances(mid):
    out = {}
    for t in (AccountType.MERCHANT_AVAILABLE, AccountType.SUSPENSE):
        a = Account.query.filter_by(merchant_id=mid, type=t).first()
        out[t.value] = -a.cached_balance if a else 0
    return out


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="KarlPOS", email="k@x.com", public_key="pk_k",
                     secret_key="sk_k", kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX")
        rail = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX")
        ledger.post([(rail, +START), (avail, -START)], currency="UGX", memo="funding")
        db.session.commit()
        assert balances(mid)["merchant_available"] == START
    print(f"[0] Merchant funded with {START:,} UGX")

    client = app.test_client()

    def post_payout(idem, **body):
        return client.post("/v1/payouts", headers={
            "Authorization": "Bearer sk_k", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))}, json=body)

    # 1. Airtel: rejected, and NOTHING moves. This is the leak.
    r = post_payout("airtel-1", amount=20000, currency="UGX", channel="airtel_money",
                    recipient={"phone": "256700123456", "name": "KarlPOS payout"})
    assert r.status_code == 400, (r.status_code, r.json)
    with app.app_context():
        b = balances(mid)
        assert b["merchant_available"] == START, (
            f"LEAK: {START - b['merchant_available']} UGX left available on a rejected payout")
        assert b["suspense"] == 0, f"LEAK: {b['suspense']} UGX stranded in suspense"
        assert Payout.query.count() == 0, "a payout row was persisted for a rejected request"
    print(f"[1] Airtel rejected ({r.json['error'][:44]}…) — balance untouched, no payout row")

    # 2. The rail blowing up mid-call must not stranded money either.
    original = payouts_service._get_disbursement_adapter

    class Exploding:
        def initiate(self, payout):
            raise RuntimeError("connection reset by peer")

    payouts_service._get_disbursement_adapter = lambda channel: Exploding()
    try:
        r2 = post_payout("boom-1", amount=15000, currency="UGX", channel="mtn_momo",
                         recipient={"phone": "256780000001", "name": "x"})
        assert r2.status_code == 400, (r2.status_code, r2.json)
        with app.app_context():
            b = balances(mid)
            assert b["merchant_available"] == START, (
                f"LEAK on rail exception: available is {b['merchant_available']}")
            assert b["suspense"] == 0, f"LEAK on rail exception: suspense {b['suspense']}"
    finally:
        payouts_service._get_disbursement_adapter = original
    print("[2] Rail exception mid-call — earmark rolled back, balance untouched")

    # 3. Over-balance request still rejected cleanly (this path was always fine).
    r3 = post_payout("over-1", amount=500000, currency="UGX", channel="mtn_momo",
                     recipient={"phone": "256780000001", "name": "x"})
    assert r3.status_code == 400 and "insufficient" in r3.json["error"]
    with app.app_context():
        assert balances(mid)["merchant_available"] == START
    print("[3] Insufficient balance rejected — balance untouched")

    # 4. A GOOD payout still works and debits exactly amount+fee.
    r4 = post_payout("good-1", amount=10000, currency="UGX", channel="mtn_momo",
                     recipient={"phone": "256780000001", "name": "Supplier"})
    assert r4.status_code == 201, (r4.status_code, r4.json)
    fee = r4.json["fee"]
    with app.app_context():
        b = balances(mid)
        assert b["merchant_available"] == START - 10000 - fee, b
        assert b["suspense"] == 10000, b
    print(f"[4] Valid MTN payout accepted: debited 10,000 + {fee} fee, 10,000 in flight")

    # 5. Ledger integrity intact.
    with app.app_context():
        from app.models import JournalEntry
        total = sum(e.amount for e in JournalEntry.query.all())
        assert total == 0, f"journal does not balance: {total}"
    print("[5] Journal sums to zero")

    print("\nALL PAYOUT GUARD TESTS PASSED")


if __name__ == "__main__":
    main()
