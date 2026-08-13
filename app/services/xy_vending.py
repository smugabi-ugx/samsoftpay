"""XY Vending (Hunan Xing Yuan) cloud API connector.

Samsoftpay -> XY Vending backend. After a payment succeeds, Samsoftpay calls the
supplier's `ApplyExportGoods` endpoint to make the machine dispense. Also wraps
the read endpoints (list machines, live tray inventory/products) so a menu can
be built from the machine's real stock.

CREDENTIALS ARE PER MERCHANT. XY issues a key/secret/merchant-number (shbh) to
each operator, and one operator account owns many machines (jqbh). Every call
therefore takes an `XYCredentials`; the environment variables remain only as a
fallback for a single-operator deployment and for local testing.

Auth (per XY "third-party platform integration service interface" doc):
  Common params on signed calls: key, secret, sign, timestamp (13-digit ms).
  sign = MD5(secret + timestamp + reqData)
  reqData = the request params joined "k=v&k=v", keys sorted alphabetically asc.
  MD5 is lowercase hex (matches the doc's example signatures).

Env fallback (optional):
  XY_BASE_URL       default http://175.6.71.238:8090
  XY_KEY            merchant key value from XY
  XY_SECRET         secret from XY (used only to compute the sign)
  XY_MERCHANT_NO    shbh (merchant number) for query calls

Endpoints are plain HTTP on the supplier's own infrastructure — the secret is
never put in a URL or body, only hashed into the sign.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

import requests

_TIMEOUT = 30
_DEFAULT_BASE = "http://175.6.71.238:8090"


class XYVendingError(Exception):
    pass


# ---------- credentials ----------

@dataclass(frozen=True)
class XYCredentials:
    """One operator's access to the supplier's cloud."""
    key: str = ""
    secret: str = ""
    merchant_no: str = ""
    base_url: str = _DEFAULT_BASE

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    def base(self) -> str:
        return (self.base_url or _DEFAULT_BASE).rstrip("/")


def env_credentials() -> XYCredentials:
    """Fallback credentials from the environment (single-operator deployments)."""
    return XYCredentials(
        key=os.environ.get("XY_KEY", ""),
        secret=os.environ.get("XY_SECRET", ""),
        merchant_no=os.environ.get("XY_MERCHANT_NO", ""),
        base_url=os.environ.get("XY_BASE_URL", _DEFAULT_BASE),
    )


def for_merchant(merchant) -> XYCredentials:
    """The merchant's own XY credentials, falling back to the environment.

    Falling back matters for the first operator (ours) who may be configured at
    the platform level, but a merchant with their own key always wins.
    """
    from .secrets_box import decrypt

    key = (getattr(merchant, "xy_key", "") or "").strip()
    secret = decrypt(getattr(merchant, "xy_secret_encrypted", None)) or ""
    if key and secret:
        return XYCredentials(
            key=key,
            secret=secret,
            merchant_no=(getattr(merchant, "xy_merchant_no", "") or "").strip(),
            base_url=(getattr(merchant, "xy_base_url", "") or "").strip() or _DEFAULT_BASE,
        )
    return env_credentials()


def _resolve(creds: XYCredentials | None) -> XYCredentials:
    return creds if creds is not None else env_credentials()


# ---------- signing ----------

def _now_ms() -> int:
    return int(time.time() * 1000)


def make_sign(secret: str, timestamp_ms: int, params: dict) -> str:
    """sign = MD5(secret + timestamp + reqData), reqData = 'k=v&k=v' with keys
    sorted alphabetically ascending. Lowercase hex. Empty values are kept
    (the doc's examples include empty fields like dsfjybh=)."""
    req_data = "&".join(f"{k}={_str(params[k])}" for k in sorted(params))
    raw = f"{secret}{timestamp_ms}{req_data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


# ---------- HTTP helpers ----------

def _post(path: str, body: dict, creds: XYCredentials) -> dict:
    if not creds.configured:
        raise XYVendingError("XY connector not configured (set XY_KEY and XY_SECRET)")
    url = f"{creds.base()}{path}"
    try:
        r = requests.post(url, json=body, timeout=_TIMEOUT,
                          headers={"Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise XYVendingError(f"XY request failed: {exc}") from exc
    try:
        data = r.json()
    except ValueError:
        raise XYVendingError(f"XY returned non-JSON ({r.status_code}): {r.text[:200]}")
    return data


# ---------- read endpoints (query machines / inventory) ----------

def query_machines(shbh: str | None = None, *, creds: XYCredentials | None = None) -> dict:
    """List the merchant's machines (§2.1.1 queryMachine).

    Returns code/message/data where each machine carries jqbh (number), jqmc
    (name), jqlb/jqlx (category/type), dwmc (address), dwjd/dwwd (coordinates).
    """
    c = _resolve(creds)
    return _post("/service-api/api/queryMachine", {"shbh": shbh or c.merchant_no}, c)


def query_machine_state(jqbh: str, shbh: str | None = None, *,
                        creds: XYCredentials | None = None) -> dict:
    """Network/temperature/humidity status of a machine (§2.1.2)."""
    c = _resolve(creds)
    return _post("/service-api/api/queryMachineState",
                 {"shbh": shbh or c.merchant_no, "jqbh": jqbh}, c)


def query_machine_goods(jqbh: str, shbh: str | None = None, *,
                        creds: XYCredentials | None = None) -> dict:
    """Live tray inventory + products + prices for a machine (§2.1.4
    queryMachineHdGoodPlus). This is what we build the customer menu from."""
    c = _resolve(creds)
    return _post("/service-api/api/queryMachineHdGoodPlus",
                 {"shbh": shbh or c.merchant_no, "jqbh": jqbh}, c)


# ---------- dispense (the payment -> dispense bridge) ----------

def apply_export_goods(*, jqbh: str, order_id: str, third_party_txn_id: str,
                       pay_account: str, goods: list[dict], pay_type: int = 2,
                       consume_type: str = "hiTrade", pickup_code: str = "",
                       creds: XYCredentials | None = None) -> dict:
    """Tell the machine to dispense after payment is confirmed (§2.2.1 ApplyExportGoods).

    jqbh              machine number
    order_id          our order number (ddbh)
    third_party_txn_id our transaction id (dsfjybh) — e.g. the Samsoftpay charge id
    pay_account       payer account (zfzh)
    goods             [{"spbh": product_no, "spmc": name, "spdj": unit_price}, ...]
    pay_type          order type (2 in the doc's examples)
    consume_type      business type ("hiTrade" in the doc)
    creds             the operator's credentials; env fallback when omitted

    Signed per the doc's §2.2.1 case: the flattened order fields
    (consumeType, ddbh, dsfjybh, jqbh, qhm, zfzh) sorted alphabetically.
    """
    if not goods:
        raise XYVendingError("goods list is required")

    c = _resolve(creds)
    ts = _now_ms()
    sign_params = {
        "consumeType": consume_type,
        "ddbh": order_id,
        "dsfjybh": third_party_txn_id,
        "jqbh": jqbh,
        "qhm": pickup_code,
        "zfzh": pay_account,
    }
    sign = make_sign(c.secret, ts, sign_params)

    body = {
        "orderDTO": {
            "payType": str(pay_type),
            "orderGoodsDetailList": [
                {"spbh": g.get("spbh", ""), "spmc": g.get("spmc", ""),
                 "spdj": _str(g.get("spdj", ""))}
                for g in goods
            ],
            "order": {
                "spsl": str(len(goods)),
                "zfzh": pay_account,
                "qhm": pickup_code,
                "dsfjybh": third_party_txn_id,
                "ddbh": order_id,
                "jqbh": jqbh,
            },
        },
        "sign": sign,
        "consumeType": consume_type,
        "key": c.key,
        "timestamp": ts,
    }
    resp = _post("/service-pay-third/third/pay/api/ApplyExportGoods", body, c)
    # code "1" == success per the doc.
    if str(resp.get("code")) != "1":
        raise XYVendingError(f"dispense rejected: {resp.get('message') or resp}")
    return resp
