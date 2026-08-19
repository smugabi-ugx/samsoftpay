"""Payment-link and vending-order creation honor Idempotency-Key (persona #8).

Before: a retried POST /v1/payment-links or /v1/vending/orders silently created
a DUPLICATE link/order/QR — found only when a customer paid the wrong one. Now
these honor Idempotency-Key when sent (dedupe, reserve-before-execute) while
staying optional so callers that don't send one keep working.

What this proves:
  1. Same key + same body -> the SAME link is returned, no second row created.
  2. Same key + DIFFERENT body -> 409.
  3. No key -> each call creates a new link (backward compatible).
  4. Same for vending orders.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="idem_lv_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")

import atexit
@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass

from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Idem Co", email="i@x.com", public_key="pk", secret_key="sk_live",
                     test_secret_key="sk_test_i", kyc_status="verified", handle="idem",
                     vending_enabled=True)
        db.session.add(m)
        db.session.commit()

    c = app.test_client()

    def H(idem=None):
        h = {"Authorization": "Bearer sk_test_i", "Content-Type": "application/json",
             "X-Timestamp": str(int(time.time()))}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    def link(idem=None, amount=5000):
        return c.post("/v1/payment-links", headers=H(idem),
                      data=json.dumps({"amount": amount, "description": "x"}))

    def order(idem=None, amount=3000):
        return c.post("/v1/vending/orders", headers=H(idem),
                      data=json.dumps({"amount": amount, "machine": "M1",
                                       "goods": [{"spbh": "0001", "spmc": "Coke", "spdj": amount}]}))

    # 1. Same key + same body -> same link, no duplicate row.
    r1 = link("k1")
    r2 = link("k1")
    check("link create 1 -> 201", r1.status_code == 201)
    check("link retry same key -> same id (deduped)", r1.json["id"] == r2.json["id"])
    with app.app_context():
        check("only ONE link row exists for k1", PaymentLink.query.count() == 1)

    # 2. Same key + different body -> 409.
    r3 = link("k1", amount=9999)
    check("same key + different body -> 409", r3.status_code == 409)

    # 3. No key -> each call is a new link.
    a = link()
    b = link()
    check("no key -> distinct links", a.json["id"] != b.json["id"])

    # 4. Vending orders: same key -> same order.
    o1 = order("vk1")
    o2 = order("vk1")
    check("vending order 1 -> 201", o1.status_code == 201)
    check("vending retry same key -> same order id", o1.json["order_id"] == o2.json["order_id"])
    with app.app_context():
        # one for the order + none extra; links table now holds prior links + 1 order link
        before = PaymentLink.query.count()
    order("vk1")   # third retry
    with app.app_context():
        check("vending retry created no extra row", PaymentLink.query.count() == before)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL IDEMPOTENCY-LINK/VENDING TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
