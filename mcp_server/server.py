"""Samsoftpay MCP server.

Exposes Samsoftpay's merchant API as MCP tools so an AI client (Claude Desktop,
Claude Code, etc.) — or KarlPOS automation — can operate the platform: create
charges, check status, issue payouts/refunds, run BATCH payouts, create links.

Config (environment variables; never hardcode a key):
    SAMSOFTPAY_API_KEY     merchant secret key. sk_test_... = sandbox, sk_live_... = real money.
    SAMSOFTPAY_BASE_URL    default https://api.samsoftpay.com
    SAMSOFTPAY_ALLOW_LIVE  must be "1" to permit money-moving ops with a LIVE key.

Money safety:
  - Money-moving tools REFUSE a live key unless SAMSOFTPAY_ALLOW_LIVE=1.
  - Every money op takes an idempotency_key; reusing the same key never double-pays.
    For KarlPOS withdrawals/batch payouts, pass a STABLE key per item so retries are safe.
  - The MCP client also asks the human to approve each tool call.

Run:  python -m mcp_server.server     (stdio transport)
"""
import os
import time
import uuid

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("samsoftpay")

_TIMEOUT = 30


# ---------- config (read dynamically so it's testable + reflects env changes) ----------

def _base() -> str:
    return os.environ.get("SAMSOFTPAY_BASE_URL", "https://api.samsoftpay.com").rstrip("/")


def _key() -> str:
    return os.environ.get("SAMSOFTPAY_API_KEY", "")


def _allow_live() -> bool:
    return os.environ.get("SAMSOFTPAY_ALLOW_LIVE") == "1"


def _is_live() -> bool:
    return _key().startswith("sk_live_")


def _mode() -> str:
    return "live" if _is_live() else "test"


# ---------- helpers ----------

def _headers(post: bool = False, idem: str = "") -> dict:
    h = {"Authorization": f"Bearer {_key()}"}
    if post:
        h["Idempotency-Key"] = idem or uuid.uuid4().hex
        h["X-Timestamp"] = str(int(time.time()))
        h["Content-Type"] = "application/json"
    return h


def _guard_money() -> str | None:
    if not _key():
        return "No SAMSOFTPAY_API_KEY configured. Set it in the MCP server environment."
    if _is_live() and not _allow_live():
        return (
            "BLOCKED: refusing a money-moving operation with a LIVE key. "
            "Use a test key (sk_test_...) for safe testing, or set "
            "SAMSOFTPAY_ALLOW_LIVE=1 to explicitly permit real money."
        )
    return None


def _result(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:500]}
    base = {"http_status": r.status_code, "mode": _mode()}
    return {**base, **(data if isinstance(data, dict) else {"data": data})}


def _post(path: str, body: dict | None, idem: str = "") -> dict:
    try:
        r = httpx.post(f"{_base()}{path}", headers=_headers(post=True, idem=idem),
                       json=body, timeout=_TIMEOUT)
        return _result(r)
    except Exception as exc:
        return {"error": str(exc), "path": path}


def _get(path: str) -> dict:
    if not _key():
        return {"error": "No SAMSOFTPAY_API_KEY configured."}
    try:
        r = httpx.get(f"{_base()}{path}", headers=_headers(), timeout=_TIMEOUT)
        return _result(r)
    except Exception as exc:
        return {"error": str(exc), "path": path}


# ---------- read-only tools ----------

@mcp.tool()
def ping() -> dict:
    """Check that Samsoftpay is reachable and its database is healthy. No key needed."""
    try:
        r = httpx.get(f"{_base()}/healthz", timeout=_TIMEOUT)
        return {"http_status": r.status_code, "base_url": _base(), "body": r.text[:200]}
    except Exception as exc:
        return {"error": str(exc), "base_url": _base()}


@mcp.tool()
def whoami() -> dict:
    """Show which environment this server targets (test/live) and base URL. Never reveals the key."""
    k = _key()
    return {
        "base_url": _base(),
        "mode": _mode() if k else "no key set",
        "key_prefix": (k[:11] + "...") if k else None,
        "live_money_allowed": _allow_live(),
    }


@mcp.tool()
def get_charge(charge_id: str) -> dict:
    """Get a charge's current status by id (e.g. txn_abc123). Read-only."""
    return _get(f"/v1/charges/{charge_id}")


