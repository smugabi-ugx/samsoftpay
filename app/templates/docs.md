# Samsoftpay API — Integration Guide

> Samsoftpay is a payment gateway for Uganda: MTN Mobile Money collections and disbursements,
> hosted checkout, payment links, vending-machine payments, subaccount splits and signed webhooks.
> This is the complete, agent-friendly markdown version of https://api.samsoftpay.com/docs.
> Machine-readable index: https://api.samsoftpay.com/docs/llms.txt

## Quickstart

You need exactly **two things**:

1. **Base URL:** `https://api.samsoftpay.com` (same for test and live).
2. **An API key**, sent as `Authorization: Bearer <key>`:
   - `sk_test_…`: sandbox. Sandbox money lives on a separate test ledger and never touches real balances.
   - `sk_live_…`: live money.
   - `sk_test_col_…` / `sk_live_col_…`: **collections-only** keys for code that ships on a public
     device (kiosk, vending machine). They can create charges, vending orders and payment links,
     but are refused with `403` on payouts, bulk payouts and refunds. A key pulled off a device
     can never move money out.

No IPN registration, no IP whitelisting, no token exchange. Going live is a one-line change: swap
in your `sk_live_` key. Nothing else changes.

### Required headers on every POST

| Header | Value |
|---|---|
| `Authorization` | `Bearer sk_test_…` (always required, GET too) |
| `Content-Type` | `application/json` |
| `Idempotency-Key` | A unique UUID per logical request (see Idempotency below) |
| `X-Timestamp` | Current Unix timestamp in **seconds**. Requests more than 5 minutes old (or more than 60 s in the future) are rejected with `400` to prevent replay. |

### First charge

```bash
curl -X POST https://api.samsoftpay.com/v1/charges \
  -H "Authorization: Bearer sk_test_YOUR_TEST_KEY" \
  -H "Idempotency-Key: my-first-charge-001" \
  -H "X-Timestamp: $(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{"amount":5000,"currency":"UGX","channel":"mtn_momo","customer":{"phone":"256700000000"},"reference":"order-001"}'
```

Response:

```json
{
  "id": "txn_9f2c41d8a3b7e615",
  "mode": "test",
  "status": "authorized",
  "amount": 5000,
  "fee": 200,
  "currency": "UGX",
  "channel": "mtn_momo",
  "reference": "order-001"
}
```

Poll `GET /v1/charges/<id>` until `status` is `succeeded`, or configure a webhook URL on your
Account page and receive signed events instead. Charge statuses:
`pending | authorized | succeeded | failed`.

## Endpoints

| Method & path | Purpose |
|---|---|
| `POST /v1/charges` | Collect money from a customer (MTN MoMo live; Airtel/card sandbox-only until those rails launch) |
| `GET /v1/charges/<id>` | Retrieve one charge |
| `GET /v1/charges` | List/search charges (cursor pagination; filters: `status`, `reference`, `created_after`, `created_before`, `limit` 1–100) |
| `POST /v1/charges/<id>/refund` | Refund a succeeded charge (net of the original fee; full-scope key only) |
| `POST /v1/payouts` | Send money to a mobile-money wallet (full-scope key only) |
| `POST /v1/payouts/bulk` | Many payouts in one call (full-scope key only) |
| `GET /v1/payouts` | List payouts |
| `GET /v1/balance` | Per-currency balance for reconciliation (journal-derived; mode-scoped) |
| `GET /v1/settlements` | Every release of pending → available (amount, charge count, timestamp; mode-scoped) |
| `POST /v1/payment-links` | Create a shareable hosted-checkout link (returns `url`, `qr_png_url`, `qr_svg_url`) |
| `POST /v1/vending/orders` | Create a scan-to-pay vending order (QR + auto-dispense) |
| `GET /v1/webhooks` | List your recent webhook deliveries: event id, event, status, attempts, last_response_code |
| `POST /v1/webhooks/<event_id>/resend` | Re-queue one delivery by its `evt_` id; returns `202` |
| `GET /v1/resolve-account?phone=…` | Confirm a payout destination BEFORE money moves: MTN's own `active` answer + `registered_name` (full-scope keys only) |

