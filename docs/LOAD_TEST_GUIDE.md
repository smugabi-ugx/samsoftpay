# Load-test gate — the singleton-ledger sharding decision

**Status: run this WHEN LIVE (on paid Postgres), before deciding on sharding.**

The money audit flagged the platform-wide singleton ledger rows (`rail_clearing`,
`psp_revenue`, keyed `merchant_id=NULL`) as a *possible* write hot-partition:
every successful charge posts to them, and concurrent completions serialize on
those two row locks. The audit rated it **plausible, not yet biting** at the
current worker concurrency of 2 — so sharding the money core now would be
complexity without evidence. This harness is the gate that turns it into a
decision.

## What it does
`scripts/loadtest_ledger.py` drives many **concurrent charge completions** through
the real path (`orchestrator.complete_transaction` → `ledger.post`) and reports
throughput + p50/p95/p99 latency as worker concurrency rises, then asserts the
ledger stayed **zero-sum and `cached_balance == journal-recompute`** under load.

## When / how to run it
Run it once you're on **paid Postgres** (the read is only meaningful there — SQLite
serializes every writer and can't show row-lock contention). Point it at a
**throwaway** database (it inserts test rows tagged `merchant_reference=load-<id>`):

```bash
DATABASE_URL="postgresql://user:pass@host/loadtest_db" \
  python scripts/loadtest_ledger.py --charges 1500 --sweep 1,2,4,8,16 --pool 24
```

Target ~1,000 completions/min sustained (raise `--charges`/`--pool` and worker
concurrency to match production plans).

## How to read the result
The harness prints a scaling line and a verdict:

- **Throughput scales ~linearly with workers** → the singleton rows are **not** the
  bottleneck. **Do not shard** — keep the money core simple.
- **Throughput goes flat / sub-linear and latency (p95/p99) climbs** with more
  workers → **hot-partition contention** on the singleton rows. **Sharding is
  justified** — shard `rail_clearing`/`psp_revenue` into per-bucket sub-accounts
  that reconcile, or move `cached_balance` maintenance off the hot path (the
  journal is already the source of truth).

If the invariant check ever reports a non-zero mismatch or journal sum, **stop** —
that's a concurrency correctness bug in the ledger, which outranks any scaling
question.

## Safety
Sandbox only: mock rail, `is_test=True`, no real MTN, no real money. It refuses a
Postgres URL that doesn't look like a throwaway/test DB. Drop the DB (or delete by
the `load-<id>` tag) afterward.
