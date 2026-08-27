"""Machine Integration Standard v1 — vendor-neutral signing engine.

One place that knows how a machine vendor signs its dispense-result callback,
driven by a `Profile` instead of hardcoded per-vendor constants. XY Vending is
the first (legacy) profile; a new vendor is a profile row + a passed conformance
sample, NOT a code change (memory: xy-vending-callback-hardening).

Signing is ALWAYS `MD5(secret + timestamp + reqData)`. A profile only varies the
knobs a vendor's firmware actually differs on — which fields are signed, the key
ordering, spelling aliases the vendor doc contradicts itself on, and a replay
window. It can NEVER turn verification off: `verify()` fails closed (guardrail 9).

The XY profile reproduces the previous hardcoded `webhooks_xy` constants exactly,
so the existing XY tests pass unchanged. XY works from the built-in default with
NO database row and NO migration dependency; a `SigningProfile` row (seeded by the
migration, editable via `flask signing-profile`) overrides the built-in when
present, and is how NEW vendors are added.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    vendor: str
    display_name: str
    non_signed: frozenset          # fields excluded from the signature base
    aliases: dict                  # {canonical_spelling: alternate_spelling}
    order: str                     # "alpha" | "alpha_swap"
    swaps: tuple                   # for "alpha_swap": ((a, b), ...) key pairs swapped
    replay_window_seconds: int     # 0 = no freshness check
    dispense_path: str             # outbound dispense-command HTTP path
    dispense_body_style: str       # body builder selector, e.g. "xy_orderdto"
    dispense_extra: dict           # static extra body fields, e.g. {"consumeType": "hiTrade"}
    is_legacy_shim: bool           # True = a retirable per-vendor compatibility profile


def _s(v) -> str:
    """Scalar -> string, matching xy_vending._str (bool -> '1'/'0', None -> '')."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _base_alpha(params: dict) -> str:
    return "&".join(f"{k}={_s(params[k])}" for k in sorted(params))


def _base_swapped(params: dict, a: str, b: str) -> str:
    """Sorted-except-that-one-pair-swapped (XY v1.41's non-alphabetical example)."""
    keys = sorted(params)
    if a in keys and b in keys:
        ia, ib = keys.index(a), keys.index(b)
        keys[ia], keys[ib] = keys[ib], keys[ia]
    return "&".join(f"{k}={_s(params[k])}" for k in keys)


def sign_bases(profile: Profile, payload: dict) -> list[str]:
    """Every reqData base string this profile could produce for a payload.

    Scalars only (nested list/dict values — e.g. XY's `splist` — are excluded by
    listing them in `non_signed`). For each spelling variant (documented +
    aliased) we emit the alphabetical base and, for "alpha_swap", the swapped
    bases too. Bounded and additive: more accepted forms, never a weaker secret.
    """
    scalars = {k: v for k, v in payload.items()
               if k not in profile.non_signed and not isinstance(v, (dict, list))}
    variants = [scalars]
    aliased = {profile.aliases.get(k, k): v for k, v in scalars.items()}
    if aliased != scalars:
        variants.append(aliased)

    bases: list[str] = []
    for variant in variants:
        bases.append(_base_alpha(variant))
        if profile.order == "alpha_swap":
            for a, b in profile.swaps:
                bases.append(_base_swapped(variant, a, b))
    return bases


def candidate_signs(profile: Profile, secret: str, timestamp: str, payload: dict) -> set[str]:
    return {hashlib.md5(f"{secret}{timestamp}{b}".encode("utf-8")).hexdigest().lower()
            for b in sign_bases(profile, payload)}


def verify(profile: Profile, secret: str, payload: dict, *, now_ms: int | None = None) -> bool:
    """Verify a callback signature. FAILS CLOSED when a secret is configured.

    A profile with `replay_window_seconds > 0` also rejects a callback whose
    13-digit ms `timestamp` is outside the window (stale / replayed). Pass the
    current epoch-ms as `now_ms` to enable that check; omit it to skip freshness
    (the caller decides, so offline tests need no clock).
    """
    if not secret:
        # No credentials for this merchant/vendor. In production that is a hard
        # stop — never accept an unverifiable call that can change money state.
        return not os.environ.get("RENDER")
    supplied = str(payload.get("sign") or "").strip().lower()
    if not supplied:
        return False
    ts = _s(payload.get("timestamp", ""))
    if profile.replay_window_seconds > 0 and now_ms is not None:
        try:
            cts = int(ts)
        except (TypeError, ValueError):
            return False
        if abs(now_ms - cts) > profile.replay_window_seconds * 1000:
            return False
    return supplied in candidate_signs(profile, secret, ts, payload)


# ---------------------------------------------------------------------------
# Built-in profiles. XY reproduces the old webhooks_xy constants byte-for-byte.
# CLEAN is what a NEW vendor gets: strict alphabetical, no spelling aliases, a
# 5-minute replay window — the Machine Integration Standard v1 with no tolerances.
# ---------------------------------------------------------------------------

XY_PROFILE = Profile(
    vendor="xy",
    display_name="XY Vending (legacy compatibility)",
    non_signed=frozenset({"sign", "key", "timestamp", "splist"}),
    aliases={"status": "state", "dsfshdh": "dsfshbh"},
    order="alpha_swap",
    swaps=(("tkje", "tksj"),),
    replay_window_seconds=0,          # XY's timestamp semantics unconfirmed; seed 0, flip to 300 on a certified sample
    dispense_path="/service-pay-third/third/pay/api/ApplyExportGoods",
    dispense_body_style="xy_orderdto",
    dispense_extra={"consumeType": "hiTrade"},
    is_legacy_shim=True,
)

CLEAN_PROFILE = Profile(
    vendor="_default",
    display_name="Machine Integration Standard v1",
    non_signed=frozenset({"sign", "key", "timestamp"}),
    aliases={},
    order="alpha",
    swaps=(),
    replay_window_seconds=300,
    dispense_path="/service-pay-third/third/pay/api/ApplyExportGoods",
    dispense_body_style="xy_orderdto",
    dispense_extra={"consumeType": "hiTrade"},
    is_legacy_shim=False,
)

_BUILTIN = {"xy": XY_PROFILE}


def _from_row(row) -> Profile:
    import json
    return Profile(
        vendor=row.vendor,
        display_name=row.display_name,
        non_signed=frozenset(json.loads(row.non_signed_fields or "[]")),
        aliases=json.loads(row.field_aliases or "{}"),
        order=row.sign_order or "alpha",
        swaps=tuple(tuple(p) for p in json.loads(row.sign_order_swaps or "[]")),
        replay_window_seconds=int(row.replay_window_seconds or 0),
        dispense_path=row.dispense_path,
        dispense_body_style=row.dispense_body_style,
        dispense_extra=json.loads(row.dispense_extra or "{}"),
        is_legacy_shim=bool(row.is_legacy_shim),
    )


def resolve_profile(vendor: str | None) -> Profile:
    """The signing profile for a vendor.

    A `SigningProfile` DB row wins when present (this is how a vendor is edited
    and how new vendors are added); otherwise the built-in default is used, so XY
    keeps working with no row and no migration. An unknown vendor falls back to
    the strict CLEAN standard (never to a permissive one).
    """
    v = (vendor or "xy").strip().lower()
    try:
        from ..models import SigningProfile
        row = SigningProfile.query.filter_by(vendor=v).one_or_none()
        if row is not None:
            return _from_row(row)
    except Exception:
        # No app/DB context, or table not migrated yet — fall back to built-ins.
        pass
    return _BUILTIN.get(v) or CLEAN_PROFILE
