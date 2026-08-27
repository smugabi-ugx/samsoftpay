# Samsoftpay Python SDK

A thin wrapper over the [Samsoftpay API](https://api.samsoftpay.com/docs). Depends only on `requests`.

```bash
pip install requests   # then copy samsoftpay.py into your project (or pip install samsoftpay once published)
```

```python
import os
from samsoftpay import Samsoftpay

sp = Samsoftpay(os.environ["SAMSOFTPAY_SECRET_KEY"],
                webhook_secret=os.environ.get("SAMSOFTPAY_WEBHOOK_SECRET"))

# Collect
charge = sp.charges.create(amount=10000, currency="UGX", channel="mtn_momo",
                           customer={"phone": "256700123456"}, reference="order-1")
charge = sp.charges.get(charge["id"])

# Pay out
payout = sp.payouts.create(amount=50000, channel="mtn_momo",
                           recipient={"phone": "256780000001", "name": "Jane"}, reference="SAL-1")

# Recurring payroll
sp.scheduled_payouts.create(amount=500000, interval="monthly",
                            recipients=[{"phone": "256780000001", "name": "Jane"}])

# Reconcile
bal = sp.balance.get()
stmt = sp.statements.get("2026-08")
pdf_url = sp.statements.pdf_url("2026-08")
```

`X-Timestamp` and a per-request `Idempotency-Key` are added to money POSTs automatically. The key
prefix (`sk_test_`/`sk_live_`) picks the mode; same base URL for both.

## Verify webhooks (Flask)

```python
from flask import request, abort

@app.post("/webhooks/samsoftpay")
def hook():
    if not sp.verify_webhook(request.get_data(), request.headers.get("X-Samsoftpay-Signature")):
        abort(400)
    evt = request.get_json()      # dedupe on evt["id"]; act on evt["event"]/evt["data"]
    return {"ok": True}           # 2xx stops retries
```

Surface: `sp.charges.{create,get,list,refund}` · `sp.payouts.{create,get,list,bulk}` ·
`sp.scheduled_payouts.{create,get,list}` · `sp.balance.get` · `sp.statements.{get,pdf_url}` ·
`sp.subaccounts.create` · `sp.payment_links.create` · `sp.resolve_account(phone)` ·
`sp.verify_webhook(raw_body, signature)`. Errors raise `SamsoftpayError` (`.status`, `.body`).
