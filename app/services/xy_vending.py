"""XY Vending (Hunan Xing Yuan) cloud API connector.

Samsoftpay -> XY Vending backend. After a payment succeeds, Samsoftpay calls the
supplier's `ApplyExportGoods` endpoint to make the machine dispense. Also wraps
the read endpoints (list machines, live tray inventory/products) so KarlPOS/TK
Vending can build a menu from the machine's real stock.

Auth (per XY "third-party platform integration service interface" doc):
  Common params on signed calls: key, secret, sign, timestamp (13-digit ms).
  sign = MD5(secret + timestamp + reqData)
  reqData = the request params joined "k=v&k=v", keys sorted alphabetically asc.
  MD5 is lowercase hex (matches the doc's example signatures).

Config (env vars — the supplier issues key/secret; not our sandbox keys):
  XY_BASE_URL       default http://175.6.71.238:8090
  XY_KEY            merchant key value from XY
  XY_SECRET         secret from XY (used only to compute the sign)
  XY_MERCHANT_NO    shbh (merchant number) for query calls

Everything reads config dynamically so it is unit-testable and reflects env
changes without an app restart. Endpoints are HTTP (their infra) — do not put
the secret in a URL; it is only ever hashed into the sign.
"""
from __future__ import annotations

import hashlib
import os
import time

import requests

_TIMEOUT = 30


class XYVendingError(Exception):
    pass


# ---------- config (dynamic) ----------

def _base() -> str:
    return os.environ.get("XY_BASE_URL", "http://175.6.71.238:8090").rstrip("/")


def _key() -> str:
    return os.environ.get("XY_KEY", "")


def _secret() -> str:
    return os.environ.get("XY_SECRET", "")


def _merchant_no() -> str:
    return os.environ.get("XY_MERCHANT_NO", "")


def _configured() -> bool:
    return bool(_key() and _secret())


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

def _post(path: str, body: dict) -> dict:
    if not _configured():
        raise XYVendingError("XY connector not configured (set XY_KEY and XY_SECRET)")
    url = f"{_base()}{path}"
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

def query_machines(shbh: str | None = None) -> dict:
    """List the merchant's machines (§2.1.1 queryMachine)."""
    body = {"shbh": shbh or _merchant_no()}
    return _post("/service-api/api/queryMachine", body)


def query_machine_state(jqbh: str, shbh: str | None = None) -> dict:
    """Network/temperature/humidity status of a machine (§2.1.2)."""
    body = {"shbh": shbh or _merchant_no(), "jqbh": jqbh}
    return _post("/service-api/api/queryMachineState", body)


def query_machine_goods(jqbh: str, shbh: str | None = None) -> dict:
    """Live tray inventory + products + prices for a machine (§2.1.4
    queryMachineHdGoodPlus). This is what we build the customer menu from."""
    body = {"shbh": shbh or _merchant_no(), "jqbh": jqbh}
    return _post("/service-api/api/queryMachineHdGoodPlus", body)


# ---------- dispense (the payment -> dispense bridge) ----------

def apply_export_goods(*, jqbh: str, order_id: str, third_party_txn_id: str,
                       pay_account: str, goods: list[dict], pay_type: int = 2,
                       consume_type: str = "hiTrade", pickup_code: str = "") -> dict:
    """Tell the machine to dispense after payment is confirmed (§2.2.1 ApplyExportGoods).

    jqbh              machine number
    order_id          our order number (ddbh)
    third_party_txn_id our transaction id (dsfjybh) — e.g. the Samsoftpay charge id
    pay_account       payer account (zfzh)
    goods             [{"spbh": product_no, "spmc": name, "spdj": unit_price_cents}, ...]
    pay_type          order type (2 in the doc's examples)
    consume_type      business type ("hiTrade" in the doc)

    Signed per the doc's §2.2.1 case: the flattened order fields
    (consumeType, ddbh, dsfjybh, jqbh, qhm, zfzh) sorted alphabetically.
    """
    if not goods:
        raise XYVendingError("goods list is required")

    ts = _now_ms()
    sign_params = {
        "consumeType": consume_type,
        "ddbh": order_id,
        "dsfjybh": third_party_txn_id,
        "jqbh": jqbh,
        "qhm": pickup_code,
        "zfzh": pay_account,
    }
    sign = make_sign(_secret(), ts, sign_params)

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
        "key": _key(),
        "timestamp": ts,
    }
    resp = _post("/service-pay-third/third/pay/api/ApplyExportGoods", body)
    # code "1" == success per the doc.
    if str(resp.get("code")) != "1":
        raise XYVendingError(f"dispense rejected: {resp.get('message') or resp}")
    return resp
