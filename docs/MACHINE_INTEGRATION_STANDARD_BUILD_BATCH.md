# BUILD BATCH — Machine Integration Standard v1 (config-driven signing profiles + outbound binding + conformance)

> Status: **DRAFT for Sam's review — do not build until approved.**
> Author: Claude (Opus 4.8). Date: 27 Aug 2026.
> Companion: guardrails 9, 11, 14, 15 in CLAUDE.md; memory `xy-vending-callback-hardening`.

---

## (1) Summary — what and why

Today the vending machine loop is **hardcoded to XY**. Two files carry the coupling:

- **Inbound** `app/routes/webhooks_xy.py` — the §2.2.3 dispense-result callback verifier bakes XY's
  quirks in as constants: `_NON_SIGNED`, `_ALIASES` (`status`/`state`, `dsfshdh`/`dsfshbh`), and the
  alphabetical / `tksj`-`tkje`-swapped sign bases. Every new machine vendor = an edit to this file.
- **Outbound** `app/services/xy_vending.py` — `apply_export_goods` hardcodes XY's endpoint path
  (`/service-pay-third/third/pay/api/ApplyExportGoods`), XY's `orderDTO`/`consumeType` body, and XY's
  `MD5(secret+timestamp+reqData)` sign. A machine that speaks a different dispense protocol can't be
  driven at all.

**The move:** make the integration surface **configuration, not code**. A `SigningProfile` (one row per
vendor, editable in config) describes how a vendor signs and what its dispense command looks like. ONE
generic verifier + ONE generic outbound driver read the profile. XY becomes *the first profile*, byte-for-byte
identical to today. A new vendor is a profile row + a passed conformance sample — **zero code**.

**Non-negotiable invariants preserved** (regression-locked):
- Guardrail 11: no payment, no dispense; one SUCCEEDED charge → one dispense (atomic consume).
- Guardrail 9: inbound callback verification FAILS CLOSED in production.
- Guardrail 14/15: simulated rails / test-mode scoping untouched.
- The QR / hosted-checkout / payment-links / payouts code paths are **not touched** (Modes A & B, see
  `/docs#integration-modes`). This batch is entirely inside Mode C (machine-present vending).

**Scope boundary — what this batch does NOT do:** it does not add a second *payment* rail, does not change
the ledger, and does not touch `create_charge`/`create_payout`. It only generalises the machine dispense
command + its result callback.

---

## (2) Data model — the `SigningProfile`

### FILE 1 — `app/models/__init__.py` (EDIT: append after `VendingMachine`)

```python
class SigningProfile(db.Model):
    """How ONE machine vendor signs its callbacks and receives dispense commands.

    Replaces the hardcoded XY constants in webhooks_xy.py / xy_vending.py. A new
    vendor is a row here (+ a passed conformance sample), not a code change. XY is
    seeded as the first profile, reproducing today's behaviour exactly.

    Signing is always MD5(secret + timestamp + reqData); the profile only varies
    the KNOBS a vendor's firmware actually differs on. It never lets a vendor turn
    verification OFF (guardrail 9).
    """
    __tablename__ = "signing_profiles"
    id = Column(Integer, primary_key=True)
    # Vendor key, e.g. "xy". Unique, immutable, referenced by machines + creds.
    vendor = Column(String(40), nullable=False, unique=True, index=True)
    display_name = Column(String(120), nullable=False)

    # ---- INBOUND callback verification knobs ----
    # Fields NEVER included in the signed base (JSON list of strings).
    non_signed_fields = Column(Text, nullable=False, default='["sign","key","timestamp"]')
    # Accepted key spellings the vendor doc contradicts itself on: {"canonical":"alt"} pairs.
    field_aliases = Column(Text, nullable=False, default="{}")
    # Ordering strategy for reqData: "alpha" (default) or "alpha_swap" with pairs below.
    sign_order = Column(String(20), nullable=False, default="alpha")
    # For "alpha_swap": JSON list of [a,b] key pairs swapped relative to alphabetical.
    sign_order_swaps = Column(Text, nullable=False, default="[]")
    # Reject callbacks whose 13-digit ms timestamp is older than this many seconds
    # (0 = no freshness check — for a vendor that does not send a real send-time).
    replay_window_seconds = Column(Integer, nullable=False, default=0)

    # ---- OUTBOUND dispense command knobs ----
    # HTTP path appended to the vendor base URL for the dispense command.
    dispense_path = Column(String(255), nullable=False,
                           default="/service-pay-third/third/pay/api/ApplyExportGoods")
    # Body template selector: "xy_orderdto" (today's shape) | "flat" | custom builder key.
    dispense_body_style = Column(String(40), nullable=False, default="xy_orderdto")
    # Static extra top-level body fields as JSON (e.g. {"consumeType":"hiTrade"}).
    dispense_extra = Column(Text, nullable=False, default="{}")

    # Whether this profile is a retirable legacy shim (XY today) or the clean standard.
    is_legacy_shim = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
```

