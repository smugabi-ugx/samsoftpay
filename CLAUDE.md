# Samsoftpay — Project Context & Guardrails
> Authoritative state for this repo. READ THIS FIRST every session.
> Last updated: June 2026 (after PR #2 merged to main).
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
- `.env` sets `MOMO_USE_REAL=1` (real MTN sandbox). Override to `0` locally for mock/offline tests.
- Migrations: `flask db upgrade` (FLASK_APP=run.py). Current head = **b2f1a9c4d5e6**.

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

## POST-DEPLOY checklist (after a main deploy)
1. `flask db current` → expect `b2f1a9c4d5e6`
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
