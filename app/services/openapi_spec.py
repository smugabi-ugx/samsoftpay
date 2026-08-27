"""Machine-readable OpenAPI 3.1 spec, served at /openapi.json.

The single source an AI/codegen tool needs to integrate: every money endpoint,
its parameters, request/response schemas, the auth scheme, the error shape, and
the webhook events. Kept in sync with docs.md/docs.html by hand — the shapes here
match what those document (additive-only, per the changelog stability promise).

Built as a function so the `servers` URL follows the actual host.
"""
from __future__ import annotations

_BEARER = [{"bearerAuth": []}]

# Shared header parameters for money-moving POSTs.
_H_TIMESTAMP = {
    "name": "X-Timestamp", "in": "header", "required": True,
    "schema": {"type": "string"},
    "description": "Unix time — seconds or Date.now() milliseconds. Requests older than 5 min (or >60s in the future) are rejected 400 (replay guard).",
}
_H_IDEM = {
    "name": "Idempotency-Key", "in": "header", "required": True,
    "schema": {"type": "string"},
    "description": "Unique key per logical request. A retry with the same key returns the original result — never double-charges/double-pays.",
}


def _err(desc):
    return {"description": desc,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def build_openapi(base_url: str) -> dict:
    base = (base_url or "https://api.samsoftpay.com").rstrip("/")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Samsoftpay API",
            "version": "1.0.0",
            "summary": "Mobile-Money payment gateway for Uganda.",
            "description": (
                "MTN Mobile Money collections & disbursements, hosted checkout, payment "
                "links, vending payments, subaccount splits and signed webhooks. One base "
                "URL for test and live — the key prefix (`sk_test_` vs `sk_live_`) selects "
                "the mode. Human docs: " + base + "/docs · Markdown: " + base + "/docs.md"),
            "contact": {"name": "Samsoftpay", "url": base + "/docs"},
        },
        "servers": [{"url": base, "description": "Same host for test and live; the key selects the mode."}],
        "security": _BEARER,
        "tags": [
            {"name": "Charges", "description": "Collect money (money-in)."},
            {"name": "Payouts", "description": "Send money to a wallet (money-out). Full keys only."},
            {"name": "Balance", "description": "Reconciliation."},
            {"name": "Payment Links", "description": "Hosted checkout / QR."},
            {"name": "Vending", "description": "Machine-present payments."},
            {"name": "Subaccounts", "description": "Split payments."},
        ],
        "paths": _paths(),
        "webhooks": _webhooks(),
        "components": _components(),
    }


