# Samsoftpay integrations & SDKs

Ready-to-use ways to drop Samsoftpay into a store or a codebase. Everything talks to
`https://api.samsoftpay.com` (same URL for test and live; the `sk_` key prefix picks the mode).

## What's here

| Path | What it is | Status |
|---|---|---|
| `integrations/woocommerce/` | **WooCommerce payment gateway** (WordPress plugin, PHP) | ✅ ready to install |
| `integrations/shopify/` | **Shopify Payments App** (Node/Express, offsite method) | ✅ starter built + tested — needs Shopify Partner approval to list |
| `sdks/js/` | **Node SDK** (`samsoftpay`) | ✅ ready |
| `sdks/python/` | **Python SDK** (`samsoftpay`) | ✅ ready |

### WooCommerce (WordPress)
Copy `integrations/woocommerce/samsoftpay-for-woocommerce/` into `wp-content/plugins/`, activate,
then WooCommerce → Settings → Payments → **Samsoftpay**: paste your `sk_test_`/`sk_live_` keys and
`whsec_` webhook secret, and set your Samsoftpay webhook URL to `https://your-store/?wc-api=samsoftpay`.
Customers pay on Samsoftpay's hosted checkout (no card fields on your site, no PCI scope); the order is
confirmed by the signed `charge.succeeded` webhook, with a return-poll fallback.

### SDKs
- **Node:** `sdks/js` — `new Samsoftpay(secret, { webhookSecret })`, then `sp.charges.create(...)`, `sp.payouts.create(...)`, `sp.verifyWebhook(...)`. Node 18+, zero deps.
- **Python:** `sdks/python` — `Samsoftpay(secret, webhook_secret=...)`, same surface. Depends on `requests`.

Both cover charges, payouts (single/bulk/scheduled), balance, statements, subaccounts, payment links,
account resolution, and webhook signature verification. The full contract is the machine-readable
[OpenAPI spec](https://api.samsoftpay.com/openapi.json) — point any codegen tool at it for other languages.

---

## Shopify & Wix — possible, but gated by their programs (not just a plugin)

These platforms don't let a third party add a payment method with a self-hosted plugin the way
WooCommerce does — you integrate through **their** payment frameworks, which require onboarding/approval:

- **Shopify:** the [Payments Apps API](https://shopify.dev/docs/apps/build/payments) — you build a
  Shopify **app** that offers Samsoftpay as an offsite payment method. **A working starter is now in
  `integrations/shopify/`** (Node/Express: payment session → hosted checkout → `paymentSessionResolve`
  on our webhook; 9/9 end-to-end tests pass). Deploy it, wire the Partner Dashboard URLs, and submit
  for Shopify review — merchants then install it from the App Store. Shopify must approve the app
  before it can take live money; the code and the flow are done.
- **Wix:** the [Payment Provider SPI](https://dev.wix.com/docs/rest/business-solutions/payments) — you
  host a small service Wix calls to create/capture/refund a payment, which forwards to our API. Also
  requires registering as a Wix payment provider.

So the *integration logic* is the same hosted-checkout + webhook flow the WooCommerce plugin uses;
what differs is the platform wrapper and the approval step. Say the word and the Shopify app / Wix SPI
service are the next builds.

## "Forex" and "Binance" — a different category (rails, not storefronts)

These aren't shopping-cart plugins; they'd be **payment rails or settlement options**, and they carry
real regulatory weight in Uganda:

- **Binance:** the useful thing is **Binance Pay** (Binance's merchant payment API) as an *additional
  collection rail* — a customer pays in crypto/Binance balance, we record the charge. This fits the
  existing rail-adapter pattern (like the ChangeNow crypto rail): a new adapter behind the same
  `RailAdapter` interface, no core change. Accepting/settling crypto in Uganda touches BOU/AML rules —
  scope it deliberately, not as a quick plugin.
- **Forex / FX:** converting UGX ↔ USD/other, or accepting foreign-currency settlement. That's an **FX
  capability** (a conversion + a multi-currency ledger — our ledger is already per-currency), typically
  via a bank or an FX provider, and is licence-sensitive. It's a rail/treasury feature, not a store
  connector.

**Bottom line:** WooCommerce + the SDKs are drop-in today. Shopify/Wix are the same flow behind their
approval programs. Binance Pay is a *new rail* (adapter pattern, doable) and forex is an *FX/treasury*
capability — both are real roadmap items with a compliance dimension, not storefront plugins.
