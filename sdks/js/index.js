'use strict';
/**
 * Samsoftpay Node SDK (starter) — a thin, dependency-free wrapper over the
 * Samsoftpay API. Node 18+ (uses global fetch + node:crypto).
 *
 *   const Samsoftpay = require('samsoftpay');
 *   const sp = new Samsoftpay(process.env.SAMSOFTPAY_SECRET_KEY, {
 *     webhookSecret: process.env.SAMSOFTPAY_WEBHOOK_SECRET,
 *   });
 *   const charge = await sp.charges.create({ amount: 10000, channel: 'mtn_momo',
 *     customer: { phone: '256700123456' }, reference: 'order-1' });
 *
 * Same base URL for test and live — the key prefix (sk_test_ / sk_live_) picks
 * the mode. X-Timestamp and a per-request Idempotency-Key are added automatically
 * to money POSTs.
 */
const crypto = require('node:crypto');

class SamsoftpayError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'SamsoftpayError';
    this.status = status;
    this.body = body;
  }
}

class Samsoftpay {
  constructor(secretKey, opts = {}) {
    if (!secretKey) throw new Error('Samsoftpay: secretKey is required');
    this.secretKey = secretKey;
    this.baseUrl = (opts.baseUrl || 'https://api.samsoftpay.com').replace(/\/$/, '');
    this.webhookSecret = opts.webhookSecret || null;
    this.timeoutMs = opts.timeoutMs || 20000;

    this.charges = {
      create: (body, o) => this._post('/v1/charges', body, o),
      get: (id) => this._get(`/v1/charges/${encodeURIComponent(id)}`),
      list: (q) => this._get('/v1/charges' + this._qs(q)),
      refund: (id, o) => this._post(`/v1/charges/${encodeURIComponent(id)}/refund`, {}, o),
    };
    this.payouts = {
      create: (body, o) => this._post('/v1/payouts', body, o),
      get: (id) => this._get(`/v1/payouts/${encodeURIComponent(id)}`),
      list: (q) => this._get('/v1/payouts' + this._qs(q)),
      bulk: (body, o) => this._post('/v1/payouts/bulk', body, o),
    };
    this.scheduledPayouts = {
      create: (body, o) => this._post('/v1/scheduled-payouts', body, o),
      get: (id) => this._get(`/v1/scheduled-payouts/${encodeURIComponent(id)}`),
      list: (q) => this._get('/v1/scheduled-payouts' + this._qs(q)),
    };
    this.balance = { get: () => this._get('/v1/balance') };
    this.statements = {
      get: (period) => this._get(`/v1/statements/${encodeURIComponent(period)}`),
      pdfUrl: (period) => `${this.baseUrl}/v1/statements/${encodeURIComponent(period)}.pdf`,
    };
    this.subaccounts = { create: (body, o) => this._post('/v1/subaccounts', body, o) };
    this.paymentLinks = { create: (body, o) => this._post('/v1/payment-links', body, o) };
    this.resolveAccount = (phone) => this._get('/v1/resolve-account?phone=' + encodeURIComponent(phone));
  }

  /** Verify an inbound webhook. Pass the RAW request body (string/Buffer) and the
   *  X-Samsoftpay-Signature header. Returns true if authentic. */
  verifyWebhook(rawBody, signature) {
    if (!this.webhookSecret) throw new Error('Samsoftpay: webhookSecret not configured');
    const expected = crypto.createHmac('sha256', this.webhookSecret)
      .update(typeof rawBody === 'string' ? rawBody : Buffer.from(rawBody)).digest('hex');
    const a = Buffer.from(String(signature || ''));
    const b = Buffer.from(expected);
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  }

  // ---- internals ----
  _headers(post) {
    const h = { Authorization: `Bearer ${this.secretKey}` };
    if (post) {
      h['Content-Type'] = 'application/json';
      h['X-Timestamp'] = String(Math.floor(Date.now() / 1000));
    }
    return h;
  }

  _qs(q) {
    if (!q) return '';
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) if (v != null) p.append(k, String(v));
    const s = p.toString();
    return s ? '?' + s : '';
  }

  async _get(path) { return this._req('GET', path, null, {}); }

  async _post(path, body, opts = {}) {
    const headers = this._headers(true);
    // A money POST needs an Idempotency-Key — generate one if the caller didn't.
    headers['Idempotency-Key'] = opts.idempotencyKey || crypto.randomUUID();
    return this._req('POST', path, body, headers);
  }

  async _req(method, path, body, extraHeaders) {
    const headers = Object.assign(this._headers(method === 'POST'), extraHeaders || {});
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    let res;
    try {
      res = await fetch(this.baseUrl + path, {
        method, headers,
        body: body != null ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
    } finally { clearTimeout(t); }
    const text = await res.text();
    let json;
    try { json = text ? JSON.parse(text) : {}; } catch { json = { raw: text }; }
    if (!res.ok) {
      throw new SamsoftpayError(json.error || `HTTP ${res.status}`, res.status, json);
    }
    return json;
  }
}

Samsoftpay.SamsoftpayError = SamsoftpayError;
module.exports = Samsoftpay;
module.exports.default = Samsoftpay;
