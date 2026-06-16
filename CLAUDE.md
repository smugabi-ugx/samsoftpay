# Samsoftpay — Project Context

## Who is Sam
- Rogers Mugabi, trading as **Sam Software**
- Email: samsoftware75@gmail.com / smugabi@gmail.com
- Phone/MTN: +256783647260
- GitHub: https://github.com/smugabi-ugx/samsoftpay.git
- Deployed: https://samsoftpay.onrender.com

## What Samsoftpay Is
A full Flask + SQLAlchemy payment gateway built by Sam for East Africa (Uganda-first).
- Processes MTN Mobile Money (Collections + Disbursements)
- Double-entry ledger: rail_clearing → merchant_pending → merchant_available
- Merchant API with idempotency, replay protection, rate limiting
- Celery + Redis for async polling and webhook delivery
- Auto-settlement sweep every hour (24h hold then merchant_available)
- Refunds via disbursement rail
- Payment links + checkout page
- Webhook delivery with backoff retry

## Sam's Product Stack
```
KarlPOS (POS product, karlpos.com)     — like OpenFloat Africa, live with paying merchants
        |
Samsoftpay (payment rails)             — Sam's own gateway, Uganda-first
        |
MTN MoMo / future: Airtel + card sub-processing
```
KarlPOS currently runs on Pesapal. Goal: migrate KarlPOS off Pesapal onto Samsoftpay.
Pesapal created OpenFloat to compete with KarlPOS. Sam is building the rails to escape dependency.

## TK Vending (Client Project)
Client project where Samsoftpay is the hidden payment backend.
Client (TK Vending) has a merchant account on Samsoftpay — they do not know it is Sam's platform.
See C:\Users\DELL\Desktop\tk\CLAUDE.md for full TK Vending context.

## Tech Stack
- Python / Flask 3.0.3
- SQLAlchemy 2.x + PostgreSQL (Render)
- Celery 5.3.6 + Redis (Render free tier)
- Gunicorn (2 workers)
- MTN MoMo API: sandbox.momodeveloper.mtn.com
- Render deployment: web + worker + beat + redis services

## Render Deployment
- Web: samsoftpay.onrender.com (2 workers) — gunicorn run:app
- Worker: samsoftpay-worker — celery -A app.celery_worker:celery worker --concurrency=2
- Beat: samsoftpay-beat — celery -A app.celery_worker:celery beat
- Redis: samsoftpay-redis (free, allkeys-lru)
- CRITICAL: worker/beat MUST target app.celery_worker (NOT app.celery_app).
  app/celery_worker.py runs create_app()/init_celery() (wires broker=Redis,
  result backend, beat_schedule, FlaskTask) AND imports the task modules so tasks
  register. Targeting app.celery_app directly = default RabbitMQ broker + 0 tasks.
- Outbound IPs: 74.220.48.0/24 and 74.220.56.0/24 (needed for MTN Web Access Form)

## Sandbox Credentials (pesademo/.env)
- MOMO_SUBSCRIPTION_KEY=3781af42d2df4cecb20c77a1cd16e2e5
- MOMO_API_USER=7800ba3b-35ac-44e3-b17d-e2c3cfcab5c2
- MOMO_API_KEY=cb05cec32c9c4f2195bb8892b732b835
- MOMO_DISBURSEMENT_SUBSCRIPTION_KEY=e6bf2805578241feae201652e335fefa
- MOMO_BASE_URL=https://sandbox.momodeveloper.mtn.com
- MOMO_CURRENCY=EUR (sandbox only — production will be UGX)

## Key Files
- app/celery_app.py — Celery factory, beat schedule
- app/tasks/polling.py — MTN collection + disbursement polling tasks
- app/tasks/webhooks_task.py — webhook delivery + sweep
- app/tasks/sweep.py — auto settlement sweep (hourly)
- app/tasks/billing.py — subscription billing beat task
- app/services/refunds.py — charge refund via disbursement rail
- app/services/rails_mtn_real.py — MTN collections rail
- app/services/rails_mtn_disbursement.py — MTN disbursements rail
- app/services/settlement.py — sweep_to_available()
- app/routes/api.py — merchant API (charges, payouts, refunds, payment links)
- app/models/__init__.py — Transaction, Payout, Merchant, ledger models
- render.yaml — full deployment config

## Open PRs (merge on GitHub)
1. feature/celery-redis-job-queue — replaces daemon threads with Celery
2. feature/refunds-and-auto-settlement — adds refunds + auto settlement sweep

## After Merging PRs on Render
1. Create Redis service: New+ > Redis, free plan, name: samsoftpay-redis
2. Add REDIS_URL env var to web, worker, and beat services
3. Deploy worker service: celery -A app.celery_worker:celery worker --concurrency=2
4. Deploy beat service: celery -A app.celery_worker:celery beat
5. flask db upgrade runs automatically via preDeployCommand
6. After deploy, on Render shell run once: flask backfill-key-hashes

## MTN Production Onboarding (Pending)
- Contact: Felix Oluka — Felix.Oluka@mtn.com
- Send: SIT Excel report + completed Web Access Form + Email Attachment Form
- Web Access Form needs: IP ranges 74.220.48.0/24 / 74.220.56.0/24 filled and signed
- URSB: Sam Software Co Ltd needs annual returns filed (2022-2025), ~320,000 UGX
  Then get certified Form 18 + Form 20 for MTN KYC

## Strategic Roadmap
1. MTN production onboarding complete (funded by TK Vending deposit)
2. Add Airtel Money rail (covers ~40% Uganda MoMo users)
3. Equity Bank Uganda API integration (bank account settlements, not just MoMo)
4. Migrate KarlPOS MoMo transactions off Pesapal onto Samsoftpay
5. BOU PSP license application (needs transaction history + capital ~500M-1B UGX)
6. Card sub-processing via Flutterwave (no need to build card rails from scratch)
7. KarlPOS full migration off Pesapal

## Improvements Already Identified (not yet built)
- Sentry error tracking
- Real KYC verification
- Airtel Money integration
- Fraud rules engine
- Bank of Uganda PSP license
