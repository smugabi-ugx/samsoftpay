# Samsoftpay API — Integration Guide

> Samsoftpay is a payment gateway for Uganda: MTN Mobile Money collections and disbursements,
> hosted checkout, payment links, vending-machine payments, subaccount splits and signed webhooks.
> This is the complete, agent-friendly markdown version of https://api.samsoftpay.com/docs.
> Machine-readable index: https://api.samsoftpay.com/docs/llms.txt

## Quickstart

You need exactly **two things**:

1. **Base URL:** `https://api.samsoftpay.com` — same for test and live.
2. **An API key**, sent as `Authorization: Bearer <key>`:
   - `sk_test_…` — sandbox. Sandbox money lives on a separate test ledger and never touches real balances.
   - `sk_live_…` — live money.
   - `sk_test_col_…` / `sk_live_col_…` — **collections-only** keys for code that ships on a public
     device (kiosk, vending machine). They can create charges, vending orders and payment links,
     but are refused with `403` on payouts, bulk payouts and refunds — a key pulled off a device
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

Fees: charges 1.5% (min UGX 200, cap UGX 5,000), returned in the `fee` field — you receive
`amount - fee`. Payouts: flat UGX 750, fully refunded with the amount if the payout fails.
All amounts are integers in minor units (whole UGX).

### Settlement timing (set your watch by it)

A succeeded charge becomes withdrawable **24 hours after it completes**; the settlement sweep
runs **hourly**, so the release lands at most an hour after the hold expires. Every charge
response carries `available_on` (the actual settlement time once released, else the estimate)
and `settled` (boolean). Each release is a first-class record on `GET /v1/settlements` and the
dashboard's Settlements table. **Escalation threshold:** if a settlement is more than 2 hours
late, contact support with the charge id — you should never have to discover a delay yourself.

## Webhooks — the operations contract

Configure `webhook_url` on your Account page. Samsoftpay POSTs a signed JSON envelope on every
state change.

### Events

`charge.succeeded`, `charge.failed`, `payout.succeeded`, `payout.failed`,
`vending.dispensed`, `vending.dispense_failed`.

**`charge.succeeded` means the MONEY ARRIVED — nothing else.** For vending, it does not mean the
product came out: wait for `vending.dispensed` before treating the product as delivered.

### Envelope

Sent as **canonical JSON with no spaces** (the signature covers these exact bytes):

```json
{"id":"evt_1a2b3c4d5e6f7a8b9c0d1e2f","timestamp":1755900000,"event":"charge.succeeded","data":{...}}
```

- `id` — unique per event; **retries of the same event share the same id — dedupe on it.**
- `timestamp` — Unix seconds when the event was created; use it to enforce a replay window.

### Signature

Header `X-Samsoftpay-Signature` is the **HMAC-SHA256 hex digest of the RAW request body**, keyed
with your `whsec_…` secret from **Account → Webhooks**. The secret is per-merchant — it is never
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

- Respond with any **2xx within 5 seconds**. Return 200 immediately and process asynchronously —
  a slow handler is indistinguishable from a dead one and will be retried.
- The first attempt fires immediately. On failure we back off:
  **1m, 5m, 30m, 2h, 6h, 12h, 24h, 48h** — up to **8 attempts** in total, so a delivery keeps
  retrying for roughly two days after the event.
- Retries carry the **same envelope `id`** — dedupe on it, not on receipt count.
- Missed everything? `GET /v1/webhooks` shows recent deliveries and
  `POST /v1/webhooks/<event_id>/resend` re-queues one; `GET /v1/charges` lets you reconcile by
  listing.

### Egress IPs

Webhook deliveries originate from these static ranges — allowlist them if your endpoint is
firewalled:

- `74.220.48.0/24`
- `74.220.56.0/24`

## Testing — deterministic sandbox

With an `sk_test_` key no real network is touched; a charge settles a few seconds after creation.
**Every ordinary phone number succeeds, every time** — failure is something you ask for with a
magic number, matched on the **last 9 digits** (so `0700000001`, `256700000001` and
`256 700 000 001` all trigger the same outcome):

| Phone number | Outcome | `failure_reason` |
|---|---|---|
| `256700000000` | Always succeeds (documented success number) | — |
| `256700000001` | Fails | `insufficient_funds` |
| `256700000002` | Fails | `user_cancelled` |
| `256700000003` | Fails | `timeout` |

### Payout failure simulation

Payouts (`sk_test_` keys) have their own deterministic scenario set, matched on the
**recipient** phone's last 9 digits — rehearse your `payout.failed` handling before it
happens with real money:

| Recipient number | Outcome | `failure_reason` |
|---|---|---|
| `256700000000` | Always succeeds | — |
| `256700000001` | Fails | `recipient_not_found` |
| `256700000002` | Fails | `wallet_locked` |
| `256700000003` | Fails | `timeout` |

A failed payout refunds the amount **and** the fee to your available balance, and fires the
`payout.failed` webhook.

### Exact `charge.failed` payloads

