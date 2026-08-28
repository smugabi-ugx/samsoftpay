# SamsoftPay — answers to Backbone's developer questions

Re: `SAMSOFTPAY_OPEN_QUESTIONS.md` (2026-08-28). Every answer below was verified
against the SamsoftPay source, not from memory. Where we changed something in
response, it's marked **[fixed]** and ships in the next deploy.

> **Read this first — the live sandbox is one build behind.** Several things you
> observed (notably the "flat 750" fee) are the behaviour of an **older deployed
> build**, not our current code. The current code and the fixes below deploy
> together; **re-test after we confirm the deploy has landed.**

---

## 1. Webhook delivery

Your diagnosis is correct: the registered URL `https://api.samsoftpay.com/webhooks/samsoftpay`
is **our** host, so we were POSTing to ourselves — every attempt 404s. Point it at
**your** public receiver and the 12 will clear.

### Q1a — Are exhausted deliveries still resendable? Retention?
**Yes.** `POST /v1/webhooks/<event_id>/resend` explicitly **revives an exhausted
delivery**: it resets the row to `pending`, sets `next_attempt_at = now`, and if
`attempts >= 8` it zeroes `attempts` so the retry cap doesn't ignore it. So all 12
are resendable even after the schedule is spent.
**Retention:** the pruner only deletes deliveries in status **`sent`** older than
**30 days**. Your `pending`/`failed` rows are **not** pruned — they stay resendable.
(Don't wait indefinitely, but there's no 48h cliff on the row itself.)

### Q1b — Does changing the Account webhook URL re-target pending deliveries?
**Partly — and this is the important nuance:**
- The destination URL is **snapshotted on each delivery at enqueue time**. So the
  **automatic** retries of the 12 pending rows keep hitting the **old** URL.
- **A manual `resend` re-reads your current Account URL** and retargets that row to it.

**So: fix the URL first, then `resend` each of the 12.** The resend path picks up the
new URL; relying on the automatic retries alone would keep hitting the old (self)
address until they exhaust.

### Q1c — Should a self-addressed webhook URL be rejected at save time? **[fixed]**
Agreed, and done. Saving a webhook URL whose host is a SamsoftPay domain
(`*.samsoftpay.com`, `*.samsoftpay.onrender.com`, or the configured API host) is now
**rejected at save time** on both signup and Account → Webhooks, with the message:
*"That points at SamsoftPay's own API host — your webhook URL must be YOUR server's
public https:// endpoint…"*. Ships next deploy. You won't be the last to paste that in;
now nobody can.

### Q1d — Separate URLs for test and live?
**Not today.** `webhook_url` is a **single per-merchant field**; there is no per-mode
URL. Sandbox and live events both POST to the same endpoint. Disambiguate with the
**`mode` field inside the payload** (`"mode": "test"` vs `"live"`) — it's on every
envelope. We've logged "per-mode webhook URLs" as an enhancement; for go-live, gate on
`data`/`mode` server-side so a sandbox event can't be actioned by your production path.

### Q1e — Signature confirmation. **Confirmed exactly as you have it.**
`X-Samsoftpay-Signature` = **HMAC-SHA256, lowercase hex, over the raw response body**,
keyed with your `whsec_…`. **No `sha256=` prefix. No timestamp in the signed string**
(there *is* a `timestamp` field *inside* the signed JSON body, but it is not a separate
signing component — you sign the exact bytes you received). Verify by HMAC-ing the raw
body and comparing to the header. Your implementation is correct.

---

## 2. Fees — it is **1.5%, not a flat 750**

Our code charges, for a payout: **`min(max(200, floor(amount × 1.5%)), 5000)`** — i.e.
**1.5%, floor UGX 200, cap UGX 5,000** — the **same** as collections, no flat fee. A
failed payout refunds **amount + fee**.

| Payout amount | Fee |
|---|---|
| 1,000 | 200 (floor) |
| 50,000 | **750** (= 1.5%) |
| 300,000 | 4,500 |
| ≥ 333,334 | 5,000 (cap) |

**Why you saw "flat 750" on every amount:** the sandbox you tested is running an
**older build** that still had the flat-750 payout fee. Under the current code a 1,000
payout costs 200 and a 300,000 payout costs 4,500 — not 750. The "750" in our doc
examples is a **coincidence of the example amount** (1.5% × 50,000 = 750), which we've
now annotated. Our live docs and OpenAPI already state 1.5%; the offline payout doc that
said "flat 750" has been corrected (its PDF export will be regenerated from source).

