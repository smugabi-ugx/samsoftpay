"""Outbound webhook URLs must not be SSRF vectors (security-3).

A merchant webhook_url is fetched by our worker from inside the cloud network.
With only a `^https?://` check, a merchant could point it at cloud metadata
(169.254.169.254), our own Redis (localhost), or an internal host. The guard
resolves the host and rejects any non-globally-routable target, at save AND at
delivery time.

What this proves:
  1. Private / loopback / link-local / metadata / non-http URLs are rejected.
  2. A normal public https URL is accepted.
  3. Signup drops a private webhook_url instead of storing it.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="ssrf_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant
from app.services.url_guard import is_public_http_url

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://127.0.0.1:6379",                        # our Redis
    "http://localhost/hook",                        # loopback name
    "http://10.0.0.5/hook",                         # private
    "http://192.168.1.10/hook",                     # private
    "http://172.16.0.9/hook",                       # private
    "http://[::1]/hook",                            # ipv6 loopback
    "ftp://example.com/hook",                       # non-http scheme
    "not-a-url",                                     # garbage
    "https://this-host-does-not-exist-xyz123.invalid/hook",  # unresolvable
    "",                                             # empty
]

ALLOWED = [
    "https://example.com/webhooks",                 # a real public host
    "https://8.8.8.8/hook",                         # public literal IP
]


def main():
    for u in BLOCKED:
        check(f"blocked: {u or '(empty)'}", is_public_http_url(u) is False)
    for u in ALLOWED:
        check(f"allowed: {u}", is_public_http_url(u) is True)

    # Signup must drop a private webhook rather than persist it.
    app = create_app({"WTF_CSRF_ENABLED": False})
    with app.app_context():
        db.create_all()
    c = app.test_client()
    c.post("/signup", data={
        "name": "SSRF Co", "email": "ssrf@x.com", "password": "Password123",
        "webhook_url": "http://169.254.169.254/steal",
    }, follow_redirects=False)
    with app.app_context():
        m = Merchant.query.filter_by(email="ssrf@x.com").first()
        check("signup created the merchant", m is not None)
        check("signup dropped the private webhook_url", m is not None and not m.webhook_url)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL WEBHOOK-SSRF TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