Each failure number produces a `charge.failed` webhook whose `data` is exactly this shape
(same shape as `GET /v1/charges/<id>`; `completed_at` is ISO 8601):

`256700000001`:

```json
{"id":"txn_9f2c41d8a3b7e615","amount":5000,"fee":200,"currency":"UGX","channel":"mtn_momo","status":"failed","merchant_reference":"order-001","failure_reason":"insufficient_funds","completed_at":"2026-08-20T10:30:00+00:00"}
```

`256700000002`:

```json
{"id":"txn_9f2c41d8a3b7e615","amount":5000,"fee":200,"currency":"UGX","channel":"mtn_momo","status":"failed","merchant_reference":"order-001","failure_reason":"user_cancelled","completed_at":"2026-08-20T10:30:00+00:00"}
```

`256700000003`:

```json
{"id":"txn_9f2c41d8a3b7e615","amount":5000,"fee":200,"currency":"UGX","channel":"mtn_momo","status":"failed","merchant_reference":"order-001","failure_reason":"timeout","completed_at":"2026-08-20T10:30:00+00:00"}
```

A successful charge's `charge.succeeded` data is identical in shape with
`"status":"succeeded"` and `"failure_reason":null`.

## Idempotency

- `Idempotency-Key` is **required** on the money POSTs (`/v1/charges`, `/v1/charges/<id>/refund`,
  `/v1/payouts`, `/v1/payouts/bulk`) and **optional but honored** on `/v1/payment-links` and
  `/v1/vending/orders`.
- Keys are **reserved before execution**: two concurrent requests with the same key can never
  both run — the loser gets `409` ("still in flight") and should retry shortly.
- A replayed request returns the **original response**, unchanged, plus the header
  **`Idempotent-Replayed: true`** so you can tell a replay from a fresh execution.
- `409` means either: same key with a **different request body**, or the original request is
  **still in flight**.
- Discipline:
  - Generate keys with `uuid.uuid4()` / `crypto.randomUUID()` — one key per logical operation.
  - On a network error, timeout or 5xx, **retry with the SAME key** — that is the whole point.
  - Keys are retained for **30 days**; do not reuse a key for a different operation, ever.

## Integration best practices

- **Give value only on a definite outcome.** `pending` / `authorized` is NOT failed — an
  ambiguous network answer parks a charge, it does not fail it. Never release goods, credit a
  wallet or mark an order paid until `status` is `succeeded`.
- **Confirm before fulfilling**: via the `charge.succeeded` webhook or `GET /v1/charges/<id>` —
  never from your own client-side signal alone.
- **Verify `amount`, `currency` and your `reference`** on the confirmed charge match the order
  you are about to fulfil, before delivering value.
- **Prefer webhooks over polling**; poll `GET /v1/charges/<id>` as a fallback and reconcile with
  `GET /v1/charges` and `GET /v1/balance`.
- **Dedupe on the webhook envelope `id`** — retries and resends share it.
- **Never embed a full `sk_` key in a kiosk, mobile app or on-device code.** Use a
  collections-only `sk_*_col_` key there — it cannot call payouts or refunds.
- **Never log full API keys** or webhook secrets; store them in environment variables.
- **TLS only** — the API is HTTPS-only; your webhook endpoint should be too.
- For vending: money and delivery are separate facts. `charge.succeeded` = paid;
  `vending.dispensed` = the machine actually released the product.

## Errors

All `/v1` errors are JSON: `{"error": "..."}`.

| Status | Meaning | What to do |
|---|---|---|
| `400` | Bad request — missing/invalid field, stale `X-Timestamp`, or `mode_mismatch` (e.g. refunding a test charge with a live key — use the key of the matching mode). | Fix the request; do not blind-retry. |
| `400` | Unsupported channel / simulated rail refused in production — `airtel_money` and `card` have no live rail yet and are rejected on live keys before any write. | Use `mtn_momo` live, or a test key. |
| `401` | Missing or invalid Bearer token. | Check the key. |
| `403` | Key lacks permission — a collections-only key on a payout/refund endpoint. | Use a full-scope key from a secure server. |
| `404` | Resource not found or belongs to a different merchant/mode. | Check the id and key mode. |
| `409` | Idempotency conflict: same key with a different body, or the original request is still in flight. | New key for new work; same key retried shortly for in-flight. |
| `429` | Rate limit exceeded (charges 30/min, payouts 10/min). | Back off and retry; honour the `Retry-After` header when present. |
| `5xx` / network error | Outcome unknown. | Retry with the **same** `Idempotency-Key`. |

## Test vs live

- `sk_test_` money lives on a **separate sandbox ledger** — it never touches, and can never move,
  real balances.
- Everything is mode-scoped by the key you use: charges, payouts, listings and `GET /v1/balance`
  all report only the mode of the calling key. Never reconcile real liabilities against a sandbox
  figure.

---

HTML docs: https://api.samsoftpay.com/docs · Index: https://api.samsoftpay.com/docs/llms.txt ·
Status: https://api.samsoftpay.com/status
