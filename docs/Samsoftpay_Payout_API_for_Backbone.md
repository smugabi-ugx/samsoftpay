# SamsoftPay — Payout / Payroll API Contract (response to Backbone Payroll)

**For:** Backbone Payroll Development Team
**From:** SamsoftPay API Engineering
**Base URL:** `https://api.samsoftpay.com`  ·  **Date:** 27 Aug 2026

This confirms the authoritative contract for every item in your Integration
Requirements doc, and resolves the bulk-schema blocker (§30). Where your
expectation already matches our implementation, it's marked ✅.

---

## 0. The one fix from your report (§30) — DONE

`POST /v1/payouts/bulk` now accepts the **same** `recipient:{phone,name}` shape as
the single endpoint (it also still accepts a flat `phone`/`name` for CSV). The
`invalid item: 'phone'` error is resolved. Both of these are now valid per item:

```json
{ "amount": 50000, "recipient": { "phone": "256780000001", "name": "Jane" }, "reference": "BB-EMP-001" }
{ "amount": 50000, "phone": "256780000001", "name": "Jane", "reference": "BB-EMP-001" }
```

---

## 0b. Why a payout result had NO id (your "null payout id" issue) — IMPORTANT

A payout gets its `pout_…` id **only once it is actually created as a resource**.
SamsoftPay validates first and, if the request cannot become a payout, it is
rejected **before** any id or record exists. So there are two distinct outcomes —
please branch on them:

| Result | id | Meaning | What to do |
|---|---|---|---|
| **Rejected before creation** | `null` / absent, with an `error`/`failure_reason` | The payout was never created (bad schema, **insufficient wallet balance**, wrong key scope). No record exists on SamsoftPay — correctly. | Read the `error`; fix and retry. |
| **Created, then rail outcome** | `pout_…` present | The payout exists; it will reach `succeeded` or `failed` (e.g. `recipient_not_found`) — a `failed` payout STILL has its `pout_…` id and a `failure_reason`. | Store the id; track via webhook/poll. |

**The null ids you saw today were "rejected before creation."** The two causes
that match "withdrawals attempted today with no id and no record":

1. **The bulk schema bug** — every item hit `invalid item: 'phone'` (fixed in this
   change), so nothing was created. Re-test after this deploys.
2. **Empty sandbox wallet** — a payout needs `available` funds (amount + UGX 750
   fee). With a zero balance every payout is rejected `insufficient available
   balance` **before** an id is minted.

### Funding the sandbox wallet so payouts actually create + succeed
In sandbox you must have `available` balance before payroll can run. Get it by:
- A **sandbox charge / wallet top-up** (money IN with a `sk_test_` key) that settles
  to `available` (settlement is fast in sandbox), **or**
- Ask SamsoftPay to **credit your sandbox `available` balance** for testing.

Check it any time with `GET /v1/balance` (§10) — `available` must be ≥ `Σ amounts + Σ fees`.
Once funded, every accepted payout returns a `pout_…` id immediately.

---

## 1. Authentication (§3) ✅
```http
Authorization: Bearer <SECRET_KEY>
Content-Type: application/json
X-Timestamp: <UNIX_SECONDS>          # required on POST (replay guard, ±5 min)
Idempotency-Key: <UNIQUE_KEY>         # required on POST /payouts and /payouts/bulk
```
- **Sandbox vs live** are fully separate keys and ledgers: `sk_test_…` hits the
  sandbox (no real money, mock rail); `sk_live_…` hits production. Production
  credentials are **never** needed for sandbox testing.
- **Scopes:** a full `sk_…` key can move money out. A **collections-only** key
  (`sk_…_col_`) can create charges but is **rejected on payout endpoints with
  `403`** — use a full key for payroll.
- Missing/invalid auth → `401`; wrong scope → `403`; both JSON.

## 2. Create a payout (§4) ✅
```http
POST /v1/payouts
```
```json
{ "amount": 50000, "currency": "UGX", "channel": "mtn_momo",
  "recipient": { "phone": "256780000001", "name": "Jane Doe" },
  "reference": "BACKBONE-PAYROLL-001" }
```
`amount` is a whole number in UGX (no minor units for UGX). `channel` defaults to
`mtn_momo` (the only live disbursement rail today). Response `201`:
```json
{ "id": "pout_9f2a…", "mode": "live", "status": "authorized", "amount": 50000,
  "fee": 750, "currency": "UGX", "channel": "mtn_momo",
  "recipient_phone": "256780000001", "rail_reference": "…",
  "reference": "BACKBONE-PAYROLL-001", "failure_reason": null,
  "created_at": "…", "completed_at": null }
```

