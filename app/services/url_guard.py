"""SSRF guard for merchant-supplied outbound URLs (webhook endpoints).

A merchant's webhook_url is fetched by our Celery worker from inside Render's
network. With only a `^https?://` check, a merchant could point it at
`http://169.254.169.254/…` (cloud metadata), `http://localhost:6379` (our
Redis), or an internal Render host — a classic blind SSRF from a multi-tenant,
open-signup platform.

`is_public_http_url` resolves the host to its actual IP(s) and rejects anything
that is not globally routable. It is applied BOTH at save time (reject the URL)
and again at delivery time (a hostname can re-resolve to a private IP after it
was saved — DNS rebinding). Delivery must also use allow_redirects=False so a
302 to an internal URL is not followed.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # is_global is True only for globally-routable addresses; this rejects
    # private, loopback, link-local (incl. 169.254.169.254 metadata), reserved,
    # multicast, unspecified and CGNAT ranges in one check.
    return ip.is_global


def is_public_http_url(url: str | None) -> bool:
    """True only if url is http(s), has a hostname, and EVERY IP the hostname
    resolves to is globally routable. Fails closed on any error."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # A literal IP host is checked directly (no DNS needed).
    try:
        ipaddress.ip_address(host)
        return _ip_is_public(host)
    except ValueError:
        pass
    # Resolve the hostname; reject if ANY resolved address is non-public, so a
    # host that resolves to both a public and a private IP can't sneak through.
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    return all(_ip_is_public(a) for a in addrs)
