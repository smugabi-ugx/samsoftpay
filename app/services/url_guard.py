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
from urllib.parse import urlparse, urlunparse


class SsrfBlocked(Exception):
    """Raised by safe_post when a URL does not resolve to a public address."""


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


# Hosts that belong to THIS platform. A merchant webhook pointed at our OWN API
# host is not an SSRF (api.samsoftpay.com is a perfectly public address, so the
# SSRF guard above waves it through) — it is a self-delivery loop: we POST the
# event to ourselves, the merchant's real receiver never sees it, and every
# attempt 404s. Backbone hit exactly this: they saved
# https://api.samsoftpay.com/webhooks/samsoftpay and we retried against
# ourselves for two days. Reject it at save time.
_OWN_HOST_SUFFIXES = ("samsoftpay.com", "samsoftpay.onrender.com")


def is_self_addressed_url(url: str | None, extra_hosts=()) -> bool:
    """True if url's host is one of Samsoftpay's own platform hosts (so delivering
    to it would POST to ourselves). Matches the exact host or any subdomain of a
    known own-domain, case-insensitively. `extra_hosts` lets a caller add the
    configured BASE_URL host for environments on a different domain."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    candidates = set(_OWN_HOST_SUFFIXES) | {
        str(h).lower().rstrip(".") for h in extra_hosts if h
    }
    return any(host == c or host.endswith("." + c) for c in candidates)


def _resolve_public_ip(host: str, port: int | None, scheme: str) -> str | None:
    """Resolve host to ONE validated globally-routable IP, or None. Fail-closed."""
    try:
        ipaddress.ip_address(host)               # literal IP — use directly
        return host if _ip_is_public(host) else None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(
            host, port or (443 if scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    addrs = [info[4][0] for info in infos]
    # Reject if ANYTHING resolves non-public (a split public/private answer).
    if not addrs or not all(_ip_is_public(a) for a in addrs):
        return None
    return addrs[0]


def safe_post(url: str, **kwargs):
    """POST to a merchant-supplied URL with the resolved public IP PINNED.

    Closes the DNS-rebinding TOCTOU that a save-time/deliver-time re-resolution
    left open: the address we VALIDATE is the exact address we CONNECT to, so an
    attacker-controlled DNS name cannot answer 'public' to the guard and
    169.254.169.254 / an internal host to the actual request. The original
    hostname is preserved for the Host header and TLS SNI/cert verification, so
    genuine HTTPS delivery is unchanged. Fail-closed: any non-public/unresolvable
    host raises SsrfBlocked before a byte is sent. allow_redirects defaults False.
    """
    import requests
    from requests.adapters import HTTPAdapter

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SsrfBlocked("url must be http(s) with a hostname")
    ip = _resolve_public_ip(parsed.hostname, parsed.port, parsed.scheme)
    if ip is None:
        raise SsrfBlocked(f"host {parsed.hostname!r} does not resolve to a public address")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip_host = f"[{ip}]" if ":" in ip else ip                 # bracket IPv6
    pinned_url = urlunparse(parsed._replace(netloc=f"{ip_host}:{port}"))

    headers = dict(kwargs.pop("headers", None) or {})
    headers["Host"] = parsed.hostname                        # original vhost
    kwargs.setdefault("allow_redirects", False)

    class _SNIAdapter(HTTPAdapter):
        """Keep TLS SNI + cert hostname = the real name, though we dialled an IP."""
        def init_poolmanager(self, *a, **kw):
            kw["server_hostname"] = parsed.hostname
            kw["assert_hostname"] = parsed.hostname
            return super().init_poolmanager(*a, **kw)

    session = requests.Session()
    if parsed.scheme == "https":
        session.mount("https://", _SNIAdapter())
    try:
        return session.post(pinned_url, headers=headers, **kwargs)
    finally:
        session.close()