Fees: charges 1.5% (min UGX 200, cap UGX 5,000), returned in the `fee` field. You receive
`amount - fee`. Payouts: flat UGX 750, fully refunded with the amount if the payout fails.
All amounts are integers in minor units (whole UGX).

### Settlement timing (set your watch by it)

A succeeded charge becomes withdrawable **24 hours after it completes**; the settlement sweep
runs **hourly**, so the release lands at most an hour after the hold expires. Every charge
response carries `available_on` (the actual settlement time once released, else the estimate)
and `settled` (boolean). Each release is a first-class record on `GET /v1/settlements` and the
dashboard's Settlements table. **Escalation threshold:** if a settlement is more than 2 hours
late, contact support with the charge id. You should never have to discover a delay yourself.

## Webhooks: the operations contract

Configure `webhook_url` on your Account page. Samsoftpay POSTs a signed JSON envelope on every
state change.

### Events

`charge.succeeded`, `charge.failed`, `payout.succeeded`, `payout.failed`,
`vending.dispensed`, `vending.dispense_failed`, `dispute.opened`.

**`charge.succeeded` means the MONEY ARRIVED, and nothing else.** For vending, it does not mean the
product came out: wait for `vending.dispensed` before treating the product as delivered.

### Envelope

Sent as **canonical JSON with no spaces** (the signature covers these exact bytes):

```json
{"id":"evt_1a2b3c4d5e6f7a8b9c0d1e2f","timestamp":1755900000,"event":"charge.succeeded","data":{...}}
```

- `id`: unique per event. **Retries of the same event share the same id, so dedupe on it.**
- `timestamp`: Unix seconds when the event was created. Use it to enforce a replay window.

### Signature

Header `X-Samsoftpay-Signature` is the **HMAC-SHA256 hex digest of the RAW request body**, keyed
with your `whsec_…` secret from **Account → Webhooks**. The secret is per-merchant, never
the platform's global signing secret. Verify over the raw bytes exactly as received; **never
re-serialize the JSON first** (any change in key order or whitespace breaks the digest).

Python (Flask):

```python
import hmac, hashlib
from flask import request, abort

WEBHOOK_SECRET = "whsec_your_secret_from_account_webhooks"

@app.post("/webhooks/samsoftpay")
def handle_webhook():
    sig = request.headers.get("X-Samsoftpay-Signature", "")
    expected = hmac.new(WEBHOOK_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        abort(400, "invalid signature")

    event = request.get_json()
    # Dedupe on event["id"] — retries share it.
    # Return 200 FAST; do real work asynchronously.
    return {"ok": True}
```

Node.js (Express):

```js
const crypto = require("crypto");

app.post("/webhooks/samsoftpay", express.raw({ type: "application/json" }), (req, res) => {
  const sig = req.headers["x-samsoftpay-signature"] || "";
  const expected = crypto
    .createHmac("sha256", process.env.WEBHOOK_SECRET)
    .update(req.body) // the RAW body Buffer — never JSON.parse-then-stringify
    .digest("hex");

  const a = Buffer.from(expected);
  const b = Buffer.from(sig);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return res.status(400).send("invalid signature");
  }

  const event = JSON.parse(req.body);
  // Dedupe on event.id; respond 200 fast, process async.
  res.json({ ok: true });
});
```

### Delivery, timeout and retries

- Respond with any **2xx within 5 seconds**. Return 200 immediately and process asynchronously.
  A slow handler is indistinguishable from a dead one and will be retried.
- The first attempt fires immediately. On failure we back off:
  **1m, 5m, 30m, 2h, 6h, 12h, 24h, 48h**, up to **8 attempts** in total, so a delivery keeps
  retrying for roughly two days after the event.
- Retries carry the **same envelope `id`**. Dedupe on it, not on receipt count.
- Missed everything? `GET /v1/webhooks` shows recent deliveries and
  `POST /v1/webhooks/<event_id>/resend` re-queues one; `GET /v1/charges` lets you reconcile by
  listing.

