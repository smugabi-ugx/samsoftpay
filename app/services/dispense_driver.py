"""Machine Integration Standard v1 — vendor-neutral outbound dispense driver.

The other half of the signing profile: builds, signs and POSTs the command that
tells a machine to dispense after payment is confirmed. XY's `ApplyExportGoods`
is the first body style (`xy_orderdto`); a new vendor with a different dispense
API is a new body-builder key on its profile, not a rewrite of this module.

`xy_vending.apply_export_goods` is now a thin shim over `command_dispense` with
the built-in XY profile, so the whole payment->dispense path and every XY test
are unchanged (guardrail 11: no payment, no dispense — this module runs strictly
downstream of that gate).
"""
from __future__ import annotations


def _build_xy_orderdto(*, profile, creds, jqbh, order_id, third_party_txn_id,
                       pay_account, goods, pay_type, consume_type, pickup_code, ts):
    """XY §2.2.1 ApplyExportGoods body + sign. Byte-for-byte the old
    xy_vending.apply_export_goods construction."""
    from . import xy_vending
    sign_params = {
        "consumeType": consume_type,
        "ddbh": order_id,
        "dsfjybh": third_party_txn_id,
        "jqbh": jqbh,
        "qhm": pickup_code,
        "zfzh": pay_account,
    }
    # Outbound XY sign is alphabetical (make_sign). The profile's `order`
    # (alpha_swap) is an INBOUND callback-verification tolerance, not an
    # outbound choice, so it does not apply here.
    sign = xy_vending.make_sign(creds.secret, ts, sign_params)
    body = {
        "orderDTO": {
            "payType": str(pay_type),
            "orderGoodsDetailList": [
                {"spbh": g.get("spbh", ""), "spmc": g.get("spmc", ""),
                 "spdj": xy_vending._str(g.get("spdj", ""))}
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
        "key": creds.key,
        "timestamp": ts,
    }
    return body


_BODY_BUILDERS = {"xy_orderdto": _build_xy_orderdto}


def command_dispense(*, profile, creds, jqbh, order_id, third_party_txn_id,
                     pay_account, goods, pay_type: int = 2,
                     consume_type: str | None = None, pickup_code: str = "") -> dict:
    """Build, sign and POST the dispense command per `profile`. Raises
    XYVendingError on an unconfigured connector, an unknown body style, a
    transport failure, or a non-success supplier code."""
    from . import xy_vending

    if not goods:
        raise xy_vending.XYVendingError("goods list is required")
    builder = _BODY_BUILDERS.get(profile.dispense_body_style)
    if builder is None:
        raise xy_vending.XYVendingError(
            f"unknown dispense_body_style: {profile.dispense_body_style}")
    if consume_type is None:
        consume_type = (profile.dispense_extra or {}).get("consumeType", "hiTrade")

    ts = xy_vending._now_ms()
    body = builder(profile=profile, creds=creds, jqbh=jqbh, order_id=order_id,
                   third_party_txn_id=third_party_txn_id, pay_account=pay_account,
                   goods=goods, pay_type=pay_type, consume_type=consume_type,
                   pickup_code=pickup_code, ts=ts)
    # _post enforces the configured-credentials check (its "not configured"
    # error is what the sign test asserts on).
    resp = xy_vending._post(profile.dispense_path, body, creds)
    if str(resp.get("code")) != "1":
        raise xy_vending.XYVendingError(f"dispense rejected: {resp.get('message') or resp}")
    return resp
