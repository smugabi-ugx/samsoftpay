"""MSISDN normalization + note sanitization (MTN production constraints).

Two production-only failure modes the sandbox masked (MTN's own KB):
a local "0772..." number fails PAYER/PAYEE_NOT_FOUND without the country code,
and an apostrophe in payerMessage/payeeNote 400-rejects the whole charge.

What this proves:
  1. Local Ugandan formats normalize to 256XXXXXXXXX (0-prefix, +256, 00256, spaces/dashes).
  2. Already-international and sandbox test MSISDNs pass through untouched.
  3. The magic TEST_PHONE_OUTCOMES last-9-digit matching is unaffected.
  4. Notes: apostrophes/specials stripped, whitespace collapsed, 160-char cap,
     never empty.
  5. Both MTN adapters import and use the helpers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.services.msisdn import normalize_msisdn, sanitize_note

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    # 1. Local formats -> country-coded.
    check("0772123456 -> 256772123456", normalize_msisdn("0772123456") == "256772123456")
    check("'0772 123 456' -> 256772123456", normalize_msisdn("0772 123 456") == "256772123456")
    check("0772-123-456 -> 256772123456", normalize_msisdn("0772-123-456") == "256772123456")
    check("+256772123456 -> 256772123456", normalize_msisdn("+256772123456") == "256772123456")
    check("00256772123456 -> 256772123456", normalize_msisdn("00256772123456") == "256772123456")

    # 2. Already-international / sandbox numbers untouched.
    check("256772123456 unchanged", normalize_msisdn("256772123456") == "256772123456")
    check("sandbox 46733123450 unchanged", normalize_msisdn("46733123450") == "46733123450")
    check("magic 256700000001 unchanged", normalize_msisdn("256700000001") == "256700000001")
    check("short junk passes through digits-only", normalize_msisdn("12ab34") == "1234")
    check("empty -> empty", normalize_msisdn(None) == "")

    # 3. Last-9 matching preserved (guardrail 16): local and international forms
    #    of the same magic number keep the same last 9 digits.
    a = normalize_msisdn("0700000001")
    b = normalize_msisdn("256700000001")
    check("magic number last-9 identical across formats", a[-9:] == b[-9:] == "700000001")

    # 4. Notes.
    check("apostrophe stripped", sanitize_note("St. Peter's School") == "St. Peter s School")
    check("specials stripped + collapsed",
          sanitize_note("Order#42 @Shop!!") == "Order 42 Shop")
    check("dot/underscore/hyphen kept", sanitize_note("ref_ab.c-d") == "ref_ab.c-d")
    check("160-char cap", len(sanitize_note("x" * 500)) == 160)
    check("never empty (fallback)", sanitize_note("!!!") == "Payment")
    check("None -> fallback", sanitize_note(None) == "Payment")

    # 5. Adapters actually use the helpers.
    import inspect
    from app.services import rails_mtn_real, rails_mtn_disbursement
    src_c = inspect.getsource(rails_mtn_real.RealMTNMoMoAdapter.initiate) \
        if hasattr(rails_mtn_real, "RealMTNMoMoAdapter") else inspect.getsource(rails_mtn_real)
    src_d = inspect.getsource(rails_mtn_disbursement.RealMTNMoMoDisbursementAdapter.initiate)
    check("collections adapter uses normalize+sanitize",
          "normalize_msisdn" in src_c and "sanitize_note" in src_c)
    check("disbursement adapter uses normalize+sanitize",
          "normalize_msisdn" in src_d and "sanitize_note" in src_d)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL MSISDN/NOTE TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
