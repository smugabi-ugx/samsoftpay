"""Machine Integration Standard v1 — signing engine unit tests.

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_signing_profiles.py

Proves the vendor-neutral signing engine:
  [1] XY profile reproduces the exact MD5(secret+ts+reqData) the old code accepted
  [2] XY accepts the doc's non-alphabetical tksj/tkje ordering (the swap tolerance)
  [3] XY accepts the alternate status/state spelling
  [4] the CLEAN standard profile REJECTS XY's swapped order (no tolerances)
  [5] verify FAILS CLOSED: wrong secret, empty sign, and (in prod) no secret
  [6] replay window rejects a stale timestamp, accepts a fresh one
  [7] resolve_profile falls back to the built-in XY profile with no DB row
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"

from app.services import signing
from app.services.signing import XY_PROFILE, CLEAN_PROFILE


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


SECRET = "s3cr3t"
TS = "1724750000000"


def alpha_sign(secret, ts, scalars):
    base = "&".join(f"{k}={scalars[k]}" for k in sorted(scalars))
    return md5(f"{secret}{ts}{base}")


def main():
    # A representative §2.2.3 callback. splist is nested (excluded from the sign).
    payload = {
        "jqbh": "XY1", "shbh": "M1", "paytype": "forwardPayCode",
        "ddbh": "vnd_1", "dsfjybh": "txn_1", "status": "1",
        "tkje": "0", "tksj": "0", "timestamp": TS,
        "splist": [{"chsl": "1", "spbh": "0001"}],
    }
    # The signed base excludes the non-signed fields (splist nested, and
    # sign/key/timestamp) — timestamp is the MD5's 2nd element, not part of reqData.
    scalars = {k: v for k, v in payload.items()
               if k not in ("splist", "timestamp", "sign", "key")}

    # [1] a plain alphabetical sign verifies under the XY profile
    good = dict(payload, sign=alpha_sign(SECRET, TS, scalars))
    assert signing.verify(XY_PROFILE, SECRET, good), "[1] valid alpha sign rejected"
    print("[1] PASS — XY profile accepts the standard MD5(secret+ts+reqData)")

    # [2] the doc's non-alphabetical tksj-before-tkje ordering also verifies
    keys = sorted(scalars)
    ia, ib = keys.index("tkje"), keys.index("tksj")
    keys[ia], keys[ib] = keys[ib], keys[ia]
    swapped_base = "&".join(f"{k}={scalars[k]}" for k in keys)
    swap_sign = md5(f"{SECRET}{TS}{swapped_base}")
    assert signing.verify(XY_PROFILE, SECRET, dict(payload, sign=swap_sign)), "[2] swap order rejected"
    print("[2] PASS — XY accepts the doc's non-alphabetical tksj/tkje ordering")

    # [3] alternate status->state spelling verifies
    aliased = {("state" if k == "status" else k): v for k, v in scalars.items()}
    alias_sign = alpha_sign(SECRET, TS, aliased)
    assert signing.verify(XY_PROFILE, SECRET, dict(payload, sign=alias_sign)), "[3] alias rejected"
    print("[3] PASS — XY accepts the alternate status/state spelling")

    # [4] the CLEAN standard profile has NO tolerances — the swapped order fails
    assert signing.verify(CLEAN_PROFILE, SECRET, dict(payload, sign=good["sign"])), "[4a] clean alpha ok"
    assert not signing.verify(CLEAN_PROFILE, SECRET, dict(payload, sign=swap_sign)), "[4b] clean must reject swap"
    print("[4] PASS — CLEAN Standard v1 accepts only strict alphabetical (no swap)")

    # [5] fail closed
    assert not signing.verify(XY_PROFILE, "wrong", good), "[5a] wrong secret accepted"
    assert not signing.verify(XY_PROFILE, SECRET, dict(payload, sign="")), "[5b] empty sign accepted"
    os.environ["RENDER"] = "true"
    try:
        assert not signing.verify(XY_PROFILE, "", good), "[5c] no secret accepted in prod"
    finally:
        os.environ.pop("RENDER", None)
    print("[5] PASS — fails closed on wrong secret, empty sign, and no-secret-in-prod")

    # [6] replay window (a profile with a 60s window)
    windowed = signing.Profile(
        vendor="w", display_name="w", non_signed=CLEAN_PROFILE.non_signed,
        aliases={}, order="alpha", swaps=(), replay_window_seconds=60,
        dispense_path="/x", dispense_body_style="xy_orderdto",
        dispense_extra={}, is_legacy_shim=False)
    now_ms = int(TS) + 10_000            # 10s later — fresh
    assert signing.verify(windowed, SECRET, good, now_ms=now_ms), "[6a] fresh rejected"
    stale = int(TS) + 10 * 60 * 1000     # 10 min later — stale
    assert not signing.verify(windowed, SECRET, good, now_ms=stale), "[6b] stale accepted"
    print("[6] PASS — replay window accepts fresh, rejects stale")

    # [7] resolve_profile with no app context -> built-in XY
    p = signing.resolve_profile("xy")
    assert p.vendor == "xy" and p.is_legacy_shim, "[7a] xy builtin"
    assert signing.resolve_profile("totally-new-vendor").vendor == "_default", "[7b] unknown -> clean"
    print("[7] PASS — resolve_profile falls back to built-ins (xy legacy, else clean)")

    print("\nALL SIGNING-PROFILE ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