`Merchant`/`VendingMachine` gain an optional `signing_profile_vendor` (String(40), default `"xy"`) so a
machine resolves its profile. Default `"xy"` keeps every existing machine on the seeded XY profile.

---

## (3) Generic verifier — refactor `webhooks_xy.py`

### FILE 2 — `app/services/signing.py` (CREATE)

Pure functions, no Flask, unit-testable in isolation:

```python
def sign_bases(profile, params: dict) -> list[str]:
    """Every reqData base string this profile could produce for a payload."""
    # 1. drop non_signed_fields + nested (list/dict) values
    # 2. build the alphabetical base
    # 3. if sign_order == "alpha_swap": also emit each pairwise-swapped base
    # 4. if field_aliases: repeat 2-3 for the aliased spelling
    ...

def candidate_signs(profile, secret: str, timestamp: str, payload: dict) -> set[str]:
    return {md5(f"{secret}{timestamp}{b}") for b in sign_bases(profile, payload)}

def verify(profile, secret: str, payload: dict, *, now_ms: int | None) -> bool:
    """FAILS CLOSED. Enforces replay_window_seconds when > 0."""
    ...
```

### FILE 3 — `app/routes/webhooks_xy.py` (EDIT)

Keep the blueprint + route + payload interpretation (`_dispensed_count`, `_find_order`, `_finish`, the
`status`/`state` tolerance for *reading*). Replace the inline `_sign_base`/`_candidate_signs`/`_verify` with
a call into `signing.verify(profile, secret, payload, now_ms=...)`, where `profile` is resolved from the
matched order's machine (fallback to the seeded `"xy"` profile). **The XY profile's knobs reproduce today's
constants exactly**, so `tests/test_xy_dispense_callback.py` (11 checks) must stay green UNCHANGED. Add the
replay guard: when `profile.replay_window_seconds > 0`, reject stale/duplicate callbacks (Redis dedupe on
`(vendor, ddbh, timestamp)`, fail-open on Redis-down like `alerts.send_alert`). XY seeds
`replay_window_seconds=0` initially (its firmware's timestamp semantics unconfirmed — memory
`xy-vending-callback-hardening` item 2); flip to 300 once XY sends a certified sample.

---

## (4) Generic outbound driver — refactor `xy_vending.py`

### FILE 4 — `app/services/dispense_driver.py` (CREATE)

```python
def command_dispense(*, profile, creds, jqbh, order_id, third_party_txn_id,
                     pay_account, goods) -> dict:
    """Build + sign + POST the dispense command per the profile. Raises
    DispenseError (renamed alias of XYVendingError for back-compat) on failure."""
    body = _build_body(profile.dispense_body_style, ...)   # "xy_orderdto" == today
    body.update(json.loads(profile.dispense_extra))
    body["sign"] = md5(creds.secret + ts + _req_data(profile, body))
    return _post(profile.dispense_path, body, creds)
```

`xy_vending.apply_export_goods` becomes a thin shim that resolves the `"xy"` profile and calls
`dispense_driver.command_dispense`, so `app/services/vending.py` and `app/routes/api.py` need **no change**
and `tests/test_vending_flow.py` (18) + `tests/test_xy_vending_sign.py` stay green. The `xy_orderdto`
body builder is a line-for-line move of today's `apply_export_goods` body construction — verified by the
existing golden-sign test (`2bc88034cf39a0c173783f161eea8e59`).

---

## (5) Self-serve conformance endpoint

### FILE 5 — `app/routes/api.py` (EDIT: append after the vending block)

```
POST /v1/vending/conformance      # full key; merchant's own vendor profile
  body: { "payload": {...the exact callback JSON...}, "timestamp": "<13-digit ms>",
          "sign": "<what your firmware produced>" }
  200:  { "ok": true,  "matched_base": "alpha", "note": "signature verifies against your profile" }
  200:  { "ok": false, "expected_any_of": ["<base1>","<base2>"],
                        "hint": "your reqData does not match; check field ordering / which fields you sign" }
```

It runs `signing.candidate_signs(profile, merchant_xy_secret, timestamp, payload)` and reports whether the
vendor's `sign` is in the set — **no money, no state change, no dispense**. This is the "certify against us"
gate: a vendor iterates here until green before go-live. Rate-limited; full key only (it reveals which bases
we accept, so not for an anonymous caller). Mirrored in the docs as the vendor sign-off step.