### Egress IPs

Webhook deliveries originate from these static ranges. Allowlist them if your endpoint is
firewalled:

- `74.220.48.0/24`
- `74.220.56.0/24`

## Testing: the deterministic sandbox

With an `sk_test_` key no real network is touched; a charge settles a few seconds after creation.
**Every ordinary phone number succeeds, every time.** Failure is something you ask for with a
magic number, matched on the **last 9 digits** (so `0700000001`, `256700000001` and
`256 700 000 001` all trigger the same outcome):

| Phone number | Outcome | `failure_reason` |
|---|---|---|
| `256700000000` | Always succeeds (documented success number) | n/a |
| `256700000001` | Fails | `insufficient_funds` |
| `256700000002` | Fails | `user_cancelled` |
| `256700000003` | Fails | `timeout` |

### Payout failure simulation

Payouts (`sk_test_` keys) have their own deterministic scenario set, matched on the
**recipient** phone's last 9 digits. Rehearse your `payout.failed` handling before it
happens with real money:

| Recipient number | Outcome | `failure_reason` |
|---|---|---|
| `256700000000` | Always succeeds | n/a |
| `256700000001` | Fails | `recipient_not_found` |
| `256700000002` | Fails | `wallet_locked` |
| `256700000003` | Fails | `timeout` |

A failed payout refunds the amount **and** the fee to your available balance, and fires the
`payout.failed` webhook.

### Pre-flight a destination (Hakikisha)

`GET /v1/resolve-account?phone=0771234567` returns `{"msisdn", "active", "registered_name"}`:
MTN's own answer about the wallet, before any money is earmarked. In the sandbox this is
deterministic: `256700000001` resolves `active: false`; every other number is an active
`SANDBOX HOLDER …`. Treat `active: false` as the hard stop; `registered_name` is the
double-check (it can be `null` while our MTN KYC scope is pending). Full-scope keys only.

### Disputes

Every hosted receipt carries a public **"Report a problem"** link. A filed report creates a
dispute you see on Dashboard → Disputes and as a `dispute.opened` webhook
(`data`: `id`, `charge_id`, `reason`, `details`, `contact`, `amount`, `currency`,
`opened_at`). **`details` and `contact` are raw customer input**. Escape them before
rendering them in any UI, exactly as you would any untrusted text. A dispute never moves money. If a refund is due, use the normal refund
tooling. Published timelines: the merchant is expected to respond within **72 hours**;
unresolved disputes escalate to support@samsoftpay.com.

### Exact `charge.failed` payloads

Each failure number produces a `charge.failed` webhook whose `data` is exactly this shape.
The core fields carry the same names and values as `GET /v1/charges/<id>`, namely `id`, `mode`,
`status`, `amount`, `fee`, `currency`, `channel`, `reference`, `failure_reason`,
`completed_at` (ISO 8601), so the same parser reads both. Two differences to code against:

- The webhook **also** sends `merchant_reference`, a legacy duplicate of `reference` with the
  identical value. It is kept forever for older integrations; new code should read `reference`.
- The webhook is a **subset**: it omits the `rail_reference`, `created_at`, `settled` and
  `available_on` fields that `GET /v1/charges/<id>` returns. Fetch the charge if you need them.

`256700000001`:

```json
{"id":"txn_9f2c41d8a3b7e615","amount":5000,"fee":200,"currency":"UGX","channel":"mtn_momo","status":"failed","reference":"order-001","merchant_reference":"order-001","mode":"test","failure_reason":"insufficient_funds","completed_at":"2026-08-20T10:30:00+00:00"}
```

`256700000002`:

```json
{"id":"txn_9f2c41d8a3b7e615","amount":5000,"fee":200,"currency":"UGX","channel":"mtn_momo","status":"failed","reference":"order-001","merchant_reference":"order-001","mode":"test","failure_reason":"user_cancelled","completed_at":"2026-08-20T10:30:00+00:00"}
```

`256700000003`:

