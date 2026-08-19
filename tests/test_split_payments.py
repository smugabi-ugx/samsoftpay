"""Split payments v1 — one charge fans out to subaccounts, EXACTLY.

Built from the agent-designed + adversarially money-audited spec. What must hold:

  1. Subaccounts are created via the API and listed; a managed subaccount's
     (never-displayed) key can NEVER authenticate.
  2. A split charge resolves shares against N = amount - fee; after success each
     subaccount's PENDING holds exactly its share and the platform holds the
     residual — Σshares + residual == N by construction (bps floors included).
  3. The whole journal still sums to zero.
  4. An over-N split is rejected 400 with ZERO writes (no transaction row).
  5. Another platform's subaccount in my split -> 400 zero writes.
  6. txn.settled_at is set at success, so the ORIGINAL sweep skips the split
     charge; the allocation pass settles each share pending->available after
     the hold — and moves exactly the allocation totals (no double-credit).
  7. Refunding a split charge is REJECTED with zero writes (v1 scope; the audit
     found critical hazards in split refunds — deferred, not shipped broken).
  8. A subaccount deactivated between create and success is NOT credited — its
     share folds into the platform residual.
"""
import atexit
import json
import os
import sys
import tempfile
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
# Timer-thread completion needs a FILE db, not :memory: (per CLAUDE.md).
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="splits_")
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
from app.models import (
    Account, AccountType, Channel, JournalEntry, Merchant, SplitAllocation,
    Subaccount, Transaction, TxnStatus, utcnow,
)

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def journal_zero():
    return sum(e.amount for e in JournalEntry.query.all()) == 0