@mcp.tool()
def get_payout(payout_id: str) -> dict:
    """Get a payout's current status by id (e.g. pout_abc123). Read-only."""
    return _get(f"/v1/payouts/{payout_id}")


# ---------- money-moving tools ----------

@mcp.tool()
def create_charge(amount: int, phone: str, channel: str = "mtn_momo",
                  currency: str = "UGX", reference: str = "", idempotency_key: str = "") -> dict:
    """Create a charge to COLLECT a Mobile Money payment from a customer.

    amount: whole UGX (2500 = UGX 2,500). phone: 2567XXXXXXXX. channel: mtn_momo | airtel_money.
    Sends a payment prompt to the payer; poll get_charge(id) until 'succeeded'/'failed'.
    idempotency_key: pass a stable value to make retries safe (no double charge).
    """
    guard = _guard_money()
    if guard:
        return {"error": guard}
    body = {"amount": amount, "currency": currency, "channel": channel, "customer": {"phone": phone}}
    if reference:
        body["reference"] = reference
    return _post("/v1/charges", body, idem=idempotency_key)


@mcp.tool()
def refund_charge(charge_id: str, idempotency_key: str = "") -> dict:
    """Refund a succeeded charge back to the customer's Mobile Money. Moves money."""
    guard = _guard_money()
    if guard:
        return {"error": guard}
    return _post(f"/v1/charges/{charge_id}/refund", None, idem=idempotency_key)


@mcp.tool()
def create_payout(amount: int, recipient_phone: str, recipient_name: str = "",
                  channel: str = "mtn_momo", currency: str = "UGX", idempotency_key: str = "") -> dict:
    """Send a single payout (disburse money) to a recipient's Mobile Money. Moves money.

    Used for KarlPOS merchant WITHDRAWALS. amount: whole UGX. recipient_phone: 2567XXXXXXXX.
    idempotency_key: pass a stable value so a retry never double-pays.
    """
    guard = _guard_money()
    if guard:
        return {"error": guard}
    body = {"amount": amount, "currency": currency, "channel": channel,
            "recipient": {"phone": recipient_phone}}
    if recipient_name:
        body["recipient"]["name"] = recipient_name
    return _post("/v1/payouts", body, idem=idempotency_key)


@mcp.tool()
def batch_payout(payouts: list[dict]) -> dict:
    """Send MANY payouts at once (KarlPOS batch withdrawals / bulk disbursement). Moves money.

    payouts: list of dicts, each:
        {"amount": int, "recipient_phone": "2567...", "recipient_name"?: str,
         "channel"?: "mtn_momo", "idempotency_key"?: str}
    STRONGLY pass a stable idempotency_key per item so re-running the batch never double-pays.
    Returns a per-item result list plus accepted/failed counts. Items are independent — one
    failure does not stop the others.
    """
    guard = _guard_money()
    if guard:
        return {"error": guard}
    if not isinstance(payouts, list) or not payouts:
        return {"error": "payouts must be a non-empty list"}
    if len(payouts) > 1000:
        return {"error": "batch too large (max 1000 per call)"}

    results = []
    for i, p in enumerate(payouts):
        try:
            amount = int(p["amount"])
            phone = str(p["recipient_phone"])
        except (KeyError, ValueError, TypeError) as exc:
            results.append({"index": i, "error": f"invalid item: {exc}"})
            continue
        body = {"amount": amount, "currency": p.get("currency", "UGX"),
                "channel": p.get("channel", "mtn_momo"), "recipient": {"phone": phone}}
        if p.get("recipient_name"):
            body["recipient"]["name"] = p["recipient_name"]
        res = _post("/v1/payouts", body, idem=p.get("idempotency_key", ""))
        results.append({"index": i, **res})

    accepted = sum(1 for x in results if x.get("http_status") in (200, 201))
    failed = len(results) - accepted
    return {"total": len(payouts), "accepted": accepted, "failed": failed, "results": results}


@mcp.tool()
def create_payment_link(amount: int, description: str = "", reference: str = "",
                        currency: str = "UGX", idempotency_key: str = "") -> dict:
    """Create a shareable payment link a customer can open to pay. Returns the link url."""
    guard = _guard_money()
    if guard:
        return {"error": guard}
    body = {"amount": amount, "currency": currency}
    if description:
        body["description"] = description
    if reference:
        body["reference"] = reference
    return _post("/v1/payment-links", body, idem=idempotency_key)


if __name__ == "__main__":
    mcp.run()
