# Samsoftpay — Payment Gateway

A full payment gateway built with Flask + SQLAlchemy. Handles both **Collections** (money in) and **Disbursements** (money out) with MTN MoMo sandbox integration.

## What this does

End-to-end, you can:

1. Create a **merchant** account with API keys (public + secret).
2. Initiate a **charge** (Collections) via HTTP API with an idempotency key.
3. The orchestrator picks a rail (MTN MoMo real sandbox, or mock Airtel/Card).
4. Async lifecycle: pending → authorized → succeeded/failed.
5. Every state change writes **double-entry journal entries** to the ledger.
6. A **webhook** is delivered (with HMAC signature) to the merchant's URL.
7. Initiate a **payout** (Disbursements) to send money out.
8. **Reconciliation** report verifies ledger consistency at any time.

## Project structure

```
samsoftpay/
├── app/
│   ├── __init__.py              # App factory, env config
│   ├── extensions.py            # SQLAlchemy instance
│   ├── cli.py                   # init-db, seed-demo CLI commands
│   ├── worker.py                # Background webhook delivery loop
│   ├── models/__init__.py       # All ORM models (Merchant, Transaction, Payout,
│   │                            # Account, JournalEntry, RailEvent, etc.)
│   ├── services/
│   │   ├── ledger.py            # Double-entry posting + balance checks
│   │   ├── orchestrator.py      # COLLECTIONS: create_charge / complete_transaction
│   │   ├── payouts.py           # DISBURSEMENTS: create_payout / complete_payout
│   │   ├── rails.py             # Collection rail adapter selector (mock + real)
│   │   ├── rails_mtn_real.py    # Real MTN MoMo Collections sandbox adapter
│   │   ├── rails_mtn_disbursement.py  # Real MTN MoMo Disbursement adapter
│   │   ├── fees.py              # Fee calculation per channel
│   │   ├── webhooks.py          # HMAC signing + delivery with backoff
│   │   ├── idempotency.py       # Idempotency-Key handling
│   │   ├── reconciliation.py    # Internal + external consistency checks
│   │   └── settlement.py        # Sweep merchant_pending -> merchant_available
│   ├── routes/
│   │   ├── api.py               # /v1/charges, /v1/payouts (public API)
│   │   ├── dashboard.py         # Merchant dashboard + reconciliation pages
│   │   └── webhooks_inbound.py  # /inbound/<channel> for rail callbacks
│   └── templates/               # Jinja templates for the dashboard
├── tests/
│   ├── test_end_to_end.py                       # Collections only (mock)
│   └── test_collections_and_disbursements.py    # Full gateway flow (mock)
├── requirements.txt
├── run.py                       # Flask entry point
├── .env.example                 # Template for credentials
└── .gitignore
```

## Quick start

```powershell
# 1. Set up environment
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure credentials — copy .env.example to .env and fill in
copy .env.example .env
notepad .env

# 3. Initialize database and create a demo merchant
flask --app run.py init-db
flask --app run.py seed-demo

# 4. Run the server (keep this terminal open)
flask --app run.py run --debug
```

Open <http://localhost:5000> to see the dashboard.

## MTN MoMo Credentials

You need two separate subscriptions on <https://momodeveloper.mtn.com>:

1. **Collections** — for receiving payments from customers
2. **Disbursements** — for sending payments out

Each subscription has its own Primary Key and requires its own API User + API Key (generated via PowerShell calls to the portal). After generation, put the six values into your `.env`:

```
MOMO_SUBSCRIPTION_KEY=<collections primary key>
MOMO_API_USER=<collections api user uuid>
MOMO_API_KEY=<collections api key>

MOMO_DISBURSEMENT_SUBSCRIPTION_KEY=<disbursement primary key>
MOMO_DISBURSEMENT_API_USER=<disbursement api user uuid>
MOMO_DISBURSEMENT_API_KEY=<disbursement api key>

MOMO_USE_REAL=1
MOMO_BASE_URL=https://sandbox.momodeveloper.mtn.com
MOMO_TARGET_ENV=sandbox
MOMO_CURRENCY=EUR
```

