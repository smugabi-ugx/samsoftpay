"""Load-test harness — does the singleton ledger row become a write bottleneck?

Every successful charge posts three ledger legs, two of which hit PLATFORM-WIDE
singleton rows (rail_clearing, psp_revenue, keyed merchant_id=None). Concurrent
completions serialize on those two row locks. This harness drives many CONCURRENT
completions through the real money path (orchestrator.complete_transaction ->
ledger.post) and measures throughput + latency as concurrency rises:

  * Near-LINEAR throughput as workers increase  -> the singletons are NOT the
    bottleneck; leave them (don't add sharding complexity to the money core).
  * FLAT/declining throughput + rising latency   -> hot-partition contention;
    the sharding change the audit flagged is justified.

It ALSO asserts the ledger stays zero-sum and cached_balance == journal-recompute
under the concurrency — a load test that breaks the invariant is the real finding.

IMPORTANT — the read is only meaningful on POSTGRES. SQLite serializes every
writer (its lock is the whole DB), so it can't show row-lock contention; against
SQLite this is a correctness smoke + a harness self-test, not a scaling verdict.

SANDBOX ONLY: mock rail, is_test=True, no real MTN, no real money.

Usage
-----
  # Meaningful contention read (throwaway Postgres DB, e.g. a paid Render PG or
  # a local one). It INSERTS test rows — point it at a DB you can drop:
  DATABASE_URL="postgresql://user:pass@host/loadtest_db" \
      python scripts/loadtest_ledger.py --charges 1500 --sweep 1,2,4,8,16 --pool 24

  # Quick local baseline (SQLite serializes — proves the harness + invariant):
  python scripts/loadtest_ledger.py --charges 400 --sweep 1,4
"""
import argparse
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _percentile(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--charges", type=int, default=800, help="completions per concurrency level")
    ap.add_argument("--sweep", type=str, default="1,2,4,8", help="comma list of worker counts")
    ap.add_argument("--pool", type=int, default=0, help="DB_POOL_SIZE (0 = maxsweep+4)")
    ap.add_argument("--db", type=str, default="", help="DATABASE_URL override (else env, else temp sqlite)")
    ap.add_argument("--test", type=str, default="all", choices=["ledger", "races", "all"],
                    help="ledger=contention sweep; races=exactly-once under concurrency; all=both")
    args = ap.parse_args()
    do_ledger = args.test in ("ledger", "all")
    do_races = args.test in ("races", "all")

    sweep = [int(x) for x in args.sweep.split(",") if x.strip()]
    pool = args.pool or (max(sweep) + 4)

    # --- DB selection + safety ---
    db_url = args.db or os.environ.get("DATABASE_URL", "")
    is_pg = db_url.startswith("postgresql")
    if not db_url:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db", prefix="loadtest_")
        os.close(fd)
        db_url = "sqlite:///" + path.replace("\\", "/")
        print(f"[db] no DATABASE_URL — using a throwaway SQLite file: {path}")
        print("[db] NOTE: SQLite serializes writers; this run is a smoke/self-test, not a scaling verdict.")
    elif is_pg:
        low = db_url.lower()
        if not any(t in low for t in ("loadtest", "test", "localhost", "127.0.0.1", "staging")):
            print("[safety] Refusing to run: the Postgres URL doesn't look like a throwaway/test DB")
            print("[safety] (it must contain 'loadtest'/'test'/'staging' or point at localhost).")
            print("[safety] This harness INSERTS load-test rows — never run it against production.")
            sys.exit(2)
        print(f"[db] Postgres target (writes test rows): {low.split('@')[-1]}")

    os.environ["DATABASE_URL"] = db_url
    os.environ["MOMO_USE_REAL"] = "0"
    os.environ["DB_POOL_SIZE"] = str(pool)
    os.environ["DB_MAX_OVERFLOW"] = str(pool)

    from sqlalchemy import func
    from app import create_app
    from app.extensions import db
    from app.models import (AccountType, Channel, JournalEntry, Merchant,
                            PaymentLink, Transaction, TxnStatus)
    from app.services import ledger, orchestrator

    # Don't let the success-path webhook enqueue block on a missing broker.
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None

    app = create_app()
    run_id = uuid.uuid4().hex[:8]

    with app.app_context():
        db.create_all()
        m = Merchant(name=f"Load {run_id}", email=f"load_{run_id}@x.com",
                     public_key=f"pk_live_{run_id}", test_secret_key=f"sk_test_{run_id}",
                     secret_key=f"sk_live_{run_id}", kyc_status="verified")
        db.session.add(m); db.session.commit()
        mid = m.id
        # warm the singleton + merchant accounts so the first post isn't a create race
        for at in (AccountType.RAIL_CLEARING, AccountType.PSP_REVENUE):
            ledger.get_or_create_account(type=at, merchant_id=None, currency="UGX", is_test=True)
        for at in (AccountType.MERCHANT_PENDING, AccountType.MERCHANT_AVAILABLE):
            ledger.get_or_create_account(type=at, merchant_id=mid, currency="UGX", is_test=True)
        db.session.commit()

    def seed(n):
        """Insert n AUTHORIZED txns ready to complete; return their ids."""
        ids = []
        with app.app_context():
            for _ in range(n):
                t = Transaction(
                    public_id=f"txn_{uuid.uuid4().hex[:16]}", merchant_id=mid,
                    amount=10000, fee_amount=200, currency="UGX",
                    channel=Channel.MTN_MOMO, status=TxnStatus.AUTHORIZED,
                    is_test=True, rail_reference=f"ld_{run_id}_{uuid.uuid4().hex[:12]}",
                    merchant_reference=f"load-{run_id}")
                db.session.add(t); ids.append(t)
            db.session.commit()
            return [t.id for t in ids]

    def complete_one(txn_id):
        t0 = time.perf_counter()
        with app.app_context():
            try:
                t = db.session.get(Transaction, txn_id)
                orchestrator.complete_transaction(
                    txn_id, success=True, rail_reference=t.rail_reference)
            finally:
                db.session.remove()
        return time.perf_counter() - t0

    results = []
    if do_ledger:
        print("\n=== LEDGER CONTENTION SWEEP (singleton rail_clearing / psp_revenue) ===")
        print(f"{'workers':>8} {'completions':>12} {'wall_s':>8} {'thru/s':>9} "
              f"{'p50_ms':>8} {'p95_ms':>8} {'p99_ms':>8} {'errors':>7}")
        print("-" * 74)
        for c in sweep:
            ids = seed(args.charges)
            lat, errs = [], 0
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=c) as ex:
                for r in ex.map(lambda i: _safe(complete_one, i), ids):
                    if r is None:
                        errs += 1
                    else:
                        lat.append(r)
            wall = time.perf_counter() - t0
            thru = len(lat) / wall if wall else 0
            results.append((c, thru))
            print(f"{c:>8} {len(ids):>12} {wall:>8.2f} {thru:>9.1f} "
                  f"{_percentile(lat,50)*1000:>8.1f} {_percentile(lat,95)*1000:>8.1f} "
                  f"{_percentile(lat,99)*1000:>8.1f} {errs:>7}")

    # --- Correctness under load: the ledger invariant must still hold ---
    with app.app_context():
        mism = ledger.assert_balances_match()
        by_cur = db.session.query(func.sum(JournalEntry.amount)).scalar()
    print("\n[invariant] cached_balance vs journal-recompute mismatches:", len(mism), "(want 0)")
    print("[invariant] global journal sum:", int(by_cur or 0), "(want 0)")

    if do_ledger and results:
        base, top = results[0][1], results[-1][1]
        scale = (top / base) if base else 0
        ideal = (sweep[-1] / sweep[0]) if sweep else 1
        print(f"\n[scaling] throughput {base:.0f}/s @ {sweep[0]} -> {top:.0f}/s @ {sweep[-1]} "
              f"workers ({scale:.1f}x; ideal ~{ideal:.0f}x)")
        if not is_pg:
            print("[verdict] SQLite serializes writers — run against Postgres for a real read.")
        elif scale >= 0.6 * ideal:
            print("[verdict] Scales with concurrency -> singleton rows NOT the bottleneck; don't shard.")
        else:
            print("[verdict] FLAT/sub-linear -> hot-partition contention; sharding justified.")

    # --- Exactly-once under true concurrency: the guards that prevent double money ---
    if do_races:
        print("\n=== CONCURRENCY RACES — exactly-once under parallel workers ===")
        W = max(max(sweep), 8)
        with app.app_context():
            key = db.session.get(Merchant, mid).test_secret_key
        race_pass = True

        # 1. Idempotency: W identical POST /v1/charges (SAME Idempotency-Key) -> 1 charge
        idem, ref1 = "raceidem-" + run_id, "raceidemref-" + run_id
        hdr = {"Authorization": f"Bearer {key}", "X-Timestamp": str(int(time.time())),
               "Idempotency-Key": idem}
        body = {"amount": 5000, "currency": "UGX", "channel": "mtn_momo",
                "customer": {"phone": "256700000000"}, "reference": ref1}
        with ThreadPoolExecutor(max_workers=W) as ex:
            list(ex.map(lambda i: _safe(lambda _: app.test_client().post(
                "/v1/charges", json=body, headers=hdr).status_code, i), range(W)))
        with app.app_context():
            n1 = Transaction.query.filter_by(merchant_reference=ref1).count()
        ok = n1 <= 1; race_pass &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] idempotency: {W} identical charges, same key -> "
              f"{n1} charge(s) (want <=1)")

        # 2. Double-submit: W POST /pay/<id>/submit on ONE single-use link -> 1 charge
        with app.app_context():
            db.session.add(PaymentLink(public_id=f"lnk_race_{run_id}", merchant_id=mid,
                                       amount=5000, currency="UGX", is_test=True,
                                       allow_multiple_uses=False))
            db.session.commit()
        with ThreadPoolExecutor(max_workers=W) as ex:
            list(ex.map(lambda i: _safe(lambda _: app.test_client().post(
                f"/pay/lnk_race_{run_id}/submit",
                data={"channel": "mtn_momo", "phone": "256700000000"}).status_code, i), range(W)))
        with app.app_context():
            n2 = Transaction.query.filter_by(merchant_reference=f"lnk_race_{run_id}").count()
        ok2 = n2 <= 1; race_pass &= ok2
        print(f"  [{'PASS' if ok2 else 'FAIL'}] double-submit: {W} submits on 1 single-use link -> "
              f"{n2} charge(s) (want <=1)")

        # 3. Vending _claim: W concurrent atomic pending->dispensing on one order -> 1 winner
        with app.app_context():
            vl = PaymentLink(public_id=f"lnk_vend_{run_id}", merchant_id=mid, amount=5000,
                             currency="UGX", is_test=True, vending_status="pending")
            db.session.add(vl); db.session.commit(); vlid = vl.id

        def _claim(_):
            with app.app_context():
                won = db.session.query(PaymentLink).filter(
                    PaymentLink.id == vlid, PaymentLink.vending_status == "pending"
                ).update({"vending_status": "dispensing"}, synchronize_session=False)
                db.session.commit(); db.session.remove()
                return won
        with ThreadPoolExecutor(max_workers=W) as ex:
            wins = sum(x for x in ex.map(lambda i: _safe(_claim, i), range(W)) if x)
        ok3 = wins == 1; race_pass &= ok3
        print(f"  [{'PASS' if ok3 else 'FAIL'}] vending _claim: {W} concurrent claims -> "
              f"{wins} winner(s) (want exactly 1)")

        print("\n[races] " + ("ALL PASS — exactly-once holds under this concurrency"
              if race_pass else "A RACE FAILED — double charge/dispense possible; INVESTIGATE")
              + ("" if is_pg else "  (SQLite serialized — re-run on Postgres for a true parallel read)"))

    print("\nDone. (Test rows tagged merchant_reference=load-%s / email load_%s@x.com — "
          "drop the DB or delete by that tag.)" % (run_id, run_id))


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:  # count, don't crash the sweep
        sys.stderr.write(f"  worker error: {e}\n")
        return None


if __name__ == "__main__":
    main()
