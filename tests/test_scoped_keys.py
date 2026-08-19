"""Collections-only (scoped) API keys must never move money out.

The threat: a full secret key embedded on a public vending machine or kiosk can
be decompiled off the device and used to drain the merchant via /v1/payouts. A
collections key is the fix — it can take payments (charges, vending orders,
payment links) but is 403'd on every money-OUT endpoint.

What this proves:
  1. A collections key CAN create a charge (money in).
  2. A collections key is 403'd on /v1/payouts        (single payout).
  3. A collections key is 403'd on /v1/payouts/bulk   (bulk payout).
  4. A collections key is 403'd on a refund           (money back out).
  5. The 403 fires on SCOPE, before any resource lookup (a bogus charge id still 403s).
  6. The full secret key still works on payouts (the guard didn't break normal use).
  7. An unknown key is still 401, not 403.
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
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="scoped_keys_")
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
from app.models import AccountType, Merchant
from app.services import ledger

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


FULL = "sk_test_full_scopedkeytest"
COLL = "sk_test_col_scopedkeytest"
START = 100_000


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Kiosk Co", email="kiosk@x.com",
                     public_key="pk_test_kiosk",
                     secret_key="sk_live_kiosk_unused",
                     test_secret_key=FULL,
                     test_collections_key=COLL,
                     kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        # Fund the sandbox available balance so a payout with the FULL key can
        # actually succeed (proving the guard didn't break normal use).
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX", is_test=True)
        rail = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX", is_test=True)
        ledger.post([(rail, +START), (avail, -START)], currency="UGX",
                    memo="funding")
        db.session.commit()

    client = app.test_client()

    def hdr(key, idem):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))}

    def charge(key, idem, **body):
        return client.post("/v1/charges", headers=hdr(key, idem), json=body)

    def payout(key, idem, **body):
        return client.post("/v1/payouts", headers=hdr(key, idem), json=body)

    def bulk(key, idem, **body):
        return client.post("/v1/payouts/bulk", headers=hdr(key, idem), json=body)

    def refund(key, idem, cid, **body):
        return client.post(f"/v1/charges/{cid}/refund", headers=hdr(key, idem), json=body)

    # 1. Collections key CAN take money in.
    r = charge(COLL, "col-charge-1", amount=5000, currency="UGX", channel="mtn_momo",
               customer={"phone": "256700000000"})
    check("collections key creates a charge (money in)", r.status_code == 201)
    charge_id = (r.json or {}).get("id", "ch_does_not_exist")

    # 2. Collections key is 403'd on a single payout.
    r = payout(COLL, "col-payout-1", amount=1000, currency="UGX", channel="mtn_momo",
               recipient={"phone": "256780000001", "name": "x"})
    check("collections key 403 on /v1/payouts", r.status_code == 403)

    # 3. Collections key is 403'd on bulk payout.
    r = bulk(COLL, "col-bulk-1",
             items=[{"amount": 1000, "currency": "UGX", "channel": "mtn_momo",
                     "reference": "b1", "recipient": {"phone": "256780000001", "name": "x"}}])
    check("collections key 403 on /v1/payouts/bulk", r.status_code == 403)

    # 4/5. Collections key is 403'd on a refund — and on SCOPE, before any
    # charge lookup, so even a bogus charge id returns 403 (not 404).
    r = refund(COLL, "col-refund-1", "ch_totally_bogus_id", amount=1000)
    check("collections key 403 on refund", r.status_code == 403)
    check("refund 403 is scope-first (bogus id still 403, not 404)", r.status_code == 403)

    # 6. Full key still works on a payout.
    r = payout(FULL, "full-payout-1", amount=2000, currency="UGX", channel="mtn_momo",
               recipient={"phone": "256780000001", "name": "Supplier"})
    check("full secret key still creates a payout (201)", r.status_code == 201)

    # 7. Unknown key is 401, not 403.
    r = payout("sk_test_unknown_nope", "unknown-1", amount=1000, currency="UGX",
               channel="mtn_momo", recipient={"phone": "256780000001", "name": "x"})
    check("unknown key is 401 (auth), not 403 (scope)", r.status_code == 401)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SCOPED-KEY TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