---

## (6) Migration — `migrations/versions/<rev>_signing_profiles.py` (CREATE)

- `create_table("signing_profiles", ...)` with the columns above.
- `add_column("merchants", signing_profile_vendor)` + `add_column("vending_machines", signing_profile_vendor)`,
  both `server_default="xy"` (backfills every existing row onto the XY profile — zero behaviour change).
- **Data migration: seed the XY profile** with knobs that reproduce today's constants exactly:
  `non_signed_fields=["sign","key","timestamp","splist"]`, `field_aliases={"status":"state","dsfshdh":"dsfshbh"}`,
  `sign_order="alpha_swap"`, `sign_order_swaps=[["tkje","tksj"]]`, `replay_window_seconds=0`,
  `dispense_path="/service-pay-third/third/pay/api/ApplyExportGoods"`, `dispense_body_style="xy_orderdto"`,
  `dispense_extra={"consumeType":"hiTrade"}`, `is_legacy_shim=True`.
- Downgrade drops the columns + table. Chain from the current head; update CLAUDE.md's head marker.

---

## (7) Money-safety / guard checklist (must all hold after the batch)

1. **Guardrail 11** — `POST /v1/vending/dispense` + `maybe_dispense_on_success` still gate on a SUCCEEDED
   charge and the atomic consume. The driver refactor is downstream of the gate; it cannot bypass it.
2. **Guardrail 9** — `signing.verify` FAILS CLOSED: no profile / no secret / no match ⇒ reject in production.
   A profile can NEVER set "verification off" — there is no such knob.
3. **Guardrail 14/15** — no change to rail simulation or `is_test` scoping.
4. **No new money endpoint** — conformance moves no money and touches no ledger row.
5. **Legacy isolation** — XY's quirks live ONLY in the seeded `is_legacy_shim=True` row; the clean-standard
   default knobs (`sign_order="alpha"`, no aliases, `replay_window_seconds=300`) are what a NEW vendor gets.
6. **Zero behaviour change for XY** — proven by the three existing XY tests staying green unmodified.

---

## (8) Offline test plan (script-style, mock rails, temp-file sqlite)

- `tests/test_signing_profiles.py` (NEW): `signing.sign_bases`/`candidate_signs`/`verify` unit tests — the
  XY profile reproduces the golden hash; the clean-standard profile rejects XY's swapped order; replay window
  rejects a stale timestamp and a duplicate; fail-closed on unknown profile.
- `tests/test_conformance_endpoint.py` (NEW): a correctly-signed sample → `ok:true`; a wrong-order sample →
  `ok:false` with `expected_any_of`; no money moved; full-key-only (collections key 403? — it's read-only, so
  allow, but decide in §10).
- **Regression (must stay green UNCHANGED):** `test_xy_dispense_callback.py` (11), `test_vending_flow.py` (18),
  `test_xy_vending_sign.py`, `test_vending_dispense_guard.py` (5), `test_end_to_end.py`, `test_settlement_sweep.py`.

---

## (9) Docs deliverables (part of the batch, not after)

1. Promote `docs/Samsoftpay_XY_Vending_Integration.*` → **`Machine_Integration_Standard_v1.*`**: same content,
   re-headed as the vendor-neutral standard, XY shown as "profile: xy (legacy)". Regenerate the PDF.
2. In-app `/docs#vending`: add a "Bring your own machine" subsection — the profile knobs, the
   `POST /v1/vending/conformance` self-serve flow, and "nothing goes live until a sample passes".
3. `docs.md` mirror updated in lockstep.

---

## (10) Open decisions for Sam (answer before build)

1. **Profile editing surface:** dashboard UI now, or CLI-only (`flask signing-profile <vendor> --set ...`) to
   start? (Recommend: CLI + a read-only dashboard view first; full UI later.)
2. **Conformance key scope:** allow a collections-only key to call `/v1/vending/conformance` (it's read-only),
   or require a full key? (Recommend: allow collections — a kiosk vendor holds only that.)
3. **XY replay window:** seed `0` now and flip to `300` once XY confirms a fresh ms timestamp, or push XY to
   confirm before this batch ships? (Recommend: seed `0`, flip on their sample — don't block the batch.)
4. **Retire the XY shim when?** Keep `is_legacy_shim=True` until XY re-flashes firmware to the clean standard,
   then delete the row's aliases/swaps. (No code change to retire — just tighten the row.)

---

*Build order: FILE 1 → migration → FILE 2 → FILE 3 → FILE 4 → FILE 5 → tests → docs. Each step keeps the
existing XY tests green before moving on. Ship behind the existing vending kill switch; XY unaffected throughout.*
