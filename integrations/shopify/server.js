'use strict';
/*
 * Samsoftpay — Shopify Payments App (starter)
 * ------------------------------------------------------------------
 * Offers Samsoftpay (MTN Mobile Money, Uganda) as an OFFSITE payment method in
 * Shopify checkout, using Shopify's Payments Apps API. The money flow is the
 * SAME hosted-checkout + webhook flow the WooCommerce plugin uses:
 *
 *   1. Buyer picks "Samsoftpay" at checkout.
 *   2. Shopify POSTs a payment session to  POST /payments/session  (this app).
 *   3. We create a Samsoftpay payment link and return its hosted-checkout URL as
 *      `redirect_url`; Shopify sends the buyer there to approve on their phone.
 *   4. Samsoftpay's signed `charge.succeeded` webhook hits POST /webhooks/samsoftpay;
 *      we resolve the matching Shopify PAYMENT session (GraphQL) -> order is paid.
 *   5. Refunds: Shopify POSTs POST /payments/refund; we call Samsoftpay refund and
 *      resolve the REFUND session.
 *
 * WHAT THIS STARTER DOES NOT DO (see README): the Shopify Partner Dashboard setup,
 * App Store review, production-grade persistence (swap lib/store.js for a DB),
 * and capture/void SPIs (this assumes auto-capture "sale"). Field names on the
 * session payload can change between API versions — verify against the current
 * Payments Apps API docs before going live.
 */

const express = require('express');
const crypto = require('crypto');
const shopify = require('./lib/shopify');
const store = require('./lib/store');

const {
  PORT = 8080,
  APP_URL,                                   // this app's public https URL
  SHOPIFY_API_KEY,
  SHOPIFY_API_SECRET,
  SHOPIFY_SCOPES = 'read_payment_gateways,read_payment_sessions',
  SAMSOFTPAY_BASE_URL = 'https://api.samsoftpay.com',
  SAMSOFTPAY_SECRET_KEY,                     // sk_test_ / sk_live_
  SAMSOFTPAY_WEBHOOK_SECRET,                 // whsec_ (per-merchant outbound secret)
} = process.env;

const app = express();

// ---- Samsoftpay API client (tiny, inline so this app is self-contained) ----
async function sp(method, path, body) {
  const headers = { Authorization: `Bearer ${SAMSOFTPAY_SECRET_KEY}` };
  if (body) {
    headers['Content-Type'] = 'application/json';
    headers['X-Timestamp'] = String(Math.floor(Date.now() / 1000));
    headers['Idempotency-Key'] = crypto.randomUUID();
  }
  const res = await fetch(SAMSOFTPAY_BASE_URL + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error(json.error || `Samsoftpay HTTP ${res.status}`);
    e.body = json;
    throw e;
  }
  return json;
}

// =====================================================================
// 1) OAuth install  (minimal — get a per-shop Admin API access token)
// =====================================================================
app.get('/install', (req, res) => {
  const shop = String(req.query.shop || '');
  if (!/^[a-zA-Z0-9-]+\.myshopify\.com$/.test(shop)) return res.status(400).send('bad shop');
  const state = crypto.randomBytes(16).toString('hex');
  const redirect = `${APP_URL}/auth/callback`;
  const url =
    `https://${shop}/admin/oauth/authorize?client_id=${SHOPIFY_API_KEY}` +
    `&scope=${encodeURIComponent(SHOPIFY_SCOPES)}` +
    `&redirect_uri=${encodeURIComponent(redirect)}&state=${state}`;
  res.cookie ? res.cookie('sp_state', state) : null;
  res.redirect(url);
});

