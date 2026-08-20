# Samsoftpay API Changelog

All notable **API-visible** changes to Samsoftpay are documented here, newest first.
Rendered at https://api.samsoftpay.com/docs/changelog · raw markdown at
https://api.samsoftpay.com/docs/changelog.md

## Stability promise

> **Response shapes are additive-only: we add fields, we do not rename or remove them.
> Webhook events are only ever added.** Code written against any entry below keeps
> working; anything that would break an existing integration would be a new, versioned
> surface — announced here first.

The format follows [Keep a Changelog](https://keepachangelog.com/). Dates are the day the
change reached production; where only the month is certain we write the month.

---

## [2026-08-20]

### Added
- **`GET /v1/settlements`** — every release of pending → available is now a first-class,
  listable record (amount, charge count, timestamp), mode-scoped to your key. The
  published SLA: 24h hold, hourly sweep; a settlement more than 2 hours late is worth a
  support ticket.
- **`available_on` and `settled` fields on every charge response** — the actual
  settlement time once swept, the estimate (`completed_at` + 24h) before. No more
  guessing when money becomes withdrawable.
- **Deterministic payout failure simulation** — with an `sk_test_` key, payout outcomes
  are selected by the **recipient** number's last 9 digits: `256700000001`
  → `recipient_not_found`, `256700000002` → `wallet_locked`, `256700000003` → `timeout`,
  `256700000000` → always succeeds. Rehearse `payout.failed` handling before it can cost
  real money.
- **`GET /v1/webhooks`** — your recent webhook deliveries (event id, event, status,
  attempts, last response code), so a missed webhook never needs a support ticket.
- **`POST /v1/webhooks/<event_id>/resend`** — re-queue one delivery by its `evt_` id;
  returns `202`. The resent envelope keeps the same `id`, so existing dedupe logic
  handles it like any retry.
- **`Idempotent-Replayed: true` response header** on every idempotent replay (charges,
  payouts, refunds, bulk payouts) — a retrying integrator can tell a replay from a fresh
  execution. Bulk payout replays also carry `replayed: true` in the body.
- **`payout.succeeded` and `payout.failed` webhook events** — payout terminal states are
  now pushed, not just pollable.
- **Agent-readable docs**: `GET /docs.md` (the full docs as raw markdown) and
  `GET /docs/llms.txt` (an llms.txt index).
- Live charges and payouts now require a **verified business** (KYC). Test keys are
  unaffected — building never requires approval. Unverified live requests are refused
  with `400` and zero writes.

### Changed
- Live payouts can be **paused** in two safety states, both returned as a descriptive
  `400` with zero writes: a platform-wide payout freeze, and a per-merchant pause while
  an open reconciliation exception on that account is investigated. Money in is
  unaffected in both cases.

## [2026-08-19]

### Added
- **`GET /v1/charges` and `GET /v1/payouts` list endpoints** — newest first, cursor
  pagination (`limit` 1–100, `starting_after`), filters (`status`, `reference` for
  charges, `created_after`/`created_before` ISO 8601). Envelope:
  `{"object":"list","data":[...],"has_more":...,"next_cursor":...}`. Items are the exact
  same shape as the by-id endpoints. Reconcile after a missed webhook without ever
  having stored an id.
- **Subaccounts and split payments** — `POST /v1/subaccounts`, `GET /v1/subaccounts`,
  `GET /v1/subaccounts/<id>` (with per-currency balances), and a `split` array on
  `POST /v1/charges` (`{"subaccount": ..., "amount": ...}` or `{"subaccount": ...,
  "bps": ...}`), resolved against the net (amount − fee). The resolved split is exposed
  on `GET /v1/charges/<id>`. An over-allocated split is rejected `400` with zero writes.
  **Split charges cannot be refunded yet** — `POST /v1/charges/<id>/refund` returns
  `400 {"error": "split_charge_refunds_not_yet_supported"}`.
- **Collections-only API keys (`sk_test_col_…` / `sk_live_col_…`)** for code that ships
  on a public device (kiosk, vending machine). They can create charges, vending orders
  and payment links, but every money-out endpoint (`POST /v1/payouts`,
  `POST /v1/payouts/bulk`, `POST /v1/charges/<id>/refund`) refuses them with `403` —
  a key pulled off a device can never move money out. Issue from Account → API keys.
- **`Idempotency-Key` honoured on `POST /v1/payment-links` and
  `POST /v1/vending/orders`** — optional (existing integrations unaffected), but when
  sent, a retried request returns the original link/order instead of creating a
  duplicate.

### Changed
- **Webhook envelope now carries a top-level `id` (`evt_…`) and unix `timestamp`** —
  additive fields. Retries of the same event share the `id`: dedupe on it and use
  `timestamp` to enforce a replay window.
- Over-large `amount` values are rejected as a clean `400` ("amount exceeds the
  maximum of …") instead of surfacing as a `502`.
- Outbound webhook URLs are validated against private/internal network targets.

## [2026-08-18]

### Changed
- **Outbound webhooks are now signed with your per-merchant secret** (`whsec_…`, shown
  on Account → Webhooks) instead of a platform-wide secret. If you verified with a
  secret issued before this date, copy the `whsec_…` value from your dashboard.
- `POST /v1/vending/dispense` atomically **consumes** the charge: one succeeded charge
  pays for exactly one dispense (`409` on reuse); the claim is released if the machine
  supplier fails, so a genuine retry still works.

## [2026-08-17]

### Added
- **Deterministic sandbox magic numbers for charges** — an ordinary test phone number
  now succeeds every time; deliberate failure comes only from a magic number matched on
  the last 9 digits: `256700000001` → `insufficient_funds`, `256700000002` →
  `user_cancelled`, `256700000003` → `timeout`, `256700000000` → documented
  always-succeeds.
- **`qr_png_url` and `qr_svg_url` on `POST /v1/payment-links` responses** — a hosted QR
  for any link (public, no auth), rendered in-house.
- **`vending.dispensed` and `vending.dispense_failed` webhook events** — what the
  machine actually did. `charge.succeeded` means the money arrived, nothing more.
- `GET /healthz` reports the running commit, so a deploy can be confirmed with one curl.

### Changed
- **A payment link permanently remembers the mode of the key that created it** — a link
  made with an `sk_test_` key posts to the sandbox ledger and may use simulated rails;
  a live link never can.
- Simulated rails are refused for **live** charges before any write: `airtel_money` and
  `card` are sandbox-only until their real rails launch (`400` "… is not available for
  live payments yet").

### Fixed
- The sandbox no longer randomly fails ordinary test transactions (the old default
  success probability was 0.85; it is now 1.0 — failure is opt-in via magic numbers).

## [2026-08-15]

### Added
- **`GET /v1/balance`** — per-currency `available` / `pending` / `total` derived from
  the journal (with a `consistent` flag if our cache ever disagrees), for platforms
  reconciling their own wallets against us.

### Changed
- **Test and live ledgers are fully split.** Sandbox money is not real money: every
  charge, payout, refund and balance read is scoped to the mode of the key (or link)
  that created it.

## [2026-08-13 .. 2026-08-14]

### Added
- **Vending machine API** — `POST /v1/vending/orders` (scan-to-pay order + QR),
  `GET /v1/vending/orders/<id>`, `POST /v1/vending/orders/<id>/dispense` (safe retry),
  `GET /v1/vending/machines`, `GET /v1/vending/machines/<machine>/goods`, plus
  `POST /v1/vending/dispense` for merchants running their own payment flow. A machine
  dispenses only against a succeeded charge, exactly once.

### Fixed
- A payout on an unsupported channel is rejected **before** any money moves — it used
  to strand amount + fee in suspense.

## [2026-06]

### Added
- **`POST /v1/payouts/bulk`** — many payouts in one call (JSON or CSV, max 1000 items).
- Per-merchant instant settlement (no 24h hold) available on request for approved
  merchants.
- **`POST /v1/charges/<id>/refund`** — refund a succeeded charge, net of the original
  fee, delivered as a disbursement to the customer.
- `mode` and `created_at` fields on charge and payout responses.
- Security headers on all responses (HSTS, CSP, X-Frame-Options, and friends).

### Changed
- API keys are hashed at rest; inbound rail callbacks verify signatures fail-closed.

## [2026-06-07]

### Added
- Asynchronous processing backbone (Celery + Redis): rail polling, webhook delivery
  with backoff, hourly settlement sweep with a 24h hold.
