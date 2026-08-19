"""Webhook envelope carries an event id + timestamp, and the charge payload is
built from ONE shared helper (persona sweep priority #6).

Before: receivers had no id (can't dedupe retries) and no timestamp (docs
promised replay protection with nothing to check), and the charge.succeeded
`data` dict was hand-built in orchestrator._queue_webhook AND in the gift-card
checkout — already drifted.

What this proves:
  1. Every enqueued webhook has a top-level id (evt_...) + integer timestamp.
  2. The signature verifies over the exact envelope bytes.
  3. charge_event_data produces the documented charge fields.
  4. Two events get distinct ids (dedupe key works).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db
from app.models import Channel, Merchant, Transaction, TxnStatus, utcnow
from app.services.webhooks import (
    charge_event_data, enqueue, merchant_signing_secret, verify_signature,
)

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    # Stub the immediate-delivery broker call so enqueue() doesn't block trying
    # to reach Redis (there is none in the offline test).
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None
    with app.app_context():
        db.create_all()
        m = Merchant(name="Hook Co", email="h@x.com", public_key="pk", secret_key="sk_live",
                     handle="hook", webhook_url="https://merchant.example/hooks")
        db.session.add(m)
        db.session.commit()
        txn = Transaction(public_id="txn_abc", merchant_id=m.id, amount=5000, fee_amount=75,
                          currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                          merchant_reference="order-1", completed_at=utcnow())
        db.session.add(txn)
        db.session.commit()

        # 3. Shared helper shape.
        data = charge_event_data(txn)
        check("charge_event_data has the documented fields",
              data["id"] == "txn_abc" and data["amount"] == 5000 and data["fee"] == 75
              and data["status"] == "succeeded" and "completed_at" in data)

        from app.models import WebhookDelivery
        enqueue(m, "charge.succeeded", data, transaction_id=txn.id)
        enqueue(m, "charge.succeeded", data, transaction_id=txn.id)
        rows = WebhookDelivery.query.order_by(WebhookDelivery.id).all()
        check("two deliveries were queued", len(rows) == 2)

        env = json.loads(rows[0].payload)
        # 1. Envelope fields.
        check("envelope has an evt_ id", isinstance(env.get("id"), str) and env["id"].startswith("evt_"))
        check("envelope has an integer timestamp", isinstance(env.get("timestamp"), int) and env["timestamp"] > 0)
        check("envelope has event + data", env.get("event") == "charge.succeeded" and env.get("data", {}).get("id") == "txn_abc")

        # 2. Signature verifies over the exact bytes.
        secret = merchant_signing_secret(m)
        check("signature verifies over the envelope payload",
              verify_signature(rows[0].payload, rows[0].signature, secret))

        # 4. Distinct ids per event (dedupe key).
        env2 = json.loads(rows[1].payload)
        check("each event gets a distinct id", env["id"] != env2["id"])

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL WEBHOOK-ENVELOPE TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
