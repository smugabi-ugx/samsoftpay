# Samsoftpay — Payment Gateway

A Flask + SQLAlchemy payment gateway for Uganda: MTN MoMo Collections and
Disbursements, a double-entry ledger with a hard split between sandbox and
live money, a merchant API with idempotency and hashed keys, Celery/Redis
background processing, hourly settlement, refunds, payment links, hosted
checkout, webhook delivery, and a scan-to-pay vending machine integration
(the first live external client, TK Vending).

**Live:** <https://api.samsoftpay.com> · Render fallback:
<https://samsoftpay.onrender.com>

---

## Start here, not here

This README is the front door. The actual authoritative, continuously
updated state of the project — what's built, what broke and why, sixteen
guardrails each documenting a real past production incident with real
numbers — lives in **[`CLAUDE.md`](./CLAUDE.md)**. Read that first if you're
about to change anything. This file exists to get a new machine (or a new
Claude session) running quickly and to say who to ask when you're stuck.

Companion docs, if you need the wider picture:
- `COMMERCIAL_READINESS.md` — audit + business roadmap
- `C:\Users\DELL\Desktop\tk\MASTER_CLAUDE.md` — TK Vending, the first external client
- `C:\Users\DELL\Desktop\tk\SAMSOFTPAY_INTEGRATION_GUIDE.md` — the contracted integration model for machine partners

## What this actually does today

