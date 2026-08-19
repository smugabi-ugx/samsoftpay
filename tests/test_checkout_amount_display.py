"""The checkout page must show ONE honest amount (persona sweep, priority #4).

Before: with a gift card applied, the hero amount and the Pay button both still
showed the FULL price while a separate line said "you pay less" — three
different numbers at the moment of payment, which reads as bait-and-switch.

What this proves:
  1. With a voucher applied, the headline + Pay button show the DISCOUNTED amount.
  2. The full price is shown struck-through (not as the amount to pay).
  3. With no voucher, the full amount shows and nothing is struck.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink

CHECKS = []
CH = [("mtn_momo", "MTN MoMo", "momo")]


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Shop", email="s@x.com", public_key="pk", secret_key="sk_live", handle="shop")
        db.session.add(m)
        db.session.commit()
        link = PaymentLink(public_id="lnk_x", merchant_id=m.id, amount=10000,
                           currency="UGX", is_test=False, is_active=True)
        db.session.add(link)
        db.session.commit()

        from flask import render_template
        with app.test_request_context():
            applied = render_template("checkout.html", link=link, merchant=m, channels=CH,
                                      crypto_url="#", voucher_applied=True, voucher_discount=3000,
                                      prefill={})
            check("voucher: headline shows discounted 7,000", "7,000" in applied)
            check("voucher: Pay button shows discounted amount", "Pay UGX 7,000" in applied)
            check("voucher: full 10,000 shown struck-through",
                  "line-through" in applied and "10,000" in applied)

            plain = render_template("checkout.html", link=link, merchant=m, channels=CH,
                                    crypto_url="#", voucher_applied=False, voucher_discount=0,
                                    prefill={})
            check("no voucher: Pay button shows full 10,000", "Pay UGX 10,000" in plain)
            check("no voucher: nothing struck through", "line-through" not in plain)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL CHECKOUT-AMOUNT-DISPLAY TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
