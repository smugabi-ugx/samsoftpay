"""The ONE way to identify a client IP behind Cloudflare + Render.

Sovereign-audit finding (HIGH): rate limiting keyed on `request.remote_addr`,
which behind Render's proxy is the PROXY hop — every visitor shared one
bucket, so the first 5 disputes/hour (or 10 logins/minute) platform-wide
exhausted the limit for every legitimate customer. Meanwhile the old helpers
took the FIRST X-Forwarded-For entry, which is CLIENT-CONTROLLED (proxies
append; an attacker's own header survives at position 0) — spoofable in audit
logs and lockout keys.

Resolution order:
  1. `CF-Connecting-IP` — set by Cloudflare, which fronts api.samsoftpay.com;
     this is the real client for all production traffic.
  2. The RIGHTMOST X-Forwarded-For entry — appended by the immediate trusted
     proxy (Render), never client-controlled. For direct-to-onrender.com
     traffic this is the true peer; for Cloudflare traffic it degrades to a
     per-CF-edge bucket, which header 1 already handles.
  3. `remote_addr` (local dev — no proxy at all).

Known residual: a client hitting the .onrender.com URL directly can forge
CF-Connecting-IP to dodge per-IP rate limits (never to hit someone else's
lockout — those also key on the account). Acceptable: strictly better than
the shared-bucket state, without maintaining Cloudflare IP-range lists.
"""
from __future__ import annotations

from flask import has_request_context, request


def real_client_ip() -> str:
    if not has_request_context():
        return "worker"
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "unknown"