- **Collections**: MTN MoMo (real sandbox adapter, `MOMO_USE_REAL` gates it),
  Airtel/Card (mocked — no real rail exists yet), Visa/Crypto (passthrough,
  settled by Flutterwave/ChangeNow before we're called).
- **Disbursements**: real MTN MoMo payout adapter, row-locked balance checks
  (no double-spend), rail resolved before any money moves (a rejected
  channel writes nothing).
- **Ledger**: double-entry, sums to zero, accounts keyed by
  `(type, merchant, currency, is_test)` — sandbox and live money physically
  cannot mix. Settlement sweeps each transaction once, after its own 24h
  hold, committed per merchant.
- **A sandbox that behaves like one**: an ordinary test phone number
  succeeds every time; specific magic numbers (documented at `/docs`)
  deterministically trigger `insufficient_funds` / `user_cancelled` /
  `timeout`, the same pattern Stripe uses with test cards.
- **A rail guard**: a channel with no real adapter behind it (Airtel, Card)
  is refused for live charges before anything is written — it cannot
  silently fake-succeed a payment nobody made.
- **Vending**: `POST /v1/payment-links` returns a QR a customer scans on
  their own phone; the machine dispenses itself and Samsoftpay never
  touches the hardware. A supplier dispense-result callback
  (`/inbound/xy/dispense-result`) corrects an order from `dispensed` to
  `failed` when a tray jams, and `vending.dispensed` /
  `vending.dispense_failed` webhooks tell the merchant's own backend what
  actually happened — not just that the money arrived.
- **Security**: API keys hashed at rest (dual-path auth during backfill),
  inbound webhooks fail closed on a bad/missing signature, the app refuses
  to boot in production with default secrets.
- **Ops**: `/healthz` reports the exact running commit (`RENDER_GIT_COMMIT`)
  so a deploy can be confirmed with one `curl`, `/livez` for pure liveness,
  Redis-backed rate limiting, structured audit log, optional Sentry.

## Tech stack

Python 3.10, Flask 3.0.3, SQLAlchemy 2.x + PostgreSQL (SQLite fine for local
dev only — the boot guard refuses SQLite in production), Celery 5.3.6 +
Redis, Gunicorn, Alembic migrations. Deployed on Render as three services
(web / worker / beat) sharing one Postgres and one Redis instance.

## Project structure

```
app/
├── __init__.py                  # app factory, boot guard, security headers, healthz
├── celery_app.py                # Celery factory + beat schedule
├── celery_worker.py             # REAL worker/beat entrypoint — see CLAUDE.md guardrail 2
├── cli.py                       # flask db, backfill-key-hashes, reconcile, stranded-payouts, ...
├── models/__init__.py           # Merchant, Transaction, Payout, PaymentLink, Account, ...
├── services/
│   ├── ledger.py                # double-entry posting, race-safe account creation
│   ├── orchestrator.py          # COLLECTIONS: create_charge / complete_transaction
│   ├── payouts.py               # DISBURSEMENTS: create_payout / complete_payout
│   ├── rails.py                 # rail adapter selection, mock outcomes, simulated-rail guard
│   ├── rails_mtn_real.py        # real MTN Collections sandbox adapter
│   ├── rails_mtn_disbursement.py# real MTN Disbursement adapter
│   ├── vending.py               # vending orders, the dispense gate, QR rendering
│   ├── xy_vending.py            # XY supplier cloud client (signed, per-merchant creds)
│   ├── webhooks.py              # HMAC signing, the shared outbound-event queue
│   ├── settlement.py            # per-txn, per-merchant settlement sweep
│   ├── reconciliation.py        # internal + external consistency checks
│   └── secrets_box.py           # encrypted per-merchant credential storage
├── routes/
│   ├── api.py                   # merchant API — charges, payouts, payment-links, vending
│   ├── checkout.py               # public hosted checkout — no auth, mode-scoped from the link
│   ├── dashboard.py             # merchant dashboard
│   ├── webhooks_inbound.py      # /inbound/<channel> — MTN/Airtel rail callbacks
│   └── webhooks_xy.py           # /inbound/xy/dispense-result — supplier callback
├── tasks/                       # Celery tasks: polling, webhook delivery, sweep, billing
└── templates/                   # Jinja — dashboard, checkout, docs
tests/                           # script-style — run directly, not pytest (see below)
migrations/versions/             # Alembic — current head in CLAUDE.md
render.yaml                      # web + worker + beat + Postgres + Redis
```

## Local dev quick start

```powershell
# Python 3.10 specifically — 3.14 is broken with this SQLAlchemy version.
py -3.10 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
notepad .env          # fill in secrets; leave MOMO_USE_REAL=0 for mock rails

flask --app run.py db upgrade      # apply migrations — this is the real setup path
flask --app run.py seed-demo       # optional: a demo merchant + API keys

flask --app run.py run --debug
```

Open <http://localhost:5000>. `app/worker.py` is legacy and unused — real
background processing is Celery, run via `app.celery_worker` (see
`CLAUDE.md` guardrail 2 for why pointing Celery at the wrong module silently
breaks both collections and disbursements).

## Running the tests

Script-style — run each file directly, not through pytest:

```powershell
$env:MOMO_USE_REAL="0"
python tests\test_end_to_end.py
python tests\test_settlement_sweep.py
python tests\test_ledger_mode_split.py
python tests\test_payout_guards.py
python tests\test_payment_link_mode.py
python tests\test_deterministic_sandbox.py
python tests\test_checkout.py
python tests\test_vending_flow.py
python tests\test_xy_dispense_callback.py
python tests\test_xy_vending_sign.py
python tests\test_rail_guard_and_vending_events.py
python tests\test_balance_endpoint.py
python tests\test_collections_and_disbursements.py
```

Every one should print an explicit `ALL ... PASSED` / `All N checks passed`
line. All run against mock rails — no MTN credentials needed. Note:
`test_rail_guard_and_vending_events.py` has one known pre-existing timing
flake under heavy concurrent machine load (documented in `CLAUDE.md`
guardrail 16) — it's reliably green in isolation.

## Try the API

```powershell
$headers = @{
  "Authorization"   = "Bearer sk_test_YOUR_TEST_KEY"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
  "X-Timestamp"     = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  "Content-Type"    = "application/json"
}
$body = @{ amount = 2500; description = "Test purchase" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "https://api.samsoftpay.com/v1/payment-links" -Headers $headers -Body $body
```

Full endpoint reference, request/response shapes, webhook signature
verification, and the sandbox test-phone-number table:
**<https://api.samsoftpay.com/docs>**

## What's genuinely still missing

Honest, not aspirational — checked against the live system, not the code's
intentions:

- **PCI-DSS card handling** — the card channel is mocked; never touch a real
  PAN without certification.
- **Real Airtel Money / card rails** — both mocked, refused for live traffic
  by the rail guard rather than silently faking a charge.
- **MTN production credentials** — sandbox only; production onboarding
  (SIT + Web Access Form to MTN) is submitted but not yet approved.
- **No load test has ever run** against this deployment.
- **Real KYC, fraud engine, disputes/chargebacks, multi-currency FX** — not
  built.
- **CHANGENOW_API_KEY** — crypto checkout needs this configured in Render or
  it returns a dev placeholder deposit address.

## Roadmap

1. MTN production onboarding (compliance papers already filed)
2. Airtel Money real collections rail
3. Equity Bank API for bank settlement
4. Migrate KarlPOS fully off Pesapal
5. BOU PSP license
6. Card sub-processing via Flutterwave
7. Security headers (HSTS, CSP, X-Frame-Options) — Cloudflare fronts the app
   but it should set these itself
8. Apex domain DNS (`samsoftpay.com` currently has no records at all)

## Getting help / reminding yourself what's going on

- **Read `CLAUDE.md` first, always.** It's dated, it lists exactly what's
  done vs open, and every guardrail exists because something broke in
  production once — the numbers are real.
- **Owner:** Rogers Mugabi, trading as Sam Software.
  samsoftware75@gmail.com / smugabi@gmail.com. MTN +256783647260.
- **Repo:** <https://github.com/smugabi-ugx/samsoftpay> (default branch `main`)
- **If something in production looks wrong:** check `/healthz` first (it
  reports the exact commit serving traffic), then `flask reconcile` for
  ledger integrity, then the audit log before assuming the worst.
- **Security issue?** Don't open a public issue with details — email the
  addresses above directly.