```json
{"id":"txn_9f2c41d8a3b7e615","amount":5000,"fee":200,"currency":"UGX","channel":"mtn_momo","status":"failed","reference":"order-001","merchant_reference":"order-001","mode":"test","failure_reason":"timeout","completed_at":"2026-08-20T10:30:00+00:00"}
```

A successful charge's `charge.succeeded` data is identical in shape with
`"status":"succeeded"` and `"failure_reason":null`.

## Idempotency

- `Idempotency-Key` is **required** on the money POSTs (`/v1/charges`, `/v1/charges/<id>/refund`,
  `/v1/payouts`, `/v1/payouts/bulk`) and **optional but honored** on `/v1/payment-links` and
  `/v1/vending/orders`.
- Keys are **reserved before execution**: two concurrent requests with the same key can never
  both run. The loser gets `409` ("still in flight") and should retry shortly.
- A replayed request returns the **original response**, unchanged, plus the header
  **`Idempotent-Replayed: true`** so you can tell a replay from a fresh execution.
- `409` means either: same key with a **different request body**, or the original request is
  **still in flight**.
- Discipline:
  - Generate keys with `uuid.uuid4()` / `crypto.randomUUID()`: one key per logical operation.
  - On a network error, timeout or 5xx, **retry with the SAME key**. That is the whole point.
  - Keys are retained for **30 days**; do not reuse a key for a different operation, ever.

## Integration best practices

- **Give value only on a definite outcome.** `pending` / `authorized` is NOT failed. An
  ambiguous network answer parks a charge, it does not fail it. Never release goods, credit a
  wallet or mark an order paid until `status` is `succeeded`.
- **Confirm before fulfilling**: via the `charge.succeeded` webhook or `GET /v1/charges/<id>`,
  never from your own client-side signal alone.
- **Verify `amount`, `currency` and your `reference`** on the confirmed charge match the order
  you are about to fulfil, before delivering value.
- **Prefer webhooks over polling**; poll `GET /v1/charges/<id>` as a fallback and reconcile with
  `GET /v1/charges` and `GET /v1/balance`.
- **Dedupe on the webhook envelope `id`**: retries and resends share it.
- **Never embed a full `sk_` key in a kiosk, mobile app or on-device code.** Use a
  collections-only `sk_*_col_` key there. It cannot call payouts or refunds.
- **Never log full API keys** or webhook secrets; store them in environment variables.
- **TLS only.** The API is HTTPS-only; your webhook endpoint should be too.
- For vending: money and delivery are separate facts. `charge.succeeded` = paid;
  `vending.dispensed` = the machine actually released the product.

## Error catalog

All `/v1` errors are JSON with a single `error` field: `{"error": "..."}`. Every error the
API returns is listed here verbatim (dynamic parts shown as `…`). The **Retry?** column is
the contract: *yes, same key* = retry with the **same** `Idempotency-Key`; *no, fix
request* = change something first; *no, permanent* = retrying will never help.

### Authentication & headers

| Error | HTTP | Cause | Retry? |
|---|---|---|---|
| `missing bearer token` | 401 | No `Authorization: Bearer <key>` header | no, fix request |
| `invalid api key` | 401 | Unknown, rotated or revoked key | no, fix request |
| `this endpoint requires a full secret key; collections-only keys cannot move money out` | 403 | An `sk_*_col_` key on `POST /v1/payouts`, `/v1/payouts/bulk` or `/v1/charges/<id>/refund`; fires on scope before any resource lookup | no, call from your server with a full key |
| `X-Timestamp header required. …` | 400 | Missing `X-Timestamp` on a POST | no, fix request |
| `X-Timestamp must be an integer Unix timestamp` | 400 | Non-integer value | no, fix request |
| `request timestamp is …s old — max allowed skew is 300s` | 400 | Timestamp older than 5 minutes (replay protection) | yes, with a fresh timestamp |
| `request timestamp is too far in the future — check your system clock` | 400 | More than 60 s ahead of our clock | yes, after fixing your clock |

### Idempotency & rate limits

