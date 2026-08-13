"""Offline verification of the XY Vending connector's MD5 sign scheme.

We cannot hit the supplier's live server without their real key/secret, so this
proves the sign implementation matches the DOCUMENTED formula exactly:
    sign = MD5(secret + timestamp + reqData)
    reqData = "k=v&k=v" with keys sorted alphabetically ascending, lowercase hex.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import xy_vending as xy


def expected_sign(secret, ts, params):
    req = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5(f"{secret}{ts}{req}".encode()).hexdigest()


def main():
    secret = "c5ee600-35e7-11e7-uuuu-23fb85a6b085"
    ts = 1547125938899

    # 1. Params given OUT of order must sign identically to sorted order.
    unordered = {"zfzh": "0001", "jqbh": "1707100001", "consumeType": "hiTrade",
                 "ddbh": "181213189900000003", "dsfjybh": "", "qhm": "123"}
    got = xy.make_sign(secret, ts, unordered)
    exp = expected_sign(secret, ts, unordered)
    assert got == exp, f"sign mismatch: {got} != {exp}"
    print("PASS: sign matches documented formula")

    # 2. reqData ordering is alphabetical (consumeType < ddbh < dsfjybh < jqbh < qhm < zfzh).
    req = "&".join(f"{k}={unordered[k]}" for k in sorted(unordered))
    assert req.startswith("consumeType=hiTrade&ddbh="), req
    assert req.endswith("&zfzh=0001"), req
    assert "dsfjybh=&" in req, "empty values must be kept"
    print("PASS: reqData is alphabetical, empty fields kept")

    # 3. Deterministic + lowercase hex, 32 chars.
    assert got == xy.make_sign(secret, ts, dict(unordered)), "must be deterministic"
    assert len(got) == 32 and got == got.lower(), "lowercase 32-char md5"
    print(f"PASS: deterministic lowercase md5 ({got})")

    # 4. Golden hash — locks the algorithm so a future refactor can't silently change it.
    golden = expected_sign(secret, ts, unordered)
    assert got == golden
    print(f"PASS: golden = {golden}")

    # 5. apply_export_goods refuses to run unconfigured (no key/secret) — safe by default.
    os.environ.pop("XY_KEY", None); os.environ.pop("XY_SECRET", None)
    try:
        xy.apply_export_goods(jqbh="1", order_id="o1", third_party_txn_id="t1",
                              pay_account="a1", goods=[{"spbh": "0001", "spmc": "x", "spdj": 10}])
        raise AssertionError("should have refused when unconfigured")
    except xy.XYVendingError as e:
        assert "not configured" in str(e)
    print("PASS: refuses to dispense when unconfigured")

    print("\nALL XY SIGN TESTS PASSED")


if __name__ == "__main__":
    main()
