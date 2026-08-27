"""Samsoftpay Python SDK (starter) — a thin wrapper over the Samsoftpay API.

    from samsoftpay import Samsoftpay
    sp = Samsoftpay(os.environ["SAMSOFTPAY_SECRET_KEY"],
                    webhook_secret=os.environ.get("SAMSOFTPAY_WEBHOOK_SECRET"))

    charge = sp.charges.create(amount=10000, channel="mtn_momo",
                               customer={"phone": "256700123456"}, reference="order-1")
    payout = sp.payouts.create(amount=50000, channel="mtn_momo",
                               recipient={"phone": "256780000001", "name": "Jane"}, reference="SAL-1")
    bal = sp.balance.get()

Same base URL for test and live — the key prefix (sk_test_/sk_live_) picks the mode.
X-Timestamp and a per-request Idempotency-Key are added to money POSTs automatically.
Depends only on `requests`.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid

import requests


class SamsoftpayError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class _Resource:
    def __init__(self, client):
        self._c = client


class Samsoftpay:
    def __init__(self, secret_key: str, *, base_url: str = "https://api.samsoftpay.com",
                 webhook_secret: str | None = None, timeout: int = 20):
        if not secret_key:
            raise ValueError("secret_key is required")
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.webhook_secret = webhook_secret
        self.timeout = timeout
        self._s = requests.Session()

        self.charges = self._ns(
            create=lambda **b: self._post("/v1/charges", b),
            get=lambda cid: self._get(f"/v1/charges/{cid}"),
            list=lambda **q: self._get("/v1/charges", q),
            refund=lambda cid: self._post(f"/v1/charges/{cid}/refund", {}),
        )
        self.payouts = self._ns(
            create=lambda **b: self._post("/v1/payouts", b),
            get=lambda pid: self._get(f"/v1/payouts/{pid}"),
            list=lambda **q: self._get("/v1/payouts", q),
            bulk=lambda **b: self._post("/v1/payouts/bulk", b),
        )
        self.scheduled_payouts = self._ns(
            create=lambda **b: self._post("/v1/scheduled-payouts", b),
            get=lambda sid: self._get(f"/v1/scheduled-payouts/{sid}"),
            list=lambda **q: self._get("/v1/scheduled-payouts", q),
        )
        self.balance = self._ns(get=lambda: self._get("/v1/balance"))
        self.statements = self._ns(
            get=lambda period: self._get(f"/v1/statements/{period}"),
            pdf_url=lambda period: f"{self.base_url}/v1/statements/{period}.pdf",
        )
        self.subaccounts = self._ns(create=lambda **b: self._post("/v1/subaccounts", b))
        self.payment_links = self._ns(create=lambda **b: self._post("/v1/payment-links", b))

    def resolve_account(self, phone: str):
        return self._get("/v1/resolve-account", {"phone": phone})

    def verify_webhook(self, raw_body, signature: str) -> bool:
        """Verify an inbound webhook. Pass the RAW body (bytes/str) + the
        X-Samsoftpay-Signature header. True if authentic."""
        if not self.webhook_secret:
            raise ValueError("webhook_secret not configured")
        body = raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body).encode()
        expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature or ""))

    # ---- internals ----
    @staticmethod
    def _ns(**methods):
        # SimpleNamespace stores callables as plain attributes (NOT bound
        # methods), so sp.charges.create(...) doesn't get an extra self arg.
        import types
        return types.SimpleNamespace(**methods)

    def _headers(self, post: bool):
        h = {"Authorization": f"Bearer {self.secret_key}"}
        if post:
            h["Content-Type"] = "application/json"
            h["X-Timestamp"] = str(int(time.time()))
            h["Idempotency-Key"] = str(uuid.uuid4())
        return h

    def _get(self, path, params=None):
        return self._req("GET", path, params=params)

    def _post(self, path, body):
        return self._req("POST", path, json=body)

    def _req(self, method, path, *, params=None, json=None):
        try:
            r = self._s.request(method, self.base_url + path,
                                headers=self._headers(method == "POST"),
                                params=params, json=json, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SamsoftpayError(str(exc)) from exc
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400:
            raise SamsoftpayError(data.get("error", f"HTTP {r.status_code}"), r.status_code, data)
        return data
