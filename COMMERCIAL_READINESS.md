# Samsoftpay — Commercial Readiness Plan

> Goal: take Samsoftpay from "works in sandbox" to "commercial-grade, carries real money
> at volume, safe to share an API with third-party integrators after verification."
> Status date: June 2026. Already deployed on Render.

This is the bar a payment processor must clear before holding other people's money.

---

## A. SECURITY AUDIT — findings

### FIXED (this session) — were live on production
| # | Issue | File | Status |
|---|---|---|---|
| 1 | `/first-setup` let ANYONE reset the admin password (hardcoded `SamsoftAdmin2025!`) and take over the whole system | app/routes/auth.py | FIXED — now token-gated, never resets an existing admin, random one-time password |
| 2 | Inbound rail webhook accepted forged "payment succeeded" callbacks when the signing secret was the known default — an attacker could mark any charge paid and trigger dispense without paying | app/routes/webhooks_inbound.py | FIXED — fails closed; rejects placeholder secret in production |
| 3 | App booted in production with default `SECRET_KEY` / `WEBHOOK_SIGNING_SECRET` | app/__init__.py | FIXED — refuses to boot on Render with insecure defaults |

### STILL TO DO — security
| # | Issue | File | Severity | Fix |
|---|---|---|---|---|
| S1 | API keys stored in PLAINTEXT in DB. A DB leak = every merchant impersonated | app/models/__init__.py:98-100 | HIGH | Store bcrypt/argon2 hash of secret keys; compare with hmac.compare_digest |
| S2 | API key lookup not constant-time | app/routes/api.py:28-46 | MEDIUM | Falls out of S1 fix (hash + compare_digest) |
| S3 | Outbound webhook stores merchant's raw response body — possible PII leak into dashboard | app/services/webhooks.py:57-67 | MEDIUM | Store status code only, not body |
| S4 | Rotate the MTN sandbox credentials currently in `.env`; ensure `.env` is git-ignored and never committed | .env | HIGH | Rotate keys; confirm .gitignore |
| S5 | Set strong SECRET_KEY, WEBHOOK_SIGNING_SECRET, SETUP_TOKEN on Render env | Render | HIGH | Generate with secrets.token_urlsafe(32) |

---

## B. SCALE & ROBUSTNESS AUDIT — findings
(Targets behaviour at 1000+ transactions/minute.)

| # | Issue | File | Severity | Fix |
|---|---|---|---|---|
| R1 | **Double-spend**: payout balance check has no row lock. Two concurrent payouts can both pass and overdraft the merchant | app/services/payouts.py:76-87 | CRITICAL | `SELECT ... FOR UPDATE` on the available-balance account before checking |
| R2 | SQLAlchemy connection pool left at default (5+10). Saturates under load → timeouts | app/__init__.py | CRITICAL | Set SQLALCHEMY_ENGINE_OPTIONS: pool_size 20-50, max_overflow, pool_pre_ping, pool_recycle |
| R3 | `get_or_create_account` race → UNIQUE violation on a merchant's first concurrent transactions | app/services/ledger.py:46-57 | HIGH | INSERT ... ON CONFLICT DO NOTHING, then select |
| R4 | Settlement sweep commits ALL merchants in one transaction → lock contention blocks payouts | app/services/settlement.py | HIGH | Commit per merchant; wrap each in try/rollback |
| R5 | Missing compound index on (status, completed_at) for the sweep query | app/models/__init__.py | MEDIUM | Add index via migration |
| R6 | Polling: 18 retries x 5s per txn = huge Redis backlog at high volume | app/tasks/polling.py | MEDIUM | Shorter window + a batched reconciliation job |
| R7 | VERIFY: possible column mismatch between webhook task code and DB schema (attempt_count vs attempts, signing_secret, delivered_at) | app/tasks/webhooks_task.py vs migrations | NEEDS CHECK | Confirm against models; add migration if real |

> R1, R2, R7 are the ones that bite first. R7 must be verified before relying on webhooks.

---

## C. THE PATH TO COMMERCIAL GRADE (phased)

### Phase 0 — Stop the bleeding (DONE this session)
- [x] Close the `/first-setup` admin takeover
- [x] Make inbound webhook verification fail closed
- [x] Refuse to boot in production with default secrets

### Phase 1 — Money-safety (before TK Vending real money) — ~days
- [ ] R1 row-level locking on balance checks (no double-spend)
- [ ] R2 connection pool config
- [ ] R7 verify + fix webhook schema mismatch
- [ ] S1 hash API keys at rest
- [ ] S4/S5 rotate secrets, set strong env vars on Render
- [ ] Load test: simulate 1000 charges/min against staging, confirm no overdraft, no errors