Set `MOMO_USE_REAL=0` (or omit) to use fast local mocks instead of real sandbox calls.

## Try it out

### Collection (charge a customer)

```powershell
$body = @{
  amount = 10000
  currency = "UGX"
  channel = "mtn_momo"
  customer = @{ phone = "256780000001" }
  reference = "order-001"
} | ConvertTo-Json

$headers = @{
  "Authorization" = "Bearer sk_test_demo123"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
  "Content-Type" = "application/json"
}

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5000/v1/charges" -Headers $headers -Body $body
```

Wait ~30 seconds, then:

```powershell
# replace txn_xxx with the id from the response above
Invoke-RestMethod -Headers @{"Authorization"="Bearer sk_test_demo123"} `
  -Uri "http://127.0.0.1:5000/v1/charges/txn_xxx"
```

### Grant the merchant available balance (for testing payouts)

```powershell
python -c "
from app import create_app
from app.extensions import db
from app.services import ledger
from app.models import AccountType
app = create_app()
with app.app_context():
    avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=1, currency='UGX')
    psp = ledger.get_or_create_account(type=AccountType.PSP_FLOAT, merchant_id=None, currency='UGX')
    ledger.post([(avail, -100000), (psp, +100000)], currency='UGX', memo='test seed')
    db.session.commit()
    print('granted 100,000 UGX available to merchant 1')
"
```

### Disbursement (pay out to a recipient)

```powershell
$body = @{
  amount = 50000
  currency = "UGX"
  channel = "mtn_momo"
  recipient = @{ phone = "256780000001"; name = "Test Recipient" }
} | ConvertTo-Json

$headers = @{
  "Authorization" = "Bearer sk_test_demo123"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
  "Content-Type" = "application/json"
}

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5000/v1/payouts" -Headers $headers -Body $body
```

### Dashboard pages

- <http://localhost:5000/> — home, list of merchants
- <http://localhost:5000/dashboard/1> — merchant detail with balances, transactions, payouts
- <http://localhost:5000/admin/reconciliation> — ledger integrity report

## Running the tests

```powershell
python tests/test_end_to_end.py
python tests/test_collections_and_disbursements.py
```

Both should print `ALL ASSERTIONS PASSED`. These use mock rails — no MoMo credentials needed.

## What's intentionally NOT here (and why)

A real PSP has all of these. Left out to keep the core flow clear:

- **Real KYC** — stub field only. Real PSPs do ID checks, sanctions screening, beneficial ownership.
- **Real fraud engine** — only basic velocity-ready scaffolding. Real PSPs use ML scoring and rules engines.
- **PCI-DSS card handling** — card channel is mocked. Never touch real PANs without certification.
- **Real Airtel Money / card integrations** — those rails are mocked. Pattern is the same as MTN; adapters just need to be written.
- **HSM, secrets manager** — uses env vars.
- **Disputes & chargebacks**.
- **Multi-currency FX**.
- **Distributed tracing, full audit log**.
- **Auth on the dashboard** — anyone can view any merchant's dashboard via the URL.
- **Persistent job queue** — webhook delivery and rail polling run in-process threads. Real PSPs use Celery/RQ with Redis so jobs survive restarts.

## Roadmap

1. **Airtel Money sandbox integration** — second MNO on the Collections side
2. **Postgres + concurrency hardening** — replace SQLite, add `SELECT ... FOR UPDATE` around ledger writes
3. **Persistent job queue (Celery + Redis)** — so polling/webhook jobs survive restarts
4. **Risk engine** — velocity rules, blocklists, anomaly detection
5. **Refunds** — using the Disbursement rail to reverse charges
6. **Merchant onboarding + KYC**
7. **Proper auth and dashboard sessions** (Flask-Login + bcrypt + MFA)