def balance(merchant_id, acct_type, is_test=True):
    a = Account.query.filter_by(merchant_id=merchant_id, type=acct_type,
                                is_test=is_test).first()
    return -a.cached_balance if a else 0


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        platform = Merchant(name="Wallet Platform", email="plat@x.com",
                            public_key="pk_plat", secret_key="sk_live_plat",
                            test_secret_key="sk_test_plat", kyc_status="verified",
                            handle="plat")
        rival = Merchant(name="Rival", email="rival@x.com", public_key="pk_riv",
                         secret_key="sk_live_riv", test_secret_key="sk_test_riv",
                         kyc_status="verified", handle="riv")
        db.session.add_all([platform, rival])
        db.session.commit()
        platform_id, rival_id = platform.id, rival.id

    c = app.test_client()

    def H(key="sk_test_plat", idem=None):
        h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
             "X-Timestamp": str(int(time.time()))}
        h["Idempotency-Key"] = idem or f"k{time.time_ns()}"
        return h

    # ── 1. subaccounts via the API ──
    ra = c.post("/v1/subaccounts", headers=H(),
                data=json.dumps({"name": "Shop A", "payout_phone": "256780000001"}))
    rb = c.post("/v1/subaccounts", headers=H(),
                data=json.dumps({"name": "Shop B", "external_ref": "wallet-usr-9"}))
    check("subaccount A created 201", ra.status_code == 201)
    check("subaccount B created 201", rb.status_code == 201)
    sub_a, sub_b = ra.json["id"], rb.json["id"]
    rl = c.get("/v1/subaccounts", headers={"Authorization": "Bearer sk_test_plat"})
    check("subaccounts listed", {d["id"] for d in rl.json["data"]} == {sub_a, sub_b})

    with app.app_context():
        managed_a = Subaccount.query.filter_by(public_id=sub_a).first()
        managed_key = db.session.get(Merchant, managed_a.merchant_id).secret_key
        sub_a_mid = managed_a.merchant_id
        sub_b_mid = Subaccount.query.filter_by(public_id=sub_b).first().merchant_id
    r = c.get("/v1/subaccounts", headers={"Authorization": f"Bearer {managed_key}"})
    check("managed subaccount's key can NEVER authenticate (401)", r.status_code == 401)

    # ── 4/5. bad splits rejected with zero writes ──
    with app.app_context():
        txns_before = Transaction.query.count()
    r = c.post("/v1/charges", headers=H(), data=json.dumps({
        "amount": 10000, "channel": "mtn_momo",
        "customer": {"phone": "256700000000"},
        "split": [{"subaccount": sub_a, "amount": 99999}]}))
    check("over-N split -> 400", r.status_code == 400)
    # rival's key must not be able to use MY platform's subaccounts
    r = c.post("/v1/charges", headers=H("sk_test_riv"), data=json.dumps({
        "amount": 10000, "channel": "mtn_momo",
        "customer": {"phone": "256700000000"},
        "split": [{"subaccount": sub_a, "amount": 1000}]}))
    check("foreign subaccount in split -> 400", r.status_code == 400)
    with app.app_context():
        check("rejected splits wrote ZERO transactions",
              Transaction.query.count() == txns_before)

    # ── 2. the real split charge (fixed + bps) via the mock rail ──
    # amount 10000, fee 200 -> N=9800. A fixed 3000; B bps 2500 -> floor(2450).
    # residual = 9800 - 3000 - 2450 = 4350.
    r = c.post("/v1/charges", headers=H(), data=json.dumps({
        "amount": 10000, "channel": "mtn_momo",
        "customer": {"phone": "256700000000"},
        "split": [{"subaccount": sub_a, "amount": 3000},
                  {"subaccount": sub_b, "bps": 2500}]}))
    check("split charge created 201", r.status_code == 201)
    charge_id = r.json["id"]

    final = None
    for _ in range(40):
        j = c.get(f"/v1/charges/{charge_id}",
                  headers={"Authorization": "Bearer sk_test_plat"}).json
        final = j.get("status")
        if final in ("succeeded", "failed"):
            break
        time.sleep(0.3)
    check("split charge succeeded on the mock rail", final == "succeeded")

    with app.app_context():
        txn = Transaction.query.filter_by(public_id=charge_id).first()
        allocs = SplitAllocation.query.filter_by(transaction_id=txn.id).all()
        by_mid = {a.merchant_id: a.amount for a in allocs}
        check("three allocations (A, B, platform residual)", len(allocs) == 3)
        check("A's share exact (3000)", by_mid.get(sub_a_mid) == 3000)
        check("B's bps share exact (floor 2450)", by_mid.get(sub_b_mid) == 2450)
        check("platform residual exact (4350)", by_mid.get(platform_id) == 4350)
        check("shares + residual == N exactly", sum(by_mid.values()) == 9800)
        check("A's PENDING credited its share", balance(sub_a_mid, AccountType.MERCHANT_PENDING) == 3000)
        check("B's PENDING credited its share", balance(sub_b_mid, AccountType.MERCHANT_PENDING) == 2450)
        check("platform PENDING holds ONLY the residual",
              balance(platform_id, AccountType.MERCHANT_PENDING) == 4350)
        check("txn.settled_at set (original sweep will skip it)", txn.settled_at is not None)
        check("journal sums to zero after split", journal_zero())
        # GET exposes the split for reconciliation
        j = c.get(f"/v1/charges/{charge_id}",
                  headers={"Authorization": "Bearer sk_test_plat"}).json
        check("GET charge exposes split allocations", len(j.get("split", [])) == 3)

    # ── 6. settlement: allocation pass settles each share after the hold ──
    with app.app_context():
        txn = Transaction.query.filter_by(public_id=charge_id).first()
        txn.completed_at = utcnow() - timedelta(hours=48)
        db.session.commit()
        from app.services.settlement import sweep_to_available
        moved = sweep_to_available(hold_hours=24)
        check("sweep moved exactly the allocation totals",
              moved.get(sub_a_mid) == 3000 and moved.get(sub_b_mid) == 2450
              and moved.get(platform_id) == 4350)
        check("A available == share, pending == 0",
              balance(sub_a_mid, AccountType.MERCHANT_AVAILABLE) == 3000
              and balance(sub_a_mid, AccountType.MERCHANT_PENDING) == 0)
        check("platform available == residual ONLY (no double-credit)",
              balance(platform_id, AccountType.MERCHANT_AVAILABLE) == 4350)
        # a second sweep must move nothing (per-share settled_at)
        moved2 = sweep_to_available(hold_hours=24)
        check("second sweep moves nothing", not moved2)
        check("journal still zero after settlement", journal_zero())

    # ── 7. split refund rejected with zero writes ──
    with app.app_context():
        je_before = JournalEntry.query.count()
    r = c.post(f"/v1/charges/{charge_id}/refund", headers=H())
    with app.app_context():
        txn = Transaction.query.filter_by(public_id=charge_id).first()
        check("split refund rejected (not 2xx-refunded)",
              txn.status == TxnStatus.SUCCEEDED)
        check("split refund wrote ZERO journal entries",
              JournalEntry.query.count() == je_before)

    # ── 8. deactivated sub's share folds into the platform residual ──
    with app.app_context():
        from app.services.orchestrator import complete_transaction
        # deactivate B, then complete a fresh split charge manually (deterministic)
        db.session.get(Merchant, sub_b_mid).is_active = False
        db.session.commit()
        txn2 = Transaction(
            public_id="txn_deact01", merchant_id=platform_id, amount=10000,
            fee_amount=200, currency="UGX", channel=Channel.MTN_MOMO,
            status=TxnStatus.AUTHORIZED, is_test=True, rail_reference="ref_deact",
            split_meta=json.dumps([{"subaccount": sub_a, "amount": 3000},
                                   {"subaccount": sub_b, "amount": 2000}]))
        db.session.add(txn2)
        db.session.commit()
        a_before = balance(sub_a_mid, AccountType.MERCHANT_PENDING)
        p_before = balance(platform_id, AccountType.MERCHANT_PENDING)
        complete_transaction(txn2.id, success=True, rail_reference="ref_deact")
        check("active sub A still credited (3000)",
              balance(sub_a_mid, AccountType.MERCHANT_PENDING) == a_before + 3000)
        check("deactivated B credited NOTHING",
              balance(sub_b_mid, AccountType.MERCHANT_PENDING) == 0)
        check("B's share folded into platform residual (4800+2000=6800)",
              balance(platform_id, AccountType.MERCHANT_PENDING) == p_before + 6800)
        check("journal zero at the end", journal_zero())

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SPLIT-PAYMENT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