### Phase 2 — Operational maturity — ~weeks
- [ ] Error tracking (Sentry) wired in — know when production breaks
- [ ] Structured logging + request IDs (trace a transaction end to end)
- [ ] Uptime monitoring + alerting (UptimeRobot / Render health checks)
- [ ] Daily DB backups verified restorable (Render Postgres backups on)
- [ ] Settlement sweep batched per merchant (R4) + indexes (R5)
- [ ] Reconciliation job: nightly check ledger balances == journal sums
- [ ] Runbook: what to do when MTN is down, when Redis is down, when a payout is stuck

### Phase 3 — Integrator-ready (share API with others after verification) — ~weeks
- [ ] Public API reference (versioned, /v1) — see SAMSOFTPAY_API.md
- [ ] Sandbox keys self-serve so integrators test without real money
- [ ] Merchant onboarding + KYC/verification flow before issuing live keys
- [ ] Webhook signing docs + signature verification sample code for integrators
- [ ] Rate limits documented per plan; per-merchant key rate limiting confirmed on Redis
- [ ] Status page + changelog
- [ ] Terms of service + data handling policy

### Phase 4 — Regulatory (operate legally at scale) — ~months
- [ ] Sam Software Co Ltd compliance (URSB filings) — already on the critical path
- [ ] Bank of Uganda PSP licence (capital ~500M-1B UGX, needs transaction history)
- [ ] PCI-DSS SAQ if/when card data is in scope (use Flutterwave hosted to avoid most of it)
- [ ] AML/fraud monitoring rules

---

## D. RENDER-SPECIFIC INFRA STEPS (we are already here)
- [ ] Set env vars: SECRET_KEY, WEBHOOK_SIGNING_SECRET, SETUP_TOKEN, ADMIN_EMAIL (strong, random)
- [ ] Confirm Redis service attached; REDIS_URL on web + worker + beat
- [ ] Turn on Postgres daily backups; test a restore once
- [ ] Add a /healthz check + Render health check + external uptime monitor
- [ ] Upgrade web/worker from free tier (free tier sleeps — unacceptable for payments)
- [ ] Set SQLALCHEMY_ENGINE_OPTIONS (pool) — free/starter Postgres has low connection caps, tune accordingly
- [ ] Add Sentry DSN env var once Sentry is set up

---

## D2. PROGRESS LOG — Session 2 (June 16, 2026) — verified locally on Python 3.10, NOT committed

Local env now runs (Python 3.10 venv + SQLite), so changes are RUN-VERIFIED, not just syntax-checked.

Bugs found by actually running the code (would have hit production):
- `threading` import missing in BOTH rails_mtn_real.py and rails_mtn_disbursement.py
  (left over from the Celery refactor) — crashed MTN collections AND disbursements at import. FIXED.

Built + verified this session:
- [x] Settlement correctness: each txn settles once, only after ITS own hold (new settled_at
      column + index). Fixes wholesale-sweep bug that released money still inside its hold window.
- [x] Settlement batching (R4): commit per merchant, one bad merchant can't stall the rest.
- [x] Resilient poller enqueue: a Redis/broker blip no longer fails an accepted charge/payout
      (webhook + sweep remain the completion path).
- [x] Rate limiter uses Redis in production (limits now hold across gunicorn workers).
- [x] /healthz (DB-checked) + /livez endpoints; Render healthCheckPath -> /healthz.
- [x] Migration b2f1a9c4d5e6 extended with settled_at + index; verified to match the model.
- [x] New regression test tests/test_settlement_sweep.py (passes). End-to-end ledger test passes.

Independent correctness audit run on the changes:
- Confirmed CORRECT: payout row-lock prevents double-spend; get_or_create_account savepoint
  pattern; migration matches model exactly.
- Applied: post ledger BEFORE marking settled_at (strictly safer ordering); tidied _auth fallback.

Built + verified — round 2 (same session):
- [x] Nightly ledger reconciliation: Celery beat task (02:30) + `flask reconcile` CLI. Alarms
      (error log) if the journal doesn't sum to zero or a cached balance drifts. Verified runs clean.
- [x] Sentry error tracking: auto-inits IF SENTRY_DSN set (Flask + Celery integrations,
      send_default_pii=False). No-op locally. Added sentry-sdk[flask] to requirements.
- [x] Request-ID tracing: every request tagged (X-Request-ID echoed in response), logs carry
      [req:<id>] so a transaction can be traced end to end.
- [x] S3 fix: webhook delivery no longer stores merchants' full response bodies (PII) — only a
      200-char snippet on failure, nothing on success. Applied in both webhooks.py and the Celery task.
- [x] Verified `flask create-merchant` produces strong keys AND the hash columns auto-populate.

Still open (next): custom domain + off free tier (your action on Render), load test at
1000 charges/min, optional structured-JSON logs, and creating the TK Vending merchant on the
PRODUCTION (Render) database to hand the client a real test key.

## E. ONE-LINE SUMMARY
We're past "prototype." Phase 0 is done. Phase 1 (money-safety) is the gate before TK Vending
moves real shillings. Phases 2-3 make it safe to let other developers integrate. Phase 4 makes
it legal at scale. None of it is huge — it's a few focused weeks, done in order.
