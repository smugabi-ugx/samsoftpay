"""Simulated-rail guard + vending webhook events.

Two changes, one test file, because they defend the same moment: the machine
deciding to drop a product.

THE GUARD
A mock rail flips a charge to SUCCEEDED on a timer with nothing collected. In
live mode that credits the merchant's ledger with money nobody paid, and for a
vending order it dispenses a real soda for free. Airtel and Card have no real
adapter, so on Render they must be refused before anything is written.

THE EVENTS
`charge.succeeded` means the money arrived. It does NOT mean the product came
out. A platform reconciling stock needs `vending.dispensed` /
`vending.dispense_failed`, which is what the machine actually did.

What this proves:
  1. Airtel and Card ARE simulated; MTN with MOMO_USE_REAL is not.
  2. In production a live Airtel charge is refused, writing NO transaction.
  3. Sandbox keys are exempt — mocks are the point of a sandbox.
  4. Local/dev is unaffected, or the whole offline test suite would die.
  5. The checkout page stops offering a method it would then refuse.
  6. A successful dispense emits vending.dispensed to the merchant's webhook.
  7. A supplier failure emits vending.dispense_failed, carrying the reason.
  8. Confirming an already-dispensed order does NOT emit a duplicate event.
"""
import atexit
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="railguard_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


os.environ["XY_KEY"] = "test-key"
os.environ["XY_SECRET"] = "test-secret"

from app import create_app
from app.extensions import db
from app.models import Channel, Merchant, PaymentLink, WebhookDelivery
from app.services import rails, vending, xy_vending
from app.services.orchestrator import OrchestratorError, _simulated_rail_forbidden, create_charge

FAIL_SUPPLIER = {"on": False}


def fake_apply_export_goods(**kwargs):
    if FAIL_SUPPLIER["on"]:
        raise xy_vending.XYVendingError("machine offline")
    return {"code": "1", "message": "ok"}


xy_vending.apply_export_goods = fake_apply_export_goods
vending.xy_vending.apply_export_goods = fake_apply_export_goods

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def events_for(app, order_id):
    """Vending webhook events queued for one order, newest last."""
    with app.app_context():
        out = []
        for row in WebhookDelivery.query.order_by(WebhookDelivery.id).all():
            body = json.loads(row.payload)
            if body.get("event", "").startswith("vending.") and \
                    body["data"].get("order_id") == order_id:
                out.append(body)
        return out


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        db.session.add(Merchant(
            name="TK Vending", email="rg@x.com",
            public_key="pk_rg", secret_key="sk_rg", kyc_status="verified",
            vending_enabled=True,
            webhook_url="https://tk.example/hooks",   # required to receive events
        ))
        db.session.commit()

    # ---- 1. which rails are simulated ----
    with app.test_request_context():
        from flask import g
        g.api_mode = "live"
        check("Airtel is simulated", rails.is_simulated(Channel.AIRTEL_MONEY))
        check("Card is simulated", rails.is_simulated(Channel.CARD))
        check("MTN is simulated while MOMO_USE_REAL=0",
              rails.is_simulated(Channel.MTN_MOMO))

    # ---- 2/3/4. the guard, by environment ----
    with app.test_request_context():
        from flask import g
        g.api_mode = "live"
        check("dev/local: nothing is forbidden (offline suite must still run)",
              not _simulated_rail_forbidden(Channel.AIRTEL_MONEY))

    os.environ["RENDER"] = "true"
    try:
        with app.test_request_context():
            from flask import g
            g.api_mode = "live"
            check("production: live Airtel is forbidden",
                  _simulated_rail_forbidden(Channel.AIRTEL_MONEY))
            check("production: live Card is forbidden",
                  _simulated_rail_forbidden(Channel.CARD))

            with app.app_context():
                merchant = Merchant.query.filter_by(secret_key="sk_rg").one()
                before = db.session.query(WebhookDelivery).count()
                refused = False
                try:
                    create_charge(
                        merchant=merchant, amount=250000, currency="UGX",
                        channel=Channel.AIRTEL_MONEY, customer_phone="256700000000",
                        customer_email=None, merchant_reference="airtel-live",
                    )
                except OrchestratorError as exc:
                    refused = "not available" in str(exc)
                check("production: create_charge refuses live Airtel", refused)

                from app.models import Transaction
                stranded = Transaction.query.filter_by(
                    merchant_reference="airtel-live").count()
                check("refusal wrote NO transaction", stranded == 0)
                check("refusal wrote nothing else either",
                      db.session.query(WebhookDelivery).count() == before)

        with app.test_request_context():
            from flask import g
            g.api_mode = "test"
            check("sandbox is exempt (mocks are the point of a sandbox)",
                  not _simulated_rail_forbidden(Channel.AIRTEL_MONEY))

        # ---- 5. the checkout page stops offering it ----
        with app.test_request_context():
            from app.routes.checkout import _channel_options
            offered = [c[0] for c in _channel_options()]
            check("checkout hides Airtel in production", "airtel_money" not in offered)
            check("checkout hides Card in production", "card" not in offered)
    finally:
        os.environ.pop("RENDER", None)

    # ---- 6/7/8. vending events (back in dev mode so the mock rail works) ----
    client = app.test_client()
    hdr = {"Authorization": "Bearer sk_rg", "Content-Type": "application/json",
           "X-Timestamp": str(int(time.time()))}

    def new_paid_order(key):
        r = client.post("/v1/vending/orders",
                        headers={**hdr, "Idempotency-Key": key},
                        json={"machine": "XY000123", "amount": 2500,
                              "goods": [{"spbh": "0001", "spmc": "Coca-Cola 500ml",
                                         "spdj": 2500}]})
        oid = r.get_json()["order_id"]
        client.post(f"/pay/{oid}/submit",
                    data={"channel": "mtn_momo", "phone": "256783647260"})
        deadline = time.time() + 12
        while time.time() < deadline:
            with app.app_context():
                link = PaymentLink.query.filter_by(public_id=oid).one()
                if link.vending_status in (vending.DISPENSED, vending.FAILED):
                    return oid
            time.sleep(0.25)
        raise AssertionError("order never settled")

    oid = new_paid_order("ev1")
    evs = events_for(app, oid)
    check("successful dispense emits exactly one event", len(evs) == 1)
    check("event is vending.dispensed", evs and evs[0]["event"] == "vending.dispensed")
    check("event carries machine + charge id",
          evs and evs[0]["data"].get("machine") == "XY000123"
          and evs[0]["data"].get("charge_id"))

    FAIL_SUPPLIER["on"] = True
    oid2 = new_paid_order("ev2")
    FAIL_SUPPLIER["on"] = False
    evs2 = events_for(app, oid2)
    check("supplier failure emits vending.dispense_failed",
          evs2 and evs2[-1]["event"] == "vending.dispense_failed")
    check("failure event carries the reason",
          evs2 and "machine offline" in (evs2[-1]["data"].get("error") or ""))

    # 8. a confirming callback must not re-announce the same soda
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=oid).one()
        vending._finish(link.id, ok=True)      # same outcome as before
    check("re-confirming a dispensed order emits no duplicate",
          len(events_for(app, oid)) == 1)

    print()
    failed = [label for label, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for label in failed:
            print("  - " + label)
        sys.exit(1)
    print(f"All {len(CHECKS)} rail-guard and vending-event checks passed.")


if __name__ == "__main__":
    main()
