'use strict';
// Thin helpers for the Shopify side: HMAC verification (app-proxy / OAuth) and
// a minimal GraphQL Admin API client used to resolve/reject payment & refund
// sessions via the Payments Apps API.
//
// Docs: https://shopify.dev/docs/apps/build/payments
// The Payments Apps API is part of the GraphQL Admin API; the mutations we call
// (paymentSessionResolve/Reject, refundSessionResolve/Reject) live there.

const crypto = require('crypto');

const API_VERSION = process.env.SHOPIFY_API_VERSION || '2024-10';

// Verify Shopify's HMAC on OAuth callbacks / query strings (hex, SHA-256 of the
// sorted query string minus the `hmac`/`signature` params) — used at install.
function verifyOauthHmac(query, secret) {
  const { hmac, signature, ...rest } = query;
  const message = Object.keys(rest)
    .sort()
    .map((k) => `${k}=${Array.isArray(rest[k]) ? rest[k].join(',') : rest[k]}`)
    .join('&');
  const digest = crypto.createHmac('sha256', secret).update(message).digest('hex');
  return safeEqualHex(digest, hmac);
}

// Verify the HMAC on a webhook/session POST body (base64, SHA-256 of the RAW body).
function verifyWebhookHmac(rawBody, headerHmac, secret) {
  const digest = crypto.createHmac('sha256', secret).update(rawBody).digest('base64');
  return safeEqualB64(digest, headerHmac);
}

function safeEqualHex(a, b) {
  try {
    return crypto.timingSafeEqual(Buffer.from(a, 'hex'), Buffer.from(String(b || ''), 'hex'));
  } catch {
    return false;
  }
}
function safeEqualB64(a, b) {
  try {
    return crypto.timingSafeEqual(Buffer.from(a, 'base64'), Buffer.from(String(b || ''), 'base64'));
  } catch {
    return false;
  }
}

// Minimal GraphQL Admin API call with a shop access token.
async function adminGraphQL(shop, accessToken, query, variables) {
  const res = await fetch(`https://${shop}/admin/api/${API_VERSION}/graphql.json`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Shopify-Access-Token': accessToken,
    },
    body: JSON.stringify({ query, variables }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || json.errors) {
    const err = new Error(`Shopify GraphQL error (${res.status})`);
    err.body = json;
    throw err;
  }
  return json.data;
}

// Mark a Shopify payment session as PAID. `paymentId` is the payment's id at the
// PSP (our Samsoftpay charge/link id) for reconciliation on Shopify's side.
const RESOLVE_PAYMENT = `
  mutation resolve($id: ID!) {
    paymentSessionResolve(id: $id) {
      paymentSession { id status { code } }
      userErrors { field message }
    }
  }`;

const REJECT_PAYMENT = `
  mutation reject($id: ID!, $reason: PaymentSessionRejectionReasonInput!) {
    paymentSessionReject(id: $id, reason: $reason) {
      paymentSession { id status { code } }
      userErrors { field message }
    }
  }`;

const RESOLVE_REFUND = `
  mutation refundResolve($id: ID!) {
    refundSessionResolve(id: $id) {
      refundSession { id status { code } }
      userErrors { field message }
    }
  }`;

const REJECT_REFUND = `
  mutation refundReject($id: ID!, $reason: RefundSessionRejectionReasonInput!) {
    refundSessionReject(id: $id, reason: $reason) {
      refundSession { id status { code } }
      userErrors { field message }
    }
  }`;

async function resolvePayment(shop, token, gid) {
  return adminGraphQL(shop, token, RESOLVE_PAYMENT, { id: gid });
}
async function rejectPayment(shop, token, gid, code = 'PROCESSING_ERROR', message = 'Payment not completed') {
  return adminGraphQL(shop, token, REJECT_PAYMENT, { id: gid, reason: { code, merchantMessage: message } });
}
async function resolveRefund(shop, token, gid) {
  return adminGraphQL(shop, token, RESOLVE_REFUND, { id: gid });
}
async function rejectRefund(shop, token, gid, code = 'PROCESSING_ERROR', message = 'Refund failed') {
  return adminGraphQL(shop, token, REJECT_REFUND, { id: gid, reason: { code, merchantMessage: message } });
}

module.exports = {
  API_VERSION,
  verifyOauthHmac,
  verifyWebhookHmac,
  adminGraphQL,
  resolvePayment,
  rejectPayment,
  resolveRefund,
  rejectRefund,
};