**Commercial answer, in writing:** the payout fee is **1.5% (min 200, cap 5,000)**, same
as collections. There is **no separate flat rate**. Because of the **5,000 cap**, a
150-line payroll of typical salaries is bounded at **≤ UGX 750,000/run** (every line at
or above ~333k hits the cap). If you want a negotiated volume rate for payroll at your
scale, raise it with the SamsoftPay commercial contact — the platform supports a
per-merchant rate override, so a bespoke rate is a config change, not a code change.

**VAT — the exact position, for your client's board:**
> For salary payouts, SamsoftPay deducts only the amount plus a **1.5% service fee**
> (minimum UGX 200, capped at UGX 5,000 per payment) — nothing else. Salary
> disbursements are **not a VATable supply**, so no VAT is added to or withheld from a
> payout. Our **1.5% service fee is quoted VAT-inclusive**: SamsoftPay is VAT-registered
> (**TIN 1031035883**) and accounts for the VAT within that fee to URA. The **18% VAT**
> you may otherwise see in the platform applies only to a **merchant's own sales of
> goods/services (money-in)**, where it is the VAT already included in the price, shown
> on the customer's receipt and **remitted by that merchant** — it is **not** part of,
> nor added on top of, our payout fee.

(So your earlier reading was right that the ledger deducts only `amount + fee` with no
separate tax line — but the reason is that a *payout* carries no VAT, and our fee is
VAT-inclusive; VAT is not a component we bolt onto the 1.5%.)

---

## 3. Small API questions

### Q3a — `GET /v1/payment-links/<id>`: supported and stable? **Yes.**
It's a supported endpoint, mode-scoped (a test key only sees test links, else 404). It
always returns the `transaction_status` **key**; the value is `null` until the link is
paid, then the charge status (`"succeeded"`, …). Safe to depend on.

### Q3b — No way to list payment links (405). **[fixed] — now `GET /v1/payment-links`.**
Added. It lists your links **newest-first**, **mode-scoped**, with the **same cursor
pagination as `GET /v1/charges`**: `?limit=` (1–100, default 20), `?starting_after=<link id>`,
plus `?reference=`, `?created_after=`, `?created_before=`. Envelope:
`{ "object": "list", "data": [...], "has_more": bool, "next_cursor": <link id|null> }`.
Each row carries the same fields as `GET /<id>` (including `transaction_status`). Ships
next deploy — you no longer need to fear an unrecorded-but-payable link.

### Q3c — `Idempotent-Replayed` on bulk. **[fixed] + clarified.**
Two layers:
- **Per item (authoritative):** each replayed item in the `results` array carries
  `"replayed": true` in the body. Use this — a bulk call can be *partially* replayed
  (some items new, some already paid), so per-item is the truthful signal.
- **Batch header [new]:** when **every** item in the call is a replay (the whole retry
  disbursed **nothing** new), the response now sets **`Idempotent-Replayed: true`**. A
  mixed batch does **not** set it. So: header present ⇒ nothing was paid this call;
  header absent ⇒ check each item's `replayed`.

### Q3d — Bulk partial failure: per-item `reference`? **[fixed] — always present now.**
Every result item carries both **`index`** and **`reference`**. Previously a *malformed*
row with no reference of its own returned `reference: null` (you'd fall back to index);
now such a row echoes a synthesized `"<your-Idempotency-Key>-<index>"`, so **`reference`
is non-null on every item**. Match on `reference`; `index` remains as a backstop.

---

## 4. Go-live

- **Documents** (director IDs, company resolution, certified certificate, Forms 18 & 20):
  submit to the SamsoftPay onboarding contact; we'll confirm the channel and turnaround
  with you directly.
- **Keys:** live access is a **separate `sk_live_…` key**, not a rotation of your
  `sk_test_…`. Your sandbox key keeps working for the sandbox ledger; the live key posts
  to the live ledger. Same base URL for both — the prefix picks the mode.
- **Egress IPs** for your firewall (unchanged): `74.220.48.0/24`, `74.220.56.0/24`.

---

## What we changed for you (all in the next deploy)
1. **[Q1c]** Self-addressed webhook URLs rejected at save time (signup + Account).
2. **[Q3b]** `GET /v1/payment-links` list endpoint (cursor pagination, mode-scoped).
3. **[Q3c]** Batch-level `Idempotent-Replayed: true` header when a bulk call is a pure replay.
4. **[Q3d]** Every bulk result item now carries a non-null `reference`.
5. **[Q2]** Corrected the offline payout doc to state 1.5% (min 200, cap 5,000), not flat 750.

All five are covered by an automated test (`tests/test_backbone_integration_fixes.py`, 8/8).