def _paths() -> dict:
    charge_ok = {"200": _obj("Charge"), "201": _obj("Charge")}
    return {
        "/v1/charges": {
            "post": {
                "tags": ["Charges"], "summary": "Create a charge",
                "description": "Prompt a customer's Mobile Money to pay. Returns immediately as `authorized`/`pending`; the final state arrives via the `charge.succeeded`/`charge.failed` webhook or by polling GET /v1/charges/{id}.",
                "security": _BEARER, "parameters": [_H_TIMESTAMP, _H_IDEM],
                "requestBody": _body("ChargeCreate"),
                "responses": {"201": _obj("Charge"), "400": _err("Invalid request"),
                              "401": _err("Missing/invalid key"), "402": _err("Rail refused"),
                              "409": _err("Idempotency key in flight"), "429": _err("Rate limited")},
            },
            "get": {
                "tags": ["Charges"], "summary": "List charges",
                "security": _BEARER,
                "parameters": [
                    _q("status", "Filter by status", enum=["pending", "authorized", "succeeded", "failed", "refunded"]),
                    _q("reference", "Filter by your merchant reference"),
                    _q("phone", "Customer MSISDN (matched on last 9 digits)"),
                    _q("email", "Customer email (case-insensitive)"),
                    _q("created_after", "ISO-8601 lower bound"),
                    _q("created_before", "ISO-8601 upper bound"),
                    _q("limit", "1–100 (default 20)", typ="integer"),
                    _q("starting_after", "Cursor: pass a previous next_cursor"),
                ],
                "responses": {"200": _list("Charge"), "401": _err("Auth")},
            },
        },
        "/v1/charges/{id}": {
            "get": {
                "tags": ["Charges"], "summary": "Retrieve a charge",
                "security": _BEARER, "parameters": [_path_id("txn_…")],
                "responses": {"200": _obj("Charge"), "404": _err("Not found")},
            },
        },
        "/v1/charges/{id}/refund": {
            "post": {
                "tags": ["Charges"], "summary": "Refund a charge",
                "description": "Refunds the customer in full via disbursement. Requires a FULL key and available balance. Split charges are not refundable yet (400).",
                "security": _BEARER, "parameters": [_path_id("txn_…"), _H_TIMESTAMP, _H_IDEM],
                "responses": {"202": _obj("RefundResult"), "400": _err("Already refunded / insufficient available / split charge"),
                              "403": _err("Collections-only key"), "404": _err("Not found")},
            },
        },
        "/v1/payouts": {
            "post": {
                "tags": ["Payouts"], "summary": "Create a payout",
                "description": "Disburse to a recipient's Mobile Money wallet. FULL key only (collections-only keys are 403'd). A flat fee applies; a failed payout refunds amount+fee.",
                "security": _BEARER, "parameters": [_H_TIMESTAMP, _H_IDEM],
                "requestBody": _body("PayoutCreate"),
                "responses": {"201": _obj("Payout"), "400": _err("Invalid / insufficient available balance"),
                              "401": _err("Auth"), "403": _err("Collections-only key"),
                              "409": _err("Idempotency in flight"), "503": _err("Rail temporarily unavailable — retry same key")},
            },
            "get": {
                "tags": ["Payouts"], "summary": "List payouts",
                "security": _BEARER,
                "parameters": [_q("status", "Filter by status"),
                               _q("created_after", "ISO-8601"), _q("created_before", "ISO-8601"),
                               _q("limit", "1–100", typ="integer")],
                "responses": {"200": _list("Payout"), "401": _err("Auth")},
            },
        },
        "/v1/payouts/{id}": {
            "get": {
                "tags": ["Payouts"], "summary": "Retrieve a payout",
                "security": _BEARER, "parameters": [_path_id("pout_…")],
                "responses": {"200": _obj("Payout"), "404": _err("Not found")},
            },
        },
        "/v1/payouts/bulk": {
            "post": {
                "tags": ["Payouts"], "summary": "Bulk payouts (per-item, not atomic)",
                "description": "Up to 1000 items; some may succeed while others fail. FULL key only. The array root may be `payouts` or `items`. Each item dedupes on its `reference`.",
                "security": _BEARER, "parameters": [_H_TIMESTAMP, _H_IDEM],
                "requestBody": _body("BulkPayoutCreate"),
                "responses": {"200": _obj("BulkPayoutResult"), "400": _err("Invalid"),
                              "403": _err("Collections-only key")},
            },
        },
        "/v1/balance": {
            "get": {
                "tags": ["Balance"], "summary": "Per-currency balance",
                "description": "Reports the JOURNAL sum (with a `consistent` flag if the cached figure disagrees). Mode-scoped by your key. Use `available` to decide if payroll can run.",
                "security": _BEARER,
                "responses": {"200": _obj("BalanceResponse"), "401": _err("Auth")},
            },
        },
        "/v1/resolve-account": {
            "get": {
                "tags": ["Payouts"], "summary": "Resolve a Mobile-Money account (pre-payout check)",
                "security": _BEARER,
                "parameters": [_q("phone", "Recipient MSISDN", required=True)],
                "responses": {"200": _obj("ResolveAccount"), "400": _err("Missing phone"),
                              "403": _err("Collections-only key")},
            },
        },
        "/v1/payment-links": {
            "post": {
                "tags": ["Payment Links"], "summary": "Create a hosted-checkout link + QR",
                "security": _BEARER, "parameters": [_H_TIMESTAMP],
                "requestBody": _body("PaymentLinkCreate"),
                "responses": {"201": _obj("PaymentLink"), "400": _err("Invalid (e.g. non-http success_url)")},
            },
        },
        "/v1/vending/orders": {
            "post": {
                "tags": ["Vending"], "summary": "Create a vending order (QR + auto-dispense)",
                "security": _BEARER, "parameters": [_H_TIMESTAMP, dict(_H_IDEM, required=False)],
                "requestBody": _body("VendingOrderCreate"),
                "responses": {"201": _obj("VendingOrder"), "400": _err("Invalid"), "403": _err("Vending disabled")},
            },
        },
        "/v1/vending/orders/{id}": {
            "get": {
                "tags": ["Vending"], "summary": "Retrieve a vending order",
                "security": _BEARER, "parameters": [_path_id("vnd_…")],
                "responses": {"200": _obj("VendingOrder"), "404": _err("Not found")},
            },
        },
        "/v1/vending/conformance": {
            "post": {
                "tags": ["Vending"], "summary": "Machine Integration Standard — signature conformance check",
                "description": "Post a sample signed dispense-result callback; we confirm it verifies against your vendor signing profile. Moves no money, changes no state.",
                "security": _BEARER, "parameters": [_H_TIMESTAMP],
                "requestBody": _body("ConformanceCheck"),
                "responses": {"200": _obj("ConformanceResult"), "400": _err("Missing payload/sign or no vendor secret")},
            },
        },
        "/v1/subaccounts": {
            "post": {
                "tags": ["Subaccounts"], "summary": "Register a subaccount for split payments",
                "security": _BEARER, "parameters": [_H_TIMESTAMP],
                "requestBody": _body("SubaccountCreate"),
                "responses": {"201": _obj("Subaccount"), "400": _err("Invalid")},
            },
        },
    }


