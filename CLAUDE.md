# Samsoftpay — Project Context & Guardrails
> Authoritative state for this repo. READ THIS FIRST every session.
> Last updated: August 2026 (after PRs #10–#14 merged to main).
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

14. **A simulated rail must never settle live money.** `rails.is_simulated(channel)` is true when
    a channel resolves to `_MockRail` — the mock flips a charge to SUCCEEDED on a timer with
    NOTHING collected, which credits the merchant ledger with money nobody paid and makes a
    vending machine dispense a real product for free. `orchestrator._simulated_rail_forbidden()`
    refuses those charges on Render BEFORE any row is written (sandbox keys exempt — mocks are
    the point of a sandbox; local/dev exempt or the offline suite could not run). Airtel Money
    and Card have NO real adapter yet and are therefore refused in production, and hidden by
    `checkout._channel_options()`. When a real Airtel rail lands, that adapter stops being a
    `_MockRail` and both the guard and the UI open up on their own — do not special-case names.
    Escape hatch for a staging box: `ALLOW_SIMULATED_RAILS`.

15. **`PaymentLink.is_test` is set ONCE, at creation, from the key that made it** —
    `create_payment_link`/`create_vending_order` in `app/routes/api.py`. The public
    checkout page (`GET /pay/<id>`, `submit`, crypto settlement — no auth on any of
    them) reads `link.is_test` to set `g.api_mode` for that request; it must NEVER
    hardcode `"live"` again. Migration `a3b4c5d6e7f8`. This is what lets an `sk_test_`
    order (1) post to the sandbox ledger, not live (guardrail 12), and (2) get
    exempted from guardrail 14's simulated-rail guard, so MTN's mock rail is usable
    for real end-to-end testing without ever touching the live ledger or weakening
    the guard for genuine live traffic. Tests: `tests/test_payment_link_mode.py`.

16. **The mock rail is DETERMINISTIC, like a real sandbox (Stripe/Pesapal/Flutterwave),
    not a coin flip.** `RAIL_SUCCESS_PROBABILITY` defaults to **1.0** — an ordinary test
    phone number succeeds every time. Deliberate failure comes ONLY from a magic number in
    `rails.TEST_PHONE_OUTCOMES` (256700000001 = insufficient_funds, …0002 = user_cancelled,
    …0003 = timeout, …0000 = documented always-succeeds). Matched on the customer phone's
    last 9 digits, so 07.../256.../spaced forms all hit the same entry. Do NOT lower the
    default probability back down "to be more realistic" — a sandbox that randomly fails an
    integrator's ordinary test transaction is a bug, not realism; that was reported live as
    "the app is broken" when it was actually the dice. Tests: `tests/test_deterministic_sandbox.py`.
    Known pre-existing flake: `tests/test_rail_guard_and_vending_events.py`'s supplier-failure
    checks occasionally miss their 12s deadline under heavy concurrent load on the dev
    machine (confirmed unrelated to this guardrail — re-running in isolation is reliably green).

17. **Gift-card redemption is deferred until AFTER the remaining charge is
    known to go through** (`app/routes/checkout.py checkout_submit`). Found by an
    independent review, not by the person who wrote the original code — the gift
    card used to be debited BEFORE `create_charge` was attempted for the
    remainder; if that charge was then rejected (blocked channel, inactive
    merchant, fee ≥ amount), the customer's gift-card balance was gone with
    nothing delivered for it. Now: validate with `redeem_gift_card(..., dry_run=True)`
    up front, only actually redeem once `create_charge` has NOT raised AND
    `txn.status != FAILED` (a synchronous rail rejection returns normally
    without raising — check the status too, not just the absence of an exception).
    `redeem_gift_card` is now row-locked (`with_for_update`, SQLite no-ops it,
    same as `ledger.lock_account_for_update`) so two concurrent redemptions of
    one code can't over-spend it.
    **Gift cards have no test/live mode of their own** — they're dashboard-issued
    real merchant liability, not API-key-scoped like PaymentLink. A sandbox
    checkout (`link.is_test`) must be refused a gift card at the `apply_voucher`
    step, never given a parallel sandbox gift-card system.
    **A fully-covered ($0 remainder) charge must still get the exact same side
    effects a rail-collected SUCCEEDED charge gets** — `is_test` from `link.is_test`
    (was hardcoded `False`), `completed_at` set, `maybe_dispense_on_success` called
    (a vending order fully paid by gift card must still dispense), and the
    `charge.succeeded` webhook enqueued.
    **Reading a newly-added row's autoincrement id without an explicit
    `db.session.flush()` first is not reliably safe** — `link.transaction_id =
    txn_obj.id` right after `db.session.add(txn_obj)` intermittently persisted
    `None` (implicit autoflush didn't reliably fire before the attribute read,
    especially after another commit earlier in the same request). This is a
    general SQLAlchemy gotcha, not gift-card-specific — grep for `.id` read
    immediately after `.add()` anywhere else before assuming it's safe.
    Tests: `tests/test_giftcard_checkout.py` (11 checks, including the exact
    money case: a rejected remainder leaves the gift card completely untouched).

18. **A bill flips to "paid" only when the rail CONFIRMS, never at initiation**
    (`orchestrator._maybe_mark_bill_paid`, called from complete_transaction's success
    path — same never-raises pattern as the vending dispense hook). It used to be set
    the moment `create_charge` returned, i.e. when the MoMo prompt was merely SENT: a
    cancelled/ignored prompt left the bill permanently "paid" with no money and no
    retry, because the public pay routes 404 any non-active bill. Found by the
    10-agent design sweep (PR #25), not by a money review — status lifecycle bugs
    hide in "UI" code.

19. **Every customer-facing payment surface must use the live-channel gate** —
    checkout's `_channel_options()`, bills' `_bill_channels()`, subscriptions'
    `_live_channels()` all filter through `orchestrator._simulated_rail_forbidden`.
    Never hardcode a channel list on a page that takes live money: pay_bill and
    subscriptions both hardcoded Airtel/card, so bill payers failed only AFTER
    filling the whole form, and every Airtel/card subscription plan silently failed
    each billing cycle. When a real Airtel rail lands, all three open up on their own.

23. **External reconciliation against MTN is the third leg — never reconcile only against
    ourselves.** `reconciliation.reconcile_against_mtn()` queries MTN's own status per
    `rail_reference` for live MTN charges and records disagreements in `recon_exceptions`
    (migration e9f0a1b2c3d4): MTN SUCCESSFUL but we're not = lost callback (money in our
    float, unbooked — the invisible failure the internal check CANNOT see); we SUCCEEDED but
    MTN FAILED = booked money MTN never took. A network-unknown answer is NEVER an exception
    (same principle as the sweep). A later run that finds agreement AUTO-RESOLVES the row
    (backdated reconciliation). Surfaced on /admin/reconciliation with a resolve action;
    hourly `reconcile_mtn` beat task logs open criticals loudly AND now alerts (guardrail 25).
    `flask reconcile-mtn` runs it on demand. Testable against MTN sandbox now. This is the
    artifact a partner's compliance review asks for first. Tests: `tests/test_mtn_reconciliation.py`.

22. **Per-merchant webhook secrets; the global WEBHOOK_SIGNING_SECRET is INBOUND-ONLY.**
    Outbound merchant deliveries are signed with `merchant.webhook_secret` (whsec_…, shown
    masked on Account → Webhooks); the global secret only authenticates inbound rail
    callbacks. They must never be the same key — one is handed to merchants, the other
    proves a callback can mark money succeeded. Migration d8e9f0a1b2c3 (backfilled).
    `webhooks.merchant_signing_secret()` generates lazily. Round 2 also: direct
    `/v1/vending/dispense` now atomically CONSUMES the charge (`vending_consumed_at`) so one
    SUCCEEDED charge = one dispense (released on supplier failure); the stale sweep is
    channel-aware (VISA/CRYPTO are externally settled → completed as success, never expired,
    and never queried against MTN's status API with a ChangeNow reference); daily
    `prune_old_rows` beat task (30d retention on sent webhooks + idempotency keys); bill-pay
    and subscribe are rate-limited (unauthenticated, fire MoMo prompts); apply_voucher
    preserves the customer's typed phone/email; `flask reset-password <email>` is the
    no-email account-recovery path.

21. **THE RECORD IS DURABLE BEFORE EVERY OUTBOUND RAIL CALL, and an ambiguous
    network failure is NEVER treated as definite non-delivery.** Both MTN adapters
    commit the txn/payout row WITH its client-generated rail reference BEFORE the
    requesttopay/transfer POST, and a requests exception during the POST raises
    `AmbiguousRailError` (the request may have reached MTN). create_charge parks
    such a charge AUTHORIZED; create_payout parks the payout AUTHORIZED with the
    earmark preserved — reconciliation/`flask stranded-payouts` resolve them from
    MTN's own answer. The old rollback-on-any-exception erased the ONLY record of
    money that may have moved: customer charged with no ledger entry, or recipient
    paid twice after a refund. Corollaries, all shipped in the same audit round
    (engineering sweep, PRs of Aug 18): complete_transaction/complete_payout
    row-lock before their terminal checks (webhook + poller race = double post);
    the pollers leave AUTHORIZED at their 90s timeout instead of failing/refunding
    (MTN can complete at 91s) and the hourly `resolve_stale_transactions` beat task
    is the safety net (skips on network error — an unknown outcome is not a
    failure); Idempotency-Keys are RESERVED before execution (concurrent duplicates
    used to both run: double prompt / double disbursement) and released on
    non-cacheable outcomes; the settlement due-query uses FOR UPDATE SKIP LOCKED
    (manual sweep button + hourly beat ran concurrently and double-credited);
    subscription billing claims each row atomically; `verified_required` actually
    verifies (it was a no-op — withdrawals were open to unverified merchants); the
    merchant sweep button runs the real settlement, not the stale-charge expirer it
    was mis-wired to (which expired every in-flight txn PLATFORM-WIDE); wallet
    top-ups mark settled_at (the hourly sweep re-settled the same money — every
    top-up double-credited); Postgres enum gained VISA/CRYPTO (migration
    b5c6d7e8f9a0 — the first production crypto settle would have crashed AFTER the
    customer sent real crypto); PSP accounts got a partial unique index
    (c6d7e8f9a0b1). Tests cover the originals; the sweep report lives in the
    workflow transcript.

20. **QRs on payment-adjacent pages are rendered IN-HOUSE (segno), never by a
    third-party service.** api.qrserver.com was hot-linked on the topup page, the
    crypto deposit address, gift-card codes (a live bearer code shipped to a third
    party!), the profile page and the account page. All now use our own endpoints
    (`checkout.order_qr_png`, `crypto_qr_png`, `profile_qr_png`). A payment QR must
    not depend on someone else's uptime nor leak its content to them.

25. **A logged error nobody reads is not an alert; a dead worker cannot report its own
    death.** `app/services/alerts.py` `send_alert()` routes a critical condition to Slack
    (`SLACK_WEBHOOK_URL`), email (`ALERT_EMAIL`/`ADMIN_EMAIL`, reusing MAIL_*) and Sentry
    (`capture_message` — the Celery integration only catches RAISES, but our money tasks LOG
    problems, they don't raise). Two hard rules: it NEVER raises (it runs inside the failing
    task) and it DEDUPES via a short-TTL Redis key (the hourly beats would otherwise re-alert
    every hour) — Redis-down FAILS OPEN (double-alert beats silence). Wired into
    `reconcile_mtn`, `reconcile_ledger`, and a new hourly `check_money_stuck` (stranded payouts
    + charges stuck AUTHORIZED > 6h — exactly the states the auto-resolvers deliberately avoid
    touching, so they pile up silently). **Heartbeat:** `monitoring.heartbeat` beat task writes
    a liveness timestamp to Redis every 60s; the WEB process exposes `GET /ops/status` (503 if
    stale, tunable `HEARTBEAT_STALE_SECONDS`, default 300) — point an external uptime monitor
    there, because the worker can't alert on its own death. `/ops/status` is deliberately NOT
    folded into `/healthz` (a dead worker must not make Render restart the healthy web dyno).
    Tests: `tests/test_alerts.py` (15 checks). Alerts fire from the WORKER — set the mail/Slack
    vars there too (see render.yaml).

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
- Migrations: `flask db upgrade` (FLASK_APP=run.py). Current head = **e9f0a1b2c3d4**
  (chain: … → a3b4c5d6e7f8 → b5c6d7e8f9a0 enum+indexes → c6d7e8f9a0b1 PSP account
  unique → d8e9f0a1b2c3 merchant webhook_secret + txn vending_consumed_at -> e9f0a1b2c3d4 recon_exceptions).

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

**Supplier doc lives at `C:\Users\DELL\Desktop\兴元XY_VendingCOM_SDK_V1.4\_apidoc.txt`** (plus the
PDF beside it). It defines ALL the XY interfaces — read it before adding any. `forwardPayCode` is
NOT an endpoint: it is one allowed value of the `paytype` field ("scan the QR code to buy"),
alongside f2F, memberCard, getGoodsCode, delayExport, offline. There is no XY interface anywhere
in that doc by which a machine asks us to START an order — machine-initiated ordering has to come
from our own software on the board (see `C:\Users\DELL\Desktop\tk\TKVendingApp`, which already
calls Samsoftpay), not from XY's cloud. Implemented: 2.1.1, 2.1.2, 2.1.4, 2.2.1. Not implemented:
2.1.3 lockMachineGoodRepertory, 2.2.2 pickup-code verification.

### XY §2.2.3 dispense-result callback — `POST /inbound/xy/dispense-result`
`app/routes/webhooks_xy.py`. XY calls US after a machine finishes dispensing. This is the only
ground truth we get: `ApplyExportGoods` returning code "1" means the command was ACCEPTED, not
that a soda came out, so before this a jammed tray left the order marked `dispensed` with the
customer's money kept. The callback carries `status`, per-product `chsl` (units actually
dispensed, 1 or 0) and `tkje`/`tksj` (supplier-side refund), and flips a wrongly-optimistic
order to `failed` so a refund decision becomes possible.
- It RECORDS outcomes and NEVER moves money — a forged callback must not be able to trigger a
  refund. Refunding stays a merchant action through the existing refund tooling.
- Signature is `MD5(secret + timestamp + sorted k=v&k=v)` with the merchant's own XY secret,
  and FAILS CLOSED (guardrail 9). The vendor doc contradicts itself on two field names
  (`status`/`state`, `dsfshdh`/`dsfshbh`) so both spellings are accepted — that tolerance is
  deliberate, do not "tidy" it away.
- Unknown orders are acknowledged with `code:"1"` (their retry semantics) but audit-logged as
  `vending.callback_unmatched`, never silently dropped.
- Blueprint must stay CSRF-exempt in `app/__init__.py` or every callback 400s.
- Give the supplier: `https://api.samsoftpay.com/inbound/xy/dispense-result`
- Tests: `tests/test_xy_dispense_callback.py` (11 checks).
- **Flagged, not changed:** an independent review noted `_finish()` trusts a validly-signed
  callback's `ok` flag unconditionally — it doesn't cross-check that the link was ever
  actually claimed via `_claim()`/had a real `ApplyExportGoods` call before accepting
  `ok=True`. In practice this requires the caller to already hold the merchant's own XY
  secret (same trust boundary as the real supplier), and the module never moves money either
  way — but it's a real gap in defense-in-depth, not a bug fix, worth a second opinion before
  changing it, since tightening it wrong could reject XY's genuine callbacks.

### Vending webhook events (for platforms integrating on top)
`charge.succeeded` means the MONEY arrived; it does NOT mean the product came out. Two vending
events now say what the machine actually did, emitted from `vending._finish()` — the one place
an order's outcome is settled — via the shared `webhooks.enqueue()`:
- **`vending.dispensed`** / **`vending.dispense_failed`** — payload carries order_id, machine,
  goods, amount, currency, charge_id, reference, vending_status, error, dispensed_at.
- Emitted only on a CHANGE of outcome, so the §2.2.3 supplier callback confirming an order we
  already marked dispensed does not send a second event for the same soda.
- Requires `merchant.webhook_url`. Delivery reuses the existing signed queue + 30s beat sweep.
- Full event catalogue is now: `charge.succeeded`, `charge.failed`, `vending.dispensed`,
  `vending.dispense_failed`. Tests: `tests/test_rail_guard_and_vending_events.py` (18 checks).

### Vending readiness — what is NOT built (as of Aug 17, 2026)
- **No emails, at all, on payment or dispense.** The ONLY email in the system is auth OTP
  (`app/services/email_service.py`, called only from `auth.py`). No receipt, no merchant order
  mail; `customer_email` is stored and never used. SMTP is not even provisioned — MAIL_* appear
  only inside a COMMENT in render.yaml, so OTPs print to the console in production too.
  Notification today = HTTP webhook only.
- **Nothing lets an XY machine START an order** (see the supplier-doc note above) — that must
  come from our own app on the board.
- `TKVendingApp` (C:\Users\DELL\Desktop\tk) now calls `POST /v1/payment-links`, shows the
  returned QR, polls for `succeeded`, and dispenses over RS232 itself — see the payment-links
  entry above. This is confirmed the RIGHT architecture per `SAMSOFTPAY_INTEGRATION_GUIDE.md`
  (their contracted deliverable: MTN integration only, machine keeps its own dispensing) — but
  is UNTESTED against real hardware. Nobody has confirmed with the supplier or on-device that
  `VmcSerialPort`'s command/status bytes (`VmcProtocol.kt`) are correct for TK's actual board;
  the file has its own "verify against the supplier's docs before going to hardware" warning.
  No `gradlew` in the project and no Android build tooling in this dev environment — the Kotlin
  was hand-written and could not be compiled here. Needs Android Studio to build/install/test.
  Still embeds a Samsoftpay secret key in `res/values/strings.xml` (sk_test_ today) — a secret
  key on a public kiosk can create charges AND payouts, so it needs a restricted credential
  before any live key ships. Samsoftpay has no scoped/restricted key type yet — that is new
  backend work, not yet scoped.

### Merged after vending (Aug 2026)
- **PR #11 — payout earmark money leak fix** (see guardrail 13). KarlPOS's detectChannel()
  sends `airtel_money` for 70/75/74/20 numbers; each rejected payout used to strand
  amount+fee in SUSPENSE. New CLI: `flask stranded-payouts`, `flask reverse-payout <id>`.
  Tests: `tests/test_payout_guards.py`.
- **PR #12 — test/live ledger split** (see guardrail 12). Sandbox money is no longer real
  money. Existing accounts migrated to `is_test=False` (preserves balances exactly).
  Tests: `tests/test_ledger_mode_split.py`. Also repaired the long-broken
  `tests/test_collections_and_disbursements.py` (dotenv pop + :memory: pitfalls, wrong fee).
- **`POST /v1/payment-links` now returns `qr_png_url` / `qr_svg_url`** (same renderer vending
  orders use — public, no auth, works for ANY link since `_qr_response` never checks
  `is_vending_order`). Built for TK's real machine architecture: their Android app
  (`C:\Users\DELL\Desktop\tk\TKVendingApp`) is NOT registered with the XY supplier's cloud and
  dispenses itself over RS232 — using `/v1/vending/orders` there would make Samsoftpay ALSO
  attempt an XY-cloud dispense against an unregistered machine, which fails and reports a false
  `vending.dispense_failed` for a product the customer already received. Plain links have no such
  side effect (`maybe_dispense_on_success` only acts on links carrying `vending_meta`). Tests:
  `tests/test_checkout.py` [1]/[1b].
- **PRs #13/#14 — `GET /v1/balance`**: per-currency reconciliation
  endpoint for platforms (KarlPOS, TK Vending). Reports the JOURNAL sum, not
  `cached_balance` (returns `consistent` flag when they disagree); mode-scoped (sk_test_
  sees sandbox only); credit-normal sign flipped so merchants see positive numbers.
  Documented on the docs page. Tests: `tests/test_balance_endpoint.py` (7 checks).
  KarlPOS already calls this from its `samsoftpay-balance-sync` edge function (retro-pos-cart
  PR #334) — it shipped before this endpoint existed, so keep the response shape stable.

### Aug 17-18, 2026 session — PRs #15-#25 (all merged + deployed)
#15 XY 2.2.3 dispense-result callback · #16 simulated-rail guard + vending webhooks ·
#17 healthz commit marker (deploy confirmation = one curl) · #18 payment-link QR fields ·
#19 PaymentLink.is_test mode scoping (migration a3b4c5d6e7f8) · #20 voucher form nesting fix ·
#21 deterministic sandbox (magic test numbers, guardrail 16) · #22 gift-card atomicity
(guardrail 17) + README rewrite · #23 checkout conversion polish · #24 landing/dashboard
honesty+polish · #25 the 10-agent design sweep (94 findings; guardrails 18-20; bills paid-at-
initiation money bug, live-channel gate on bills/subscriptions, docs samples fixed, secret-key
masking, real dashboard aggregates). TK Vending's Android app (C:\Users\DELL\Desktop\tk\
TKVendingApp) rebuilt for the QR flow and verified end-to-end on the physical machine with
real sandbox payments. **Deferred engineering items live in the memory file
`engineering-gaps-sweep-plan.md`** — run that orchestration next.

## POST-DEPLOY checklist (after a main deploy)
1. `flask db current` → expect `e9f0a1b2c3d4`
2. `flask backfill-key-hashes` (once)
3. open https://api.samsoftpay.com/healthz → `{"status":"ok","database":"up"}`
4. If worker/beat are manually configured (not blueprint-synced), set their start commands to
   `app.celery_worker` in the Render dashboard.

## OBSERVABILITY setup (one-time; makes alerting actually deliver — guardrail 25)
Until these are set, the money tasks still LOG loudly but nothing buzzes a human — no
behaviour regression, just no delivery. Alerts fire from the WORKER, so set the channel vars
on BOTH the web and worker services (web serves /ops/status; worker sends the alerts).
1. **Pick at least one channel** (set on web + worker in Render → Environment):
   - Slack: `SLACK_WEBHOOK_URL` = a Slack Incoming Webhook URL. Fastest; recommended.
   - Email: `ALERT_EMAIL` (or it falls back to `ADMIN_EMAIL`) AND the `MAIL_*` vars
     (`MAIL_HOST/PORT/USERNAME/PASSWORD/MAIL_FROM`). No `MAIL_HOST` = email channel is skipped.
   - Sentry: set `SENTRY_DSN` (already wired via CeleryIntegration) to also get `capture_message`.
   Optional: `HEARTBEAT_STALE_SECONDS` (default 300), `ALERTS_ENABLED=0` to mute everything.
2. **Wire an external uptime monitor** (UptimeRobot/Render/Pingdom) to
   `https://api.samsoftpay.com/ops/status` — it returns **503 when the Celery worker/beat
   pipeline is dead** (heartbeat older than the threshold) even though the web app is fine.
   This is the ONLY way a dead worker gets noticed — it can't alert on its own death.
   (Do NOT monitor /healthz for this — that's the web dyno's own liveness and stays 200.)
3. **Verify it's live:** `curl https://api.samsoftpay.com/ops/status` → within ~2 min of the
   worker running it should read `{"status":"ok","worker":"up",...}`. `"unknown"` = no
   heartbeat yet (cold start or no Redis); persistent `"stale"`/503 = worker or beat is down.
4. **Smoke-test a channel** (optional): `flask reconcile-mtn` with a known open exception, or
   temporarily lower `HEARTBEAT_STALE_SECONDS`, and confirm Slack/email actually receives it.

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
