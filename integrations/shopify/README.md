# Samsoftpay for Shopify (Payments App — starter)

Offers **Samsoftpay** (MTN Mobile Money, Uganda) as an **offsite** payment method in Shopify
checkout, using Shopify's [Payments Apps API](https://shopify.dev/docs/apps/build/payments).
The money flow is identical to the WooCommerce plugin — Shopify redirects the buyer to
Samsoftpay's hosted checkout, and the signed `charge.succeeded` webhook marks the order paid —
but Shopify only allows third-party payment methods through **their app framework**, so this is a
small hosted Node app rather than a drop-in plugin.

```
Buyer picks Samsoftpay
      │
      ▼
Shopify ──POST /payments/session──▶  this app ──POST /v1/payment-links──▶ Samsoftpay
      ◀── redirect_url (hosted checkout) ──┘
      │
Buyer approves on phone (MTN MoMo) on api.samsoftpay.com/pay/<id>
      │
Samsoftpay ──POST /webhooks/samsoftpay (signed)──▶ this app
      │
      └── GraphQL paymentSessionResolve ──▶ Shopify marks the order PAID
```

## What's in here
| File | Purpose |
|---|---|
| `server.js` | Express app: OAuth install, payment session, refund session, Samsoftpay webhook |
| `lib/shopify.js` | HMAC verification + GraphQL `paymentSessionResolve/Reject`, `refundSessionResolve/Reject` |
| `lib/store.js` | File-backed store for shop tokens + session↔reference mapping (**swap for a DB in prod**) |
| `test/test_flow.js` | End-to-end test (session → webhook → resolve), mocked Shopify + Samsoftpay — `node test/test_flow.js` |

## Run locally
```bash
cp .env.example .env      # fill in the values
npm install
npm start                 # listens on :8080
node test/test_flow.js    # 9/9 should pass
```

## Going live (the Shopify-side steps this app can't do for you)
1. **Create a Payments App** in the [Shopify Partner Dashboard](https://partners.shopify.com) →
   Apps → *Create app* → **Payments app**. Uganda/UGX support and the exact offsite config are
   gated by Shopify's review — start this early.
2. Set the app URLs to point at your deployment:
   - **Payment session URL** → `https://<APP_URL>/payments/session`
   - **Refund session URL** → `https://<APP_URL>/payments/refund`
   - **App URL / redirect** → `https://<APP_URL>/install` and `/auth/callback`
3. Copy the app's **API key/secret** into `.env` (`SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`).
4. In the **Samsoftpay dashboard**, set your outbound **webhook URL** to
   `https://<APP_URL>/webhooks/samsoftpay` and copy the `whsec_` secret into `SAMSOFTPAY_WEBHOOK_SECRET`.
5. Deploy this app somewhere always-on (Render/Fly/railway). Install it on a dev store, place a
   test order with an `sk_test_` key, and confirm the order flips to **Paid**.
6. Submit the app for Shopify review; live merchants install it from the App Store.

## Honest limitations of this starter
- **Persistence** is a JSON file (`lib/store.js`). Use Postgres/Redis before production —
  shop tokens and the session↔charge mapping must survive restarts.
- Assumes **auto-capture** ("sale"). If you enable manual capture/void, implement those SPI
  endpoints (`paymentSessionResolve` after a separate capture, `paymentSessionReject` on void).
- Payment-session **field names** (`amount`, `currency`, `customer.phone_number`, `id`) follow the
  Payments Apps API; verify them against the current API version before shipping — they can change.
- OAuth here is minimal (no cookie-based `state` validation store). Harden it (nonce store,
  `state` check, per-shop reinstall) for production.

The Samsoftpay side is stable: `POST /v1/payment-links` → hosted checkout at `/pay/<id>` →
signed `charge.succeeded` / `charge.failed` webhook. Full contract:
[openapi.json](https://api.samsoftpay.com/openapi.json).
