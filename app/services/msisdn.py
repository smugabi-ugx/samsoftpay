"""MSISDN + free-text normalization for the MTN rails.

Two production-only failure modes the sandbox masked (sourced from MTN's own
common-errors KB during production-onboarding research):

1. MSISDNs must carry the country code. A customer typing "0772123456" went to
   MTN as-is and fails with PAYER_NOT_FOUND / PAYEE_NOT_FOUND in production.
   normalize_msisdn converts a local Ugandan format to 256XXXXXXXXX, strips
   +/spaces/punctuation, and passes anything else through untouched (sandbox
   test MSISDNs like 46733123450, and the magic TEST_PHONE_OUTCOMES numbers,
   are unaffected — matching is on the last 9 digits either way).

2. payerMessage / payeeNote reject "special" characters — an APOSTROPHE
   (e.g. a reference of "St. Peter's") 400-rejects the whole charge — and are
   capped at 160 chars. sanitize_note keeps a conservative charset and
   truncates; it never returns an empty string.
"""
from __future__ import annotations

import re

_UG_CC = "256"


def normalize_msisdn(raw: str | None) -> str:
    """Digits-only MSISDN with the Ugandan country code expanded.

    "0772 123 456" -> "256772123456"; "+256772123456" -> "256772123456";
    "00256772123456" -> "256772123456". Anything already international (or too
    short/odd to be a Ugandan local number) passes through digits-only — we
    normalize the common case, we do not invent numbers.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    # Local Ugandan format: 0 + 9 digits (07XXXXXXXX / 04XXXXXXXX ...).
    if digits.startswith("0") and len(digits) == 10:
        digits = _UG_CC + digits[1:]
    return digits


def sanitize_note(text: str | None, *, max_len: int = 160,
                  fallback: str = "Payment") -> str:
    """A payerMessage/payeeNote MTN will accept: conservative charset, <=160.

    Replaces every character outside [A-Za-z0-9 ._-] with a space (apostrophes
    are a confirmed production rejection), collapses whitespace, truncates.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 ._\-]", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or fallback)[:max_len]