# ---- helpers ----

def _q(name, desc, *, required=False, typ="string", enum=None):
    sch = {"type": typ}
    if enum:
        sch["enum"] = enum
    return {"name": name, "in": "query", "required": required, "schema": sch, "description": desc}


def _path_id(example):
    return {"name": "id", "in": "path", "required": True,
            "schema": {"type": "string"}, "example": example}


def _obj(ref):
    return {"description": "OK", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}}}


def _list(ref):
    return {"description": "A page of results", "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "object": {"type": "string", "const": "list"},
            "data": {"type": "array", "items": {"$ref": f"#/components/schemas/{ref}"}},
            "has_more": {"type": "boolean"},
            "next_cursor": {"type": ["string", "null"]},
        }}}}}


def _body(ref):
    return {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}}}


def _webhooks() -> dict:
    def hook(event, data_ref):
        return {"post": {
            "summary": event,
            "description": f"Delivered to your webhook URL, signed with `X-Samsoftpay-Signature` (HMAC-SHA256 of the raw body using your whsec_ secret). Dedupe on `id`; may arrive more than once / out of order.",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "example": "evt_…"},
                    "event": {"type": "string", "const": event},
                    "data": {"$ref": f"#/components/schemas/{data_ref}"},
                }}}}},
            "responses": {"200": {"description": "Return any 2xx to acknowledge and stop retries."}},
        }}
    return {
        "charge.succeeded": hook("charge.succeeded", "Charge"),
        "charge.failed": hook("charge.failed", "Charge"),
        "payout.succeeded": hook("payout.succeeded", "Payout"),
        "payout.failed": hook("payout.failed", "Payout"),
        "vending.dispensed": hook("vending.dispensed", "VendingOrder"),
        "vending.dispense_failed": hook("vending.dispense_failed", "VendingOrder"),
    }


