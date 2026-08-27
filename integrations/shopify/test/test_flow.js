'use strict';
// End-to-end test of the Shopify payments app WITHOUT real Shopify/Samsoftpay:
// global.fetch is stubbed to (a) mock Samsoftpay /v1/payment-links + refund and
// (b) capture the Shopify GraphQL resolve/reject mutations. Run: node test/test_flow.js
const os = require('os');
const path = require('path');
const crypto = require('crypto');

process.env.STORE_FILE = path.join(os.tmpdir(), `sp_shopify_e2e_${process.pid}.json`);
process.env.SHOPIFY_API_SECRET = 'shpss_test';
process.env.SHOPIFY_API_KEY = 'apikey';
process.env.APP_URL = 'https://app.example.com';
process.env.SAMSOFTPAY_BASE_URL = 'https://api.samsoftpay.com';
process.env.SAMSOFTPAY_SECRET_KEY = 'sk_test_x';
process.env.SAMSOFTPAY_WEBHOOK_SECRET = 'whsec_test';

const SHOP = 'demo.myshopify.com';
const captured = { graphql: [] };

const realFetch = global.fetch;
global.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes('/v1/payment-links')) {
    return jsonRes(200, { id: 'lnk_123', status: 'pending' });
  }
  if (u.includes('/refund')) {
    return jsonRes(200, { id: 'ref_1', status: 'refunded' });
  }
  if (u.includes('/admin/api/') && u.endsWith('/graphql.json')) {
    const body = JSON.parse(opts.body);
    captured.graphql.push(body);
    // Return a plausible success shape for whichever mutation ran.
    return jsonRes(200, {
      data: {
        paymentSessionResolve: { paymentSession: { id: body.variables.id, status: { code: 'RESOLVED' } }, userErrors: [] },
        paymentSessionReject: { paymentSession: { id: body.variables.id, status: { code: 'REJECTED' } }, userErrors: [] },
        refundSessionResolve: { refundSession: { id: body.variables.id, status: { code: 'RESOLVED' } }, userErrors: [] },
      },
    });
  }
  return realFetch(url, opts);
};
function jsonRes(status, obj) {
  return { ok: status < 400, status, json: async () => obj };
}

const app = require('../server');
const store = require('../lib/store');
store.saveShopToken(SHOP, 'shpat_token'); // pretend the shop installed the app

// --- tiny in-process HTTP driver ---
const http = require('http');
let server, base;
function req(method, path, { raw, headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(base + path);
    const r = http.request(
      { method, hostname: u.hostname, port: u.port, path: u.pathname + u.search, headers },
      (res) => {
        let d = '';
        res.on('data', (c) => (d += c));
        res.on('end', () => resolve({ status: res.statusCode, body: d, json: safeJson(d) }));
      }
    );
    r.on('error', reject);
    if (raw) r.write(raw);
    r.end();
  });
}
function safeJson(s) { try { return JSON.parse(s); } catch { return null; } }
function hmacB64(body, secret) { return crypto.createHmac('sha256', secret).update(body).digest('base64'); }
function hmacHex(body, secret) { return crypto.createHmac('sha256', secret).update(body).digest('hex'); }

let pass = 0, fail = 0;
function ok(name, cond) { cond ? (pass++, console.log('  ✓', name)) : (fail++, console.log('  ✗', name)); }

(async () => {
  server = app.listen(0);
  await new Promise((r) => server.once('listening', r));
  base = `http://127.0.0.1:${server.address().port}`;
  const GID = 'gid://shopify/PaymentSession/sess777';

  // 1) Payment session: signed body -> 201 with redirect_url to hosted checkout
  const sessionBody = Buffer.from(JSON.stringify({
    id: GID, group: 'ord42', amount: '15000.00', currency: 'UGX', test: true,
    customer: { phone_number: '+256700000000' },
  }));
  const r1 = await req('POST', '/payments/session', {
    raw: sessionBody,
    headers: {
      'Content-Type': 'application/json',
      'X-Shopify-Hmac-Sha256': hmacB64(sessionBody, process.env.SHOPIFY_API_SECRET),
      'X-Shopify-Shop-Domain': SHOP,
    },
  });
  ok('[1] session accepted -> 201', r1.status === 201);
  ok('[1b] redirect_url points to hosted checkout /pay/lnk_123',
     r1.json && /\/pay\/lnk_123$/.test(r1.json.redirect_url));

  // 2) Payment session with a BAD hmac -> 401, no side effects
  const r2 = await req('POST', '/payments/session', {
    raw: sessionBody,
    headers: { 'Content-Type': 'application/json', 'X-Shopify-Hmac-Sha256': 'nope', 'X-Shopify-Shop-Domain': SHOP },
  });
  ok('[2] forged session rejected -> 401', r2.status === 401);

  // 3) Samsoftpay webhook (charge.succeeded) -> resolves the RIGHT Shopify session
  captured.graphql.length = 0;
  const reference = 'shpfy_sess777'; // shortId(GID) == 'sess777'
  const evt = Buffer.from(JSON.stringify({ event: 'charge.succeeded', data: { id: 'chg_9', reference } }));
  const r3 = await req('POST', '/webhooks/samsoftpay', {
    raw: evt,
    headers: { 'Content-Type': 'application/json', 'X-Samsoftpay-Signature': hmacHex(evt, process.env.SAMSOFTPAY_WEBHOOK_SECRET) },
  });
  ok('[3] webhook accepted -> 200', r3.status === 200);
  const resolveCall = captured.graphql.find((g) => /paymentSessionResolve/.test(g.query));
  ok('[3b] paymentSessionResolve called with the session GID',
     resolveCall && resolveCall.variables.id === GID);

  // 4) Forged webhook (bad signature) -> 400, no resolve
  captured.graphql.length = 0;
  const r4 = await req('POST', '/webhooks/samsoftpay', {
    raw: evt, headers: { 'Content-Type': 'application/json', 'X-Samsoftpay-Signature': 'deadbeef' },
  });
  ok('[4] forged webhook rejected -> 400', r4.status === 400);
  ok('[4b] no resolve on forged webhook', captured.graphql.length === 0);

  // 5) Duplicate webhook (already resolved) -> acked 200, NOT resolved twice
  captured.graphql.length = 0;
  const r5 = await req('POST', '/webhooks/samsoftpay', {
    raw: evt, headers: { 'Content-Type': 'application/json', 'X-Samsoftpay-Signature': hmacHex(evt, process.env.SAMSOFTPAY_WEBHOOK_SECRET) },
  });
  ok('[5] duplicate webhook acked -> 200', r5.status === 200);
  ok('[5b] duplicate did NOT re-resolve', captured.graphql.length === 0);

  server.close();
  console.log(`\n${fail === 0 ? 'ALL PASS' : 'FAIL'} — ${pass} passed, ${fail} failed`);
  process.exit(fail === 0 ? 0 : 1);
})();