| Error | HTTP | Cause | Retry? |
|---|---|---|---|
| `Idempotency-Key header required` | 400 | Missing on a money POST (charges, payouts, bulk, refund) | no, fix request |
| `idempotency key reused with different request body` | 409 | Same key, different payload; a key names one logical operation | no, new key for new work |
| `a request with this Idempotency-Key is still in flight — retry shortly` | 409 | The original request has not finished (keys are reserved before execution; concurrent duplicates never both run) | yes, same key, shortly |
| rate limit message | 429 | Too many requests. Defaults: charges 120/min, payouts 30/min, refunds 10/min (configurable per deployment) | yes, back off; honour `Retry-After` |

### Charges

| Error | HTTP | Cause | Retry? |
|---|---|---|---|
| `invalid request: …` | 400 | Malformed body (missing `amount`, bad `channel`, `split` not a list, …) | no, fix request |
| `amount must be positive` | 400 | Zero or negative amount | no, fix request |
| `amount exceeds the maximum of …` | 400 | Amount above the per-transaction ceiling | no, fix request |
| `demo only supports UGX` | 400 | Currency other than `UGX` | no, fix request |
| `merchant is not active` | 400 | Account deactivated | no, permanent until support reactivates |
| `live charges require a verified business — complete verification on your dashboard (test keys work immediately)` | 400 | `sk_live_` key before KYC verification; zero writes | no, verify first; test keys work now |
| `… is not available for live payments yet` | 400 | Channel with no real rail (`airtel_money`, `card`) on a live key. A simulated rail must never settle live money; zero writes | no, `mtn_momo` live, or a test key |
| `fee exceeds amount` | 400 | Amount too small to cover the minimum fee (UGX 200) | no, fix request |
| `invalid split: …` | 400 | Bad `split` array: unknown/inactive/duplicate subaccount, shares exceeding the net, bad `amount`/`bps`; zero writes | no, fix request |
| `payment rail temporarily unavailable — retry with the same Idempotency-Key` | 502 | Transient rail/network failure before anything was recorded; deliberately **not** cached against your key | **yes, same key** |

### Payouts & refunds

| Error | HTTP | Cause | Retry? |
|---|---|---|---|
| `live payouts require a verified business — complete verification on your dashboard (test keys work immediately)` | 400 | `sk_live_` key before KYC verification; zero writes | no, verify first |
| `insufficient available balance: have …, need … (amount … + fee …)` | 400 | Available (settled) balance below amount + fee; pending money does not count until it settles | no, top up or wait for settlement, then a new key |
| `no disbursement adapter for channel …` | 400 | Payout channel with no disbursement rail (e.g. `airtel_money`); rejected before any money moves | no, use `mtn_momo` |
| `payouts are temporarily paused platform-wide for a safety review — no action needed on your side; money in is unaffected` | 400 | Platform payout freeze during a safety event; zero writes | no, wait; money in unaffected |
| `payouts are paused on this account while a payment reconciliation issue is investigated — support has been notified; your balance is safe and money in is unaffected` | 400 | An open critical reconciliation exception on your account pauses your live payouts until resolved | no, wait for support |
| `disbursement rail unavailable: …` | 400 | The rail failed cleanly before the transfer was sent. Nothing left our side | yes, after the outage, with a **new** key (this response is cached against the old one) |
| `no payout items provided (JSON {payouts:[...]} or CSV)` / `batch too large (max 1000 items per call)` | 400 | Bulk payout body empty or over 1000 items | no, fix request |
| `already_refunded` | 400 | The charge was already refunded; refunds happen once | no, permanent |
| `cannot_refund_…_transaction` | 400 | Refund on a charge that is not `succeeded` | no, only succeeded charges refund |
| `split_charge_refunds_not_yet_supported` | 400 | The charge was created with a `split`. Split refunds are deliberately not enabled yet; contact support to reverse one; zero writes | no, permanent (for now) |
| `mode_mismatch: this is a … charge — use your … key to refund it` | 400 | Refunding a test charge with a live key or vice versa; zero writes | no, use the key of the matching mode |

### Vending

