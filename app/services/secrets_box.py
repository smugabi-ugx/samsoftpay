"""Reversible encryption for third-party credentials we must replay.

API keys we issue are HASHED — we only ever compare them. But a credential
belonging to someone ELSE (a merchant's XY Vending secret, say) has to be fed
back into their signing algorithm, so it must be recoverable. Hashing is wrong
here; plaintext in the database is worse. So: Fernet (AES-128-CBC + HMAC).

Key material, in order of preference:
  1. CREDENTIAL_ENCRYPTION_KEY — a urlsafe-base64 32-byte Fernet key.
     Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. Derived from SECRET_KEY, so a deployment that has not set the dedicated key
     still encrypts rather than storing plaintext.

CAUTION with the fallback: rotating SECRET_KEY makes every stored credential
undecryptable. Set CREDENTIAL_ENCRYPTION_KEY in production and rotate it
separately. `decrypt` returns None on any failure rather than raising, so a bad
or rotated key degrades to "credential not configured" instead of a 500 in the
middle of a payment.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    raw = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if raw:
        return Fernet(raw.encode())
    secret = os.environ.get("SECRET_KEY", "dev-only-do-not-use-in-prod")
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(derived)


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a credential for storage. Empty input stores as NULL."""
    if not plaintext:
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """Decrypt a stored credential, or None if absent/unreadable."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def mask(plaintext: str | None, keep: int = 4) -> str:
    """Render a credential for display without revealing it."""
    if not plaintext:
        return ""
    if len(plaintext) <= keep:
        return "•" * len(plaintext)
    return "•" * (len(plaintext) - keep) + plaintext[-keep:]