## 3. Provider IDs (§5) ✅
- The authoritative id is **`id` = `pout_…`**, returned **immediately** on create.
- It is **unique, immutable**, and **does not change** across
  `pending → authorized → processing → succeeded/failed`.
- It is included in **every webhook** (`data.id`) and is the key for
  `GET /v1/payouts/{id}`. Store it as your `samsoftPayoutId`. ✅

## 4. Business reference & idempotency (§6) ✅
- Your `reference` is **persisted**, returned in the payout response **and in
  webhook events** (`data.reference`), and is **searchable** on the list endpoint.
- **Idempotency:** send `Idempotency-Key` on create. A retry with the **same key
  (or same `reference` in bulk)** returns the **original** payout — it never pays
  twice. Same reference with *different* details is rejected (not silently
  replayed), so a reference-reuse bug can't mis-report success.

## 5. Bulk payouts (§7, §8, §19) ✅ (schema fixed)
```http
POST /v1/payouts/bulk           # Idempotency-Key required
```
```json
{ "payouts": [
  { "amount": 50000, "recipient": { "phone": "256780000001", "name": "Emp One" }, "reference": "BB-EMP-001" },
  { "amount": 75000, "recipient": { "phone": "256780000002", "name": "Emp Two" }, "reference": "BB-EMP-002" }
] }
```
- **Per-item, NOT atomic** — 95 can succeed while 5 fail. This is by design and is
  exactly your preferred behaviour (§19 Option A).
- **Max 1000 items** per call.
- Each result independently correlates by **reference + provider id + status +
  error**:
