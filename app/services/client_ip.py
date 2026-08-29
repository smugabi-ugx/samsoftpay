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

Hardening (audit HIGH): `CF-Connecting-IP` is only TRUSTED when the request
genuinely arrived through Cloudflare — otherwise a client hitting the raw
.onrender.com origin could forge that header and mint a fresh rate-limit bucket
per forged value (prompt/SMS-bombing a victim's phone via the unauthenticated
checkout). Behind Render, our own `remote_addr` is Render's proxy, not
Cloudflare — so the real signal is the RIGHTMOST X-Forwarded-For entry (appended
by Render, the trusted proxy): for Cloudflare traffic it is a Cloudflare EDGE
IP; for a direct-to-onrender hit it is the real client. We trust
CF-Connecting-IP only when that upstream is inside Cloudflare's published ranges.
"""
from __future__ import annotations

import ipaddress

from flask import has_request_context, request

# Cloudflare's published edge ranges (cloudflare.com/ips). Stable; update if CF
# changes them. Parsed once at import.
_CF_CIDRS = [ipaddress.ip_network(c) for c in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]


def _is_cloudflare_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _CF_CIDRS)


def real_client_ip() -> str:
    if not has_request_context():
        return "worker"
    fwd = request.headers.get("X-Forwarded-For")
    parts = [p.strip() for p in fwd.split(",") if p.strip()] if fwd else []
    upstream = parts[-1] if parts else (request.remote_addr or "")
    # Trust CF-Connecting-IP ONLY when the trusted proxy's upstream is a genuine
    # Cloudflare edge — otherwise the header is client-controlled and forgeable.
    cf = request.headers.get("CF-Connecting-IP")
    if cf and _is_cloudflare_ip(upstream):
        return cf.strip()
    # Direct/origin traffic (or a forged CF header): the rightmost XFF entry was
    # appended by the trusted proxy and is the real peer; never client-controlled.
    if parts:
        return parts[-1]
    return request.remote_addr or "unknown"