def _components() -> dict:
    money = {"type": "integer", "description": "Whole UGX (no minor units)."}
    ts = {"type": ["string", "null"], "format": "date-time"}
    return {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer",
                           "description": "`Authorization: Bearer sk_test_…` (sandbox) or `sk_live_…` (live). `sk_*_col_` collections-only keys can take money in but are 403'd on payouts/refunds."}
        },
        "schemas": {
            "Error": {"type": "object", "properties": {
                "error": {"type": "string", "description": "Human-readable message; also a stable code where relevant."},
                "request_id": {"type": "string"}}},
            "Customer": {"type": "object", "properties": {
                "phone": {"type": "string", "example": "256700123456"},
                "email": {"type": "string"}}, "required": ["phone"]},
            "Recipient": {"type": "object", "properties": {
                "phone": {"type": "string", "example": "256780000001"},
                "name": {"type": "string"}}, "required": ["phone"]},
            "Split": {"type": "object", "properties": {
                "subaccount": {"type": "string", "example": "sub_…"},
                "amount": dict(money, description="Fixed share of the net (amount − fee)."),
                "bps": {"type": "integer", "description": "Basis points of the net (100 = 1%)."}},
                "required": ["subaccount"]},
            "ChargeCreate": {"type": "object", "required": ["amount", "customer"], "properties": {
                "amount": money, "currency": {"type": "string", "default": "UGX"},
                "channel": {"type": "string", "default": "mtn_momo", "enum": ["mtn_momo"]},
                "customer": {"$ref": "#/components/schemas/Customer"},
                "reference": {"type": "string", "description": "Your idempotent business reference."},
                "split": {"type": "array", "items": {"$ref": "#/components/schemas/Split"}}}},
            "Charge": {"type": "object", "properties": {
                "id": {"type": "string", "example": "txn_9f2c41d8a3b7e615"},
                "status": {"type": "string", "enum": ["pending", "authorized", "succeeded", "failed", "refunded"]},
                "amount": money, "fee": money, "currency": {"type": "string"},
                "channel": {"type": "string"}, "mode": {"type": "string", "enum": ["test", "live"]},
                "merchant_reference": {"type": ["string", "null"]},
                "customer_phone": {"type": ["string", "null"]},
                "failure_reason": {"type": ["string", "null"]},
                "created_at": ts, "completed_at": ts}},
            "RefundResult": {"type": "object", "properties": {
                "charge_id": {"type": "string"}, "status": {"type": "string", "const": "refunded"},
                "refund": {"type": "object", "properties": {
                    "id": {"type": "string", "example": "pout_…"}, "amount": money,
                    "status": {"type": "string"}}}}},
            "PayoutCreate": {"type": "object", "required": ["amount", "recipient"], "properties": {
                "amount": money, "currency": {"type": "string", "default": "UGX"},
                "channel": {"type": "string", "default": "mtn_momo"},
                "recipient": {"$ref": "#/components/schemas/Recipient"},
                "reference": {"type": "string"}}},
            "Payout": {"type": "object", "properties": {
                "id": {"type": "string", "example": "pout_9f2a…"},
                "mode": {"type": "string", "enum": ["test", "live"]},
                "status": {"type": "string", "enum": ["pending", "authorized", "succeeded", "failed"]},
                "amount": money, "fee": money, "currency": {"type": "string"}, "channel": {"type": "string"},
                "recipient_phone": {"type": ["string", "null"]},
                "rail_reference": {"type": ["string", "null"]},
                "reference": {"type": ["string", "null"]},
                "failure_reason": {"type": ["string", "null"], "description": "Stable code, e.g. recipient_not_found, wallet_locked, timeout, insufficient_funds, ambiguous_network_error_pending_reconciliation."},
                "created_at": ts, "completed_at": ts}},
            "BulkPayoutCreate": {"type": "object", "properties": {
                "channel": {"type": "string", "default": "mtn_momo"},
                "payouts": {"type": "array", "items": {"type": "object", "properties": {
                    "amount": money, "recipient": {"$ref": "#/components/schemas/Recipient"},
                    "phone": {"type": "string", "description": "Flat alternative to recipient.phone (CSV form)."},
                    "name": {"type": "string"}, "reference": {"type": "string"}}}},
                "items": {"type": "array", "description": "Alias for `payouts`.",
                          "items": {"type": "object"}}}},
            "BulkPayoutResult": {"type": "object", "properties": {
                "batch_id": {"type": "string"}, "total": {"type": "integer"},
                "accepted": {"type": "integer"}, "failed": {"type": "integer"},
                "results": {"type": "array", "items": {"type": "object", "properties": {
                    "index": {"type": "integer"}, "ok": {"type": "boolean"},
                    "id": {"type": "string"}, "status": {"type": "string"},
                    "reference": {"type": "string"}, "error": {"type": "string"},
                    "replayed": {"type": "boolean"}, "retryable": {"type": "boolean"}}}}}},
            "BalanceResponse": {"type": "object", "properties": {
                "balances": {"type": "array", "items": {"type": "object", "properties": {
                    "currency": {"type": "string"}, "available": money, "pending": money,
                    "total": money, "cached": money}}},
                "consistent": {"type": "boolean"}}},
            "ResolveAccount": {"type": "object", "properties": {
                "msisdn": {"type": "string"}, "active": {"type": "boolean"},
                "registered_name": {"type": ["string", "null"]}}},
            "PaymentLinkCreate": {"type": "object", "required": ["amount"], "properties": {
                "amount": money, "currency": {"type": "string", "default": "UGX"},
                "reference": {"type": "string"},
                "success_url": {"type": "string", "description": "Must be http(s)."},
                "cancel_url": {"type": "string", "description": "Must be http(s)."}}},
            "PaymentLink": {"type": "object", "properties": {
                "id": {"type": "string", "example": "lnk_…"}, "amount": money, "currency": {"type": "string"},
                "status": {"type": "string"},
                "qr_png_url": {"type": "string"}, "qr_svg_url": {"type": "string"}}},
            "VendingOrderCreate": {"type": "object", "required": ["machine", "amount", "goods"], "properties": {
                "machine": {"type": "string", "description": "Machine number (jqbh)."},
                "amount": money, "currency": {"type": "string", "default": "UGX"},
                "goods": {"type": "array", "items": {"type": "object"}},
                "reference": {"type": "string"}, "success_url": {"type": "string"}}},
            "VendingOrder": {"type": "object", "properties": {
                "order_id": {"type": "string", "example": "vnd_…"}, "amount": money,
                "currency": {"type": "string"}, "machine": {"type": "string"},
                "payment_status": {"type": "string"}, "vending_status": {"type": "string"},
                "qr_png_url": {"type": "string"}, "qr_svg_url": {"type": "string"},
                "pay_url": {"type": "string"}, "status_url": {"type": "string"}}},
            "ConformanceCheck": {"type": "object", "required": ["payload", "sign"], "properties": {
                "payload": {"type": "object", "description": "The exact dispense-result callback JSON."},
                "timestamp": {"type": "string"}, "sign": {"type": "string"}}},
            "ConformanceResult": {"type": "object", "properties": {
                "ok": {"type": "boolean"}, "vendor": {"type": "string"}, "profile": {"type": "string"},
                "expected_reqData_any_of": {"type": "array", "items": {"type": "string"}}}},
            "SubaccountCreate": {"type": "object", "required": ["name", "payout_phone"], "properties": {
                "name": {"type": "string"}, "payout_phone": {"type": "string"},
                "external_ref": {"type": "string"}}},
            "Subaccount": {"type": "object", "properties": {
                "id": {"type": "string", "example": "sub_…"}, "name": {"type": "string"},
                "status": {"type": "string"}}},
        },
    }