```json
{ "batch_id": "batch_…", "total": 2, "accepted": 1, "failed": 1, "results": [
  { "index": 0, "ok": true,  "id": "pout_…", "status": "authorized", "reference": "BB-EMP-001" },
  { "index": 1, "ok": false, "reference": "BB-EMP-002", "error": "insufficient available balance …" }
]}
```
- A per-item retry with the same reference returns `"replayed": true` (proof it
  wasn't paid twice).

## 6. Payout status lifecycle (§9) ✅
| Status | Meaning | Money earmarked | Final | Webhook | Poll? |
|---|---|---|---|---|---|
| `pending` | created, not yet sent to rail | yes (reserved) | no | — | yes |
| `authorized` | accepted by the MTN rail, in flight | yes | no | — | yes |
| `succeeded` | recipient paid | yes (settled) | **yes** | `payout.succeeded` | no |
| `failed` | rail rejected / failed | **refunded in full (incl. fee)** | **yes** | `payout.failed` | no |

- Terminal: `succeeded`, `failed`. Non-terminal: `pending`, `authorized`.
- An **ambiguous network error** parks the payout `authorized` (never a false
  `failed`) with `failure_reason: "ambiguous_network_error_pending_reconciliation"`;
  our reconciler resolves it from MTN's own answer. States never move backward.

## 7. Failure reasons (§10) ✅ machine-readable
`failure_reason` is a **stable code**, with a human message where relevant.
Codes you'll see: `recipient_not_found`, `wallet_locked`, `timeout`,
`insufficient_funds`, `user_cancelled`, `insufficient_balance` (merchant wallet),
`rail_rejected`, `ambiguous_network_error_pending_reconciliation`.

## 8. Webhooks (§11, §12, §13, §14) ✅
- **Configure** your URL on **Account → Webhooks**. Events for payroll:
  **`payout.succeeded`**, **`payout.failed`** (plus `charge.succeeded` etc.).
- **Envelope** (each delivery):
```json
{ "id": "evt_…",           // UNIQUE event id — stable across retries (dedupe on this)
  "event": "payout.succeeded",
  "data": { "id": "pout_…", "reference": "BB-EMP-001", "status": "succeeded",
            "amount": 50000, "fee": 750, "currency": "UGX",
            "recipient_phone": "256780000001", "rail_reference": "…",
            "failure_reason": null, "completed_at": "…" } }
```
- **Signature (§12):** header `X-Samsoftpay-Signature` = **HMAC-SHA256 of the raw
  body** using your `whsec_…` (Account → Webhooks — now always available, even
  before your first event). No timestamp in the signature; sign the raw bytes.
- **Event id (§13):** `evt_…` is **stable across retries** — dedupe on it.
- **Retries (§14):** up to **8 attempts** with exponential backoff over **~48h**;
  any **2xx** stops delivery; non-2xx/timeouts retry. Events may arrive
  **more than once** and **out of order** — treat `GET /v1/payouts/{id}` as the
  source of truth and dedupe on `evt_…`.

### Verify a webhook — Node
```js
const crypto = require("crypto");
app.post("/webhooks/samsoftpay", express.raw({type:"application/json"}), (req, res) => {
  const sig = req.headers["x-samsoftpay-signature"] || "";
  const expected = crypto.createHmac("sha256", process.env.SAMSOFTPAY_WEBHOOK_SECRET)
                         .update(req.body).digest("hex");
  if (sig.length !== expected.length ||
      !crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected)))
    return res.status(400).send("invalid signature");
  const evt = JSON.parse(req.body);
  // dedupe on evt.id, then act on evt.event / evt.data
  res.json({ ok: true });   // 2xx stops retries
});
```
### Verify a webhook — Python (Flask)
```python
import hmac, hashlib
from flask import request, abort

SECRET = "whsec_…"  # your webhook signing secret

@app.post("/webhooks/samsoftpay")
def hook():
    sig = request.headers.get("X-Samsoftpay-Signature", "")
    expected = hmac.new(SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(400, "invalid signature")
    evt = request.get_json()
    # dedupe on evt["id"]; act on evt["event"] / evt["data"]
    return {"ok": True}   # 2xx stops retries
```

## 9. Retrieve & list payouts (§15, §16) ✅
- **`GET /v1/payouts/{id}`** → current authoritative state (your polling/
  reconciliation fallback).
- **`GET /v1/payouts`** → list; query params: `status`, `created_after`,
  `created_before` (ISO 8601), cursor pagination. Mode-scoped by your key.

## 10. Balance (§17) ✅
`GET /v1/balance` →
```json
{ "balances": [ { "currency": "UGX", "available": 5000000, "pending": 1000000,
                  "total": 6000000, "cached": 6000000 } ], "consistent": true }
```
Use **`available`** to decide if payroll can run. **`total_required = Σ amounts +
Σ fees`** (see §11). `consistent:false` means our cached figure disagrees with the
journal — treat as a signal to reconcile, not to pay.

## 11. Fees (§18) ✅
- Flat **UGX 750** per payout on MTN MoMo today. No per-channel/per-account
  variation currently (MTN is the only live payout rail).
- **Bulk = charged per item.** A **failed payout refunds the fee in full** (amount
  + fee returned to `available`). Fees ARE deducted from `available` on payout.

## 12. Recipient resolution (§22) ✅
`GET /v1/resolve-account?phone=<MSISDN>` → `{ "msisdn", "active", "registered_name" }`.
Use it as a **pre-payout validation** step to cut avoidable failures.

## 13. Sandbox test numbers (§20, §21) ✅ — match your spec exactly
Matched on the recipient phone's **last 9 digits**:
| Phone ends in | Payout outcome |
|---|---|
| `…700000000` / any normal test number | **succeeds** |
| `…700000001` | `recipient_not_found` |
| `…700000002` | `wallet_locked` |
| `…700000003` | `timeout` |
Insufficient-balance and invalid-request are reproduced by amount/shape, not a
magic number. Sandbox never touches real money or the real MTN network.

## 14. Errors & HTTP codes (§23, §24) 
- Money-out on money endpoints: `201` created · `200` retrieval · `400` invalid ·
  `401` auth · `403` scope · `404` not found · `409` idempotency-in-flight ·
  `429` rate limit · `502/503` temporary rail unavailability.
- Error bodies are JSON with a message; a request id is emitted for tracing.
  (If you need the strict `{error, message, request_id}` envelope on every error,
  tell us — we'll standardise the error handler.)

## 15. Identifiers (§25, §26) ✅
`reference` (yours) · `pout_…` (payout resource) · `evt_…` (webhook event) are all
distinct and stable — exactly your recommended model.

## 16. Production readiness (§28) — on request
Production base URL is the same host; you'll receive live keys + webhook secret,
confirmed payout permissions, limits (max 1000/bulk; rate limits per plan), and
the settlement/reconciliation process at go-live. MTN production onboarding is in
progress; sandbox certification can complete now.

---

### Acceptance criteria (§33) — status
Authenticate ✅ · balance ✅ · resolve recipient ✅ · single payout ✅ · stable
`pout_` id ✅ · retrieve by id ✅ · bulk payout ✅ (fixed) · per-item results ✅ ·
per-item ids ✅ · failed-item codes ✅ · payout webhooks ✅ · verify signature ✅ ·
dedupe on `evt_` ✅ · idempotency ✅ · insufficient-balance ✅ · timeout ✅ ·
recipient-not-found ✅. Ready for sandbox certification.
