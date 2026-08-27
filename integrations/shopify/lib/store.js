'use strict';
// A tiny file-backed store for (a) per-shop OAuth access tokens and (b) the
// mapping between a Shopify payment/refund session and the Samsoftpay charge it
// created. Swap this for Postgres/Redis in production — the interface is small.
//
// WHY a mapping is needed: Shopify POSTs us a payment session, we redirect the
// buyer to Samsoftpay's hosted checkout, and LATER Samsoftpay's webhook tells us
// the charge succeeded. To resolve the RIGHT Shopify session we must remember
// which Samsoftpay charge belonged to which session.

const fs = require('fs');
const path = require('path');

const FILE = process.env.STORE_FILE || path.join(__dirname, '..', '.data', 'store.json');

function _load() {
  try {
    return JSON.parse(fs.readFileSync(FILE, 'utf8'));
  } catch {
    return { shops: {}, sessions: {} };
  }
}
function _save(db) {
  fs.mkdirSync(path.dirname(FILE), { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify(db, null, 2));
}

// ---- shop OAuth tokens ----
function saveShopToken(shop, accessToken) {
  const db = _load();
  db.shops[shop] = { accessToken, updatedAt: new Date().toISOString() };
  _save(db);
}
function getShopToken(shop) {
  return (_load().shops[shop] || {}).accessToken || null;
}

// ---- session <-> Samsoftpay mapping, keyed by the `reference` we set on the
// payment link (the webhook echoes it back as data.reference, so it's the one
// value guaranteed to correlate the async callback to the Shopify session).
// value: { shop, sessionGid, kind: 'payment'|'refund', amount, currency }
function linkReference(reference, record) {
  const db = _load();
  db.sessions[reference] = { ...record, createdAt: new Date().toISOString() };
  _save(db);
}
function findByReference(reference) {
  return _load().sessions[reference] || null;
}
function markResolved(reference) {
  const db = _load();
  if (db.sessions[reference]) {
    db.sessions[reference].resolvedAt = new Date().toISOString();
    _save(db);
  }
}

module.exports = { saveShopToken, getShopToken, linkReference, findByReference, markResolved };