app.get('/auth/callback', async (req, res) => {
  try {
    const { shop, code } = req.query;
    if (!shopify.verifyOauthHmac(req.query, SHOPIFY_API_SECRET)) {
      return res.status(401).send('bad hmac');
    }
    const tokenRes = await fetch(`https://${shop}/admin/oauth/access_token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: SHOPIFY_API_KEY,
        client_secret: SHOPIFY_API_SECRET,
        code,
      }),
    });
    const { access_token } = await tokenRes.json();
    if (!access_token) return res.status(502).send('no token');
    store.saveShopToken(String(shop), access_token);
    res.send('Samsoftpay installed. You can enable it in Settings → Payments.');
  } catch (e) {
    res.status(500).send('install failed');
  }
});

// =====================================================================
// 2) Payment session  (Shopify -> us).  Raw body so we can verify the HMAC.
// =====================================================================
app.post('/payments/session', express.raw({ type: '*/*' }), async (req, res) => {
  const raw = req.body; // Buffer
  const sig = req.get('X-Shopify-Hmac-Sha256');
  if (!shopify.verifyWebhookHmac(raw, sig, SHOPIFY_API_SECRET)) {
    return res.status(401).json({ error: 'bad hmac' });
  }
  let s;
  try {
    s = JSON.parse(raw.toString('utf8'));
  } catch {
    return res.status(400).json({ error: 'bad json' });
  }
  const shop = req.get('X-Shopify-Shop-Domain');
  // Shopify amount is a decimal string in major units; Samsoftpay wants minor
  // units (UGX has no decimals, so this is effectively integer shillings).
  const amountMinor = Math.round(parseFloat(s.amount) * (zeroDecimal(s.currency) ? 1 : 100));
  // A stable, unique reference the webhook will echo back to correlate the session.
  const reference = `shpfy_${shortId(s.id)}`;

  try {
    const link = await sp('POST', '/v1/payment-links', {
      amount: amountMinor,
      currency: s.currency,
      channel: 'mtn_momo',
      reference,
      description: `Shopify order ${s.group || s.id}`,
      customer: s.customer ? { phone: (s.customer.phone_number || '').replace(/\D/g, '') } : undefined,
    });
    store.linkReference(reference, {
      shop,
      sessionGid: s.id,           // GID to resolve later
      kind: 'payment',
      amount: s.amount,
      currency: s.currency,
      test: !!s.test,
    });
    // Shopify redirects the buyer to this URL to complete payment offsite.
    return res.status(201).json({ redirect_url: `${SAMSOFTPAY_BASE_URL}/pay/${encodeURIComponent(link.id)}` });
  } catch (e) {
    // Ask Shopify to retry (it will re-POST the session).
    return res.status(500).json({ error: 'could not start payment' });
  }
});

// =====================================================================
// 3) Refund session  (Shopify -> us).
// =====================================================================
app.post('/payments/refund', express.raw({ type: '*/*' }), async (req, res) => {
  const raw = req.body;
  if (!shopify.verifyWebhookHmac(raw, req.get('X-Shopify-Hmac-Sha256'), SHOPIFY_API_SECRET)) {
    return res.status(401).json({ error: 'bad hmac' });
  }
  let r;
  try {
    r = JSON.parse(raw.toString('utf8'));
  } catch {
    return res.status(400).json({ error: 'bad json' });
  }
  const shop = req.get('X-Shopify-Shop-Domain');
  const token = store.getShopToken(shop);
  try {
    // r.payment_id is the id we returned as the resolved payment (our charge id).
    await sp('POST', `/v1/charges/${encodeURIComponent(r.payment_id)}/refund`, {});
    if (token) await shopify.resolveRefund(shop, token, r.id);
    return res.status(201).json({ ok: true });
  } catch (e) {
    if (token) {
      try { await shopify.rejectRefund(shop, token, r.id, 'PROCESSING_ERROR', 'Refund could not be processed'); } catch {}
    }
    return res.status(500).json({ error: 'refund failed' });
  }
});

// =====================================================================
// 4) Samsoftpay webhook (async source of truth) -> resolve the Shopify session.
// =====================================================================
app.post('/webhooks/samsoftpay', express.raw({ type: '*/*' }), async (req, res) => {
  const raw = req.body;
  const expected = crypto.createHmac('sha256', SAMSOFTPAY_WEBHOOK_SECRET).update(raw).digest('hex');
  const got = req.get('X-Samsoftpay-Signature') || '';
  if (!timingSafeHex(expected, got)) return res.status(400).send('invalid signature');

  let evt;
  try {
    evt = JSON.parse(raw.toString('utf8'));
  } catch {
    return res.status(400).send('bad json');
  }
  const data = evt.data || {};
  const reference = data.reference || data.merchant_reference || '';
  const map = store.findByReference(reference);
  if (!map || map.resolvedAt) return res.status(200).json({ ok: true }); // unknown/dup -> ack

  const token = store.getShopToken(map.shop);
  try {
    if (evt.event === 'charge.succeeded' && token) {
      await shopify.resolvePayment(map.shop, token, map.sessionGid);
      store.markResolved(reference);
    } else if (evt.event === 'charge.failed' && token) {
      await shopify.rejectPayment(map.shop, token, map.sessionGid, 'PROCESSING_ERROR', data.failure_reason || 'Payment failed');
      store.markResolved(reference);
    }
  } catch (e) {
    // Leave unresolved; Samsoftpay retries the webhook (48h backoff) and Shopify
    // re-polls the session, so a transient GraphQL error self-heals.
    return res.status(500).json({ error: 'resolve failed' });
  }
  return res.status(200).json({ ok: true }); // 2xx stops retries
});

app.get('/healthz', (_req, res) => res.json({ status: 'ok' }));

// ---- helpers ----
function zeroDecimal(currency) {
  return ['UGX', 'RWF', 'KES', 'TZS', 'JPY', 'XOF', 'XAF'].includes(String(currency || '').toUpperCase());
}
function shortId(gid) {
  // gid://shopify/PaymentSession/abc123  ->  abc123 (safe for a reference)
  return String(gid || '').split('/').pop().replace(/[^a-zA-Z0-9]/g, '').slice(0, 40) || crypto.randomUUID().slice(0, 12);
}
function timingSafeHex(a, b) {
  try {
    return crypto.timingSafeEqual(Buffer.from(a, 'hex'), Buffer.from(String(b || ''), 'hex'));
  } catch {
    return false;
  }
}

if (require.main === module) {
  app.listen(PORT, () => console.log(`Samsoftpay Shopify app on :${PORT}`));
}
module.exports = app;