| Error | HTTP | Cause | Retry? |
|---|---|---|---|
| `vending is not enabled for this merchant` | 403 | Dashboard → Vending is switched off | no, enable it first |
| `machine not registered to this merchant` | 404 | Unknown machine number for your account | no, fix request |
| `cannot dispense: charge status is …, not succeeded` | 400 | Dispense attempted against a charge that has not succeeded | no, wait for `succeeded` |
| `this charge has already paid for a dispense` | 409 | One succeeded charge pays for exactly one dispense | no, permanent |

### Asynchronous failures (not HTTP errors)

A charge or payout that is *accepted* (HTTP 201) can still fail later on the rail. That
outcome arrives as `"status": "failed"` with a `failure_reason`, via webhook
(`charge.failed` / `payout.failed`) or polling. Reproduce each one deterministically in
the sandbox with a magic number (see Testing above):

| `failure_reason` | Where | Test number that reproduces it |
|---|---|---|
| `insufficient_funds` | charge | customer phone `256700000001` |
| `user_cancelled` | charge | customer phone `256700000002` |
| `timeout` | charge | customer phone `256700000003` |
| `recipient_not_found` | payout | recipient phone `256700000001` |
| `wallet_locked` | payout | recipient phone `256700000002` |
| `timeout` | payout | recipient phone `256700000003` |

A `404` anywhere means the resource does not exist, belongs to a different merchant, or
belongs to the other mode (test vs live) than your key. On any `5xx` or network error the
outcome is unknown. Retry with the **same** `Idempotency-Key`.

## Go-live checklist

The path from first sandbox charge to real money, in order. Do not skip the config-drift
checks. They catch the classic "worked in test, silently broken in live" failures.

1. **Build in the sandbox** with your `sk_test_` key. Sandbox money lives on a separate
   ledger. Nothing you do here can touch real balances.
2. **Test failure paths in both directions with the magic numbers.** Charges: customer
   phones `256700000001/2/3` fail with `insufficient_funds` / `user_cancelled` /
   `timeout`. Payouts: the same numbers as the **recipient** fail with
   `recipient_not_found` / `wallet_locked` / `timeout`. Confirm your `charge.failed` and
   `payout.failed` handlers, your webhook signature verification, and your dedupe on the
   envelope `id`.
3. **Verify your business (KYC)** on the dashboard. Live charges and payouts are refused
   with `400` until verified. Test keys keep working throughout.
4. **Config-drift checks before switching keys:**
   - Live webhook URL set and verified. Use the **Send test event** button on
     Account → Webhooks, which POSTs a signed `test.ping` event to your endpoint;
     confirm your handler verifies the signature and returns 2xx.
   - Live key issued and stored **server-side only** (environment variable, never in
     client code, mobile apps or repos).
   - Kiosk and vending devices carry `sk_live_col_` **collections-only** keys, never
     full keys. A key pulled off a device must not be able to move money out.
5. **First live charge, small amount.** Verify it via `GET /v1/charges/<id>` **and**
   confirm the `charge.succeeded` webhook arrived and verified. Both paths must work
   before volume.
6. **First live settlement confirmed on `GET /v1/settlements`**. After the 24h hold
   (hourly sweep), the release appears as a settlement record and your `available`
   balance on `GET /v1/balance` moves.
7. **Watch the changelog** (https://api.samsoftpay.com/docs/changelog — raw markdown at
   /docs/changelog.md). Response shapes are additive-only and webhook events are only
   ever added, so reading it is routine maintenance.

## Test vs live

- `sk_test_` money lives on a **separate sandbox ledger**. It never touches, and can never move,
  real balances.
- Everything is mode-scoped by the key you use: charges, payouts, listings and `GET /v1/balance`
  all report only the mode of the calling key. Never reconcile real liabilities against a sandbox
  figure.

---

HTML docs: https://api.samsoftpay.com/docs · Index: https://api.samsoftpay.com/docs/llms.txt ·
Changelog: https://api.samsoftpay.com/docs/changelog (raw: /docs/changelog.md) ·
Status: https://api.samsoftpay.com/status

**Stability promise:** response shapes are additive-only: we add fields, we do not rename
or remove them. Webhook events are only ever added. Every API-visible change is announced
on the changelog.
