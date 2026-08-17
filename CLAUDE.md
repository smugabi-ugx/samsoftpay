# Samsoftpay — Project Context & Guardrails
> Authoritative state for this repo. READ THIS FIRST every session.
> Last updated: August 2026 (after PRs #10–#12 merged to main; GET /v1/balance in PR from feat/balance-endpoint).
> Companion docs: COMMERCIAL_READINESS.md (audit + roadmap), C:\Users\DELL\Desktop\tk\MASTER_CLAUDE.md (TK Vending).

---

## ⛔ GUARDRAILS — DO NOT BREAK THESE (each one already caused a real outage/bug)

1. **MTN rails need `import threading`.** Both `app/services/rails_mtn_real.py` and
   `app/services/rails_mtn_disbursement.py` use `threading.Lock()` for the OAuth token cache.
   The import was once removed during a refactor and CRASHED both collections AND disbursements
   at import time. NEVER remove `import threading` from these files.

2. **Celery worker/beat MUST target `app.celery_worker`, NOT `app.celery_app`.**
   `app/celery_worker.py` runs `create_app()` (which wires broker=Redis, result backend,
   beat_schedule, FlaskTask) and imports the task modules so tasks register. Pointing Celery at
   `app.celery_app` directly = unconfigured object → defaults to RabbitMQ broker + ZERO tasks.
   render.yaml start commands and CLAUDE must both keep `app.celery_worker`.

3. **API keys are hashed at rest. Do not break dual-path auth.**
   `app/routes/api.py:_auth()` looks up by `secret_key_hash` first, then falls back to plaintext.
   `app/models/__init__.py:_sync_key_hashes` (a before_insert/before_update event) auto-populates
   the hash columns from the plaintext keys. Do NOT remove the event listener or the fallback
   until `flask backfill-key-hashes` has run in production AND all keys are confirmed hashed.

4. **Settlement settles each transaction ONCE, after ITS OWN hold** via `Transaction.settled_at`
   (`app/services/settlement.py`). Do NOT revert to the old "sum all aged txns and cap at pending
   balance" approach — it released money still inside its hold window. Sweep commits PER MERCHANT.

5. **Payouts take a row lock before the balance check** (`ledger.lock_account_for_update` in
   `app/services/payouts.py`). This prevents double-spend/overdraft under concurrency. Do NOT remove it.

6. **Ledger sign convention:** `merchant_pending` / `merchant_available` are stored as NEGATIVE
   numbers (credits). `ledger.post()` entries MUST sum to zero. Don't "fix" the signs.

7. **`get_or_create_account` uses a SAVEPOINT (`begin_nested`)** for race safety
   (`app/services/ledger.py`). Don't simplify it back to plain query-then-insert.

8. **Production boot-guard:** `_assert_production_env()` in `app/__init__.py` refuses to boot on
   Render without a strong `SECRET_KEY` and `WEBHOOK_SIGNING_SECRET`. Web, worker AND beat all
   carry these (see render.yaml). Don't weaken the guard.

9. **Inbound webhook signature fails CLOSED in production** (`app/routes/webhooks_inbound.py`).
   A rail callback marks money succeeded, so it must verify the HMAC. Don't re-add a "skip if no
   secret" path for production.

10. **NEVER commit secrets.** `.env`, `render_secrets.txt`, `New folder/gh.txt`, `.venv`, `*.db`
    are gitignored. Confirm `git status` before any `git add -A`.

11. **A vending machine dispenses ONLY against a SUCCEEDED charge, ONCE.**
    `app/services/vending.py` is the single gate. Two guards must stay:
    (a) every path re-checks `txn.status == SUCCEEDED` — including retries;
    (b) `_claim()` is an atomic `UPDATE … WHERE vending_status='pending'`, so concurrent rail
    callbacks/polls can never both dispense. Do NOT replace it with a read-then-write.
    The supplier call is also wrapped so a machine/supplier outage can never roll back or hide
    a payment we already took — the order goes `failed` and is retried, the money stays recorded.

12. **Test and live ledgers are SPLIT.** Accounts are keyed by (type, merchant, currency,
    **is_test**) — `is_test` selects the LEDGER, not a label (PR #12, migration
    `f9a0b1c2d3e4`). Every money site must pass the mode of the thing it posts for (charge →
    `txn.is_test`, payout completion/reversal → `payout.is_test`, etc.), and the settlement
    sweep groups by mode as well as merchant+currency. Before the split, sandbox charges and
    payouts moved REAL withdrawable money (verified in production). Balance reads (dashboard,
    wallet, withdrawals) are scoped to the live ledger. Don't collapse the modes back together.

13. **Resolve the payout rail BEFORE any money moves** (`app/services/payouts.py`, PR #11).
    An unsupported channel (e.g. Airtel — no disbursement adapter yet) must be rejected with
    ZERO database writes. The post-earmark section is guarded: any rail exception rolls the
    session back so create_payout commits a coherent state or nothing. The old order (earmark
    first, resolve later) stranded amount+fee in SUSPENSE on every rejected attempt because the
    idempotency layer committed the session. Repair tools: `flask stranded-payouts`,
    `flask reverse-payout <id>` (refuses if the payout ever reached a rail).

---

## Who is Sam
- Rogers Mugabi, trading as **Sam Software**. Email samsoftware75@gmail.com / smugabi@gmail.com. MTN +256783647260.
- GitHub: https://github.com/smugabi-ugx/samsoftpay.git (default branch: `main`)
- Live API: **https://api.samsoftpay.com** (custom domain, Starter plan, SSL issued)
- Render fallback URL: https://samsoftpay.onrender.com

## What Samsoftpay Is
Flask + SQLAlchemy payment gateway for Uganda. MTN MoMo Collections + Disbursements, double-entry
ledger (rail_clearing → merchant_pending → merchant_available), merchant API (idempotency, replay
guard, rate limiting), Celery+Redis async polling/webhooks, hourly settlement sweep (24h hold),
refunds, payment links, hosted checkout, webhook delivery with backoff.

## Sam's product stack
KarlPOS (karlpos.com, live merchants) → currently on Pesapal → migrating onto Samsoftpay.
Samsoftpay = the rails. TK Vending = first external client (Samsoftpay is its hidden backend;
client must NOT know it's Sam's platform). Pesapal built OpenFloat to compete with KarlPOS.

---

## LOCAL DEV (this is how to run/verify — Python 3.14 is BROKEN with SQLAlchemy)
- Virtualenv: **`.venv` built with Python 3.10** (`py -3.10 -m venv .venv`). Use it for everything.
- Run app:   `.\.venv\Scripts\python.exe -c "from app import create_app; ..."`
- Tests are SCRIPT-STYLE (run directly, not pytest). Run with mock rails so no Redis is needed:
  - `$env:MOMO_USE_REAL="0"; .\.venv\Scripts\python.exe tests\test_end_to_end.py`
  - `$env:MOMO_USE_REAL="0"; .\.venv\Scripts\python.exe tests\test_settlement_sweep.py`
  - Both MUST pass (ledger sums to zero; settlement respects per-txn hold).
  - `tests\test_vending_flow.py` and `tests\test_checkout.py` pin their own env — just run them.
- `.env` sets `MOMO_USE_REAL=1` (real MTN sandbox). Override to `0` locally for mock/offline tests.
  In a test file, SET it to `"0"` — do not `pop()` it. dotenv refills a popped var from `.env`
  and the test then fires at MTN's sandbox and hangs in `authorized`.
- Tests that wait on the mock rail MUST use a temp FILE sqlite DB, not `:memory:`. The rail
  completes the charge on a timer thread, and an in-memory DB is per-connection — the thread
  writes into a different, empty database and the completion silently never lands.
- Migrations: `flask db upgrade` (FLASK_APP=run.py). Current head = **f9a0b1c2d3e4**
  (chain: b2f1a9c4d5e6 → … → d7e8f9a0b1c2 vending → e8f9a0b1c2d3 per-merchant XY →
  f9a0b1c2d3e4 test/live ledger split).

## Tech stack & Render services
- Python 3.10/3.x, Flask 3.0.3, SQLAlchemy 2.x + PostgreSQL, Celery 5.3.6 + Redis, Gunicorn.
- Web: `gunicorn run:app` (Starter, always-on). Health check path: **/healthz**.
- Worker: `celery -A app.celery_worker:celery worker --concurrency=2`
- Beat:   `celery -A app.celery_worker:celery beat`
- Redis (free, allkeys-lru), PostgreSQL (watch: free Postgres expires ~90 days — move to paid).
- Outbound IPs for MTN Web Access Form: 74.220.48.0/24 and 74.220.56.0/24.

## Required Render env vars (web; worker/beat share the secrets)
SECRET_KEY, WEBHOOK_SIGNING_SECRET (generateValue), SETUP_TOKEN, ADMIN_EMAIL, DATABASE_URL,
REDIS_URL, BASE_URL=https://api.samsoftpay.com, RENDER=true. Production MTN: MOMO_USE_REAL=1,
MOMO_BASE_URL (prod), MOMO_CURRENCY=UGX, MOMO_* keys (from MTN onboarding). Optional: SENTRY_DSN.
Vending (XY connector, only needed for vending merchants): XY_BASE_URL, XY_KEY, XY_SECRET,
XY_MERCHANT_NO — issued by the machine supplier. Without them payments still work; the machine
just cannot be told to dispense (orders land in `failed` and are retryable).

---

## WHAT'S DONE (verified locally, merged in PR #2 → main)
Security: API key hashing + dual-path auth; inbound webhook fail-closed; boot-guard on default
secrets; /first-setup token-gated. Money: payout row-lock (no double-spend); race-safe account
creation; per-txn settlement (settled_at) + per-merchant commit. Reliability: threading-import fix
in both MTN rails; resilient poller enqueue; Celery worker/beat bootstrap fix. Ops: /healthz +
/livez; Redis rate limiting in prod; request-id logging; optional Sentry; nightly reconciliation
(`flask reconcile`). API: consistent `mode` + `created_at` on charge/payout responses. UX:
production-grade checkout status page (approve-on-phone, animated states, retry). Migration
b2f1a9c4d5e6 (refund cols + settled_at + key-hash cols + indexes) — applies cleanly.

MTN SIT: re-run against live sandbox = 8/9 (only fail = disbursement balance, a sandbox limitation;
returns 200 in production). Sheet: C:\Users\DELL\Desktop\tk\MTN_MoMo_SIT_Report.xlsx (annotated).

### Vending (TK Vending / XY machines) — MERGED to main (PR #10, Aug 2026)
The whole scan-to-pay-to-dispense loop, driven by Samsoftpay:
- `app/services/xy_vending.py` — supplier cloud client (MD5 sign, queries, `ApplyExportGoods`).
- `app/services/vending.py` — orders, the dispense gate (see guardrail 11), QR rendering (segno).
- API: `POST /v1/vending/orders`, `GET /v1/vending/orders/<id>`, `POST …/<id>/dispense` (retry),
  `GET /v1/vending/machines`, `GET /v1/vending/machines/<m>/goods`, plus the older
  `POST /v1/vending/dispense` for merchants running their own payment flow.
- Public: `/pay/<id>/qr.png|.svg`, `/vending/display/<id>` (the machine's own screen — polls
  `state.json` and flips itself to "collect your item"), all QR-enabled for ANY payment link.
- Merchant UI: Dashboard → Vending (on/off kill switch, create order, order log, retry).
- Migrations `d7e8f9a0b1c2` + `e8f9a0b1c2d3`; new dep **segno** (pure-Python QR, no Pillow).
- Per-merchant XY supplier credentials + machine registry (`app/services/secrets_box.py`,
  vending settings UI) — XY_* env vars are now the fallback, not the only source.
- Tests: `tests/test_vending_flow.py` (incl. no-dispense-on-failed-payment, double-dispense
  guard, supplier-outage recovery, kill switch) + `tests/test_xy_vending_sign.py`. All passing.
NOTE: the customer QR points at our hosted checkout, so the machine never touches money. The
supplier's `forwardPayCode` model expects the payment provider to own the QR — that provider is us.

### Merged after vending (Aug 2026)
- **PR #11 — payout earmark money leak fix** (see guardrail 13). KarlPOS's detectChannel()
  sends `airtel_money` for 70/75/74/20 numbers; each rejected payout used to strand
  amount+fee in SUSPENSE. New CLI: `flask stranded-payouts`, `flask reverse-payout <id>`.
  Tests: `tests/test_payout_guards.py`.
- **PR #12 — test/live ledger split** (see guardrail 12). Sandbox money is no longer real
  money. Existing accounts migrated to `is_test=False` (preserves balances exactly).
  Tests: `tests/test_ledger_mode_split.py`. Also repaired the long-broken
  `tests/test_collections_and_disbursements.py` (dotenv pop + :memory: pitfalls, wrong fee).
- **feat/balance-endpoint (PR open) — `GET /v1/balance`**: per-currency reconciliation
  endpoint for platforms (KarlPOS, TK Vending). Reports the JOURNAL sum, not
  `cached_balance` (returns `consistent` flag when they disagree); mode-scoped (sk_test_
  sees sandbox only); credit-normal sign flipped so merchants see positive numbers.
  Documented on the docs page. Tests: `tests/test_balance_endpoint.py` (7 checks).

## POST-DEPLOY checklist (after a main deploy)
1. `flask db current` → expect `f9a0b1c2d3e4`
2. `flask backfill-key-hashes` (once)
3. open https://api.samsoftpay.com/healthz → `{"status":"ok","database":"up"}`
4. If worker/beat are manually configured (not blueprint-synced), set their start commands to
   `app.celery_worker` in the Render dashboard.

---

## OPEN / NEXT (not yet done)
- **Security headers** (live probe found NONE): add HSTS, X-Frame-Options, X-Content-Type-Options,
  Referrer-Policy, CSP. Cloudflare fronts the app but the app should set these.
- **Apex domain**: samsoftpay.com + www have NO DNS records ("no server found" is expected).
  To serve a landing page there: add both as custom domains in Render, add an A record (apex) +
  CNAME (www) in Namecheap. api.samsoftpay.com is the only one configured and is all the API needs.
- **Load test** at ~1000 charges/min — run against STAGING or a controlled window, NOT blindly
  against production (cost + tripping Cloudflare/Render).
- **Remove plaintext key fallback** in _auth() once backfill confirmed in production.
- Minor: in api.py the unauthenticated charge returns 400 (timestamp) before 401 (auth) — could
  reorder so missing auth → 401 first. Cosmetic.
- MTN PRODUCTION onboarding: submit SIT + Web Access Form + Email form to Felix Oluka
  (Felix.Oluka@mtn.com). Sam Software Co Ltd compliance papers PAID (June 2026) — URSB annual
  returns 2022-2025 + Form 18/20. Then swap in production MOMO_* keys + UGX.

## Strategic roadmap
1. MTN production onboarding (papers paid). 2. Airtel Money rail. 3. Equity Bank API (bank
settlement). 4. Migrate KarlPOS off Pesapal. 5. BOU PSP license (needs txn history + ~500M-1B UGX).
6. Card sub-processing via Flutterwave. 7. Full KarlPOS migration.

## Competition (Uganda is crowded)
Yo! Uganda, ChapChap, Jesapay, Eversend, Flutterwave, Pesapal, DPO. Do NOT compete as "another
aggregator." Edge = owning end products (KarlPOS, vending) + going vertical/niche + better DX
(48h webhook retries, live-key-prefill docs, hashed keys, reconciliation). TK Vending live = proof.

## Key files
app/__init__.py (factory, boot-guard, health, request-id, sentry) · app/celery_app.py (factory,
beat schedule) · app/celery_worker.py (worker/beat entrypoint — see guardrail #2) · app/cli.py
(create-merchant, backfill-key-hashes, reconcile) · app/routes/api.py (merchant API) ·
app/routes/webhooks_inbound.py (rail callbacks) · app/services/ledger.py · payouts.py ·
settlement.py · reconciliation.py · rails_mtn_real.py · rails_mtn_disbursement.py ·
app/tasks/* (polling, webhooks_task, sweep, billing, reconciliation) · app/models/__init__.py
(Merchant, Transaction, Payout, ledger; hash_api_key + _sync_key_hashes) · render.yaml ·
migrations/versions/b2f1a9c4d5e6_*.py (current head).
