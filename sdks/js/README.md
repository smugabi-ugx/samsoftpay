# Samsoftpay Node SDK

A thin, dependency-free wrapper over the [Samsoftpay API](https://api.samsoftpay.com/docs). Node 18+.

```bash
npm install samsoftpay   # or copy index.js into your project
```

```js
const Samsoftpay = require('samsoftpay');

const sp = new Samsoftpay(process.env.SAMSOFTPAY_SECRET_KEY, {
  webhookSecret: process.env.SAMSOFTPAY_WEBHOOK_SECRET, // for verifyWebhook()
});

// Collect money
const charge = await sp.charges.create({
  amount: 10000, currency: 'UGX', channel: 'mtn_momo',
  customer: { phone: '256700123456' }, reference: 'order-1',
});
// poll, or wait for the charge.succeeded webhook
const latest = await sp.charges.get(charge.id);

// Pay out
const payout = await sp.payouts.create({
  amount: 50000, channel: 'mtn_momo',
  recipient: { phone: '256780000001', name: 'Jane' }, reference: 'SAL-1',
});

// Reconcile
const balance = await sp.balance.get();
const statement = await sp.statements.get('2026-08');        // JSON
const pdf = sp.statements.pdfUrl('2026-08');                 // PDF download URL
```

The key prefix picks the mode: `sk_test_…` = sandbox, `sk_live_…` = live. Same base URL for both.
`X-Timestamp` and a per-request `Idempotency-Key` are added to money POSTs automatically (override with
`{ idempotencyKey }` as the 2nd arg).

## Verify webhooks

```js
// Express — use the RAW body
app.post('/webhooks/samsoftpay', express.raw({ type: 'application/json' }), (req, res) => {
  if (!sp.verifyWebhook(req.body, req.headers['x-samsoftpay-signature'])) {
    return res.status(400).send('invalid signature');
  }
  const evt = JSON.parse(req.body);
  // dedupe on evt.id, act on evt.event / evt.data
  res.json({ ok: true }); // 2xx stops retries
});
```

## Surface

`sp.charges.{create,get,list,refund}` · `sp.payouts.{create,get,list,bulk}` ·
`sp.scheduledPayouts.{create,get,list}` · `sp.balance.get` · `sp.statements.{get,pdfUrl}` ·
`sp.subaccounts.create` · `sp.paymentLinks.create` · `sp.resolveAccount(phone)` ·
`sp.verifyWebhook(rawBody, signature)`.

Errors throw `Samsoftpay.SamsoftpayError` with `.status` and `.body`.

Full contract: the machine-readable [OpenAPI spec](https://api.samsoftpay.com/openapi.json).
