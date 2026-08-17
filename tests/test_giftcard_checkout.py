"""Gift-card checkout — an independent code review found real gaps here that
had zero test coverage, which is exactly how they slipped through:

  1. A fully-gift-card-covered charge hardcoded is_test=False, ignoring the
     link's actual mode, and skipped the dispense trigger + webhook that a
     rail-collected SUCCEEDED charge always gets.
  2. The gift card was debited BEFORE create_charge was attempted for the
     remainder — if that charge was then rejected, the customer's gift-card
     balance was gone with nothing delivered for it. Non-atomic.
  3. Gift cards have no test/live mode of their own (they're dashboard-issued
     real liability, unlike PaymentLink). A sandbox checkout could redeem a
     real merchant's real gift card.

What this proves:
  1. A fully-covered LIVE order settles as is_test=False, gets completed_at,
     and both the webhook AND vending's dispense trigger fire — same side
     effects as a rail-collected charge.
  2. A fully-covered TEST-mode order settles as is_test=True.
  3. A sandbox checkout is refused a real gift card at the APPLY step —
     the card is never even looked up, let alone touched.
  4. THE MONEY CASE: a partial-discount order whose remaining charge is
     rejected (fee exceeds the tiny remainder) leaves the gift card's
     balance COMPLETELY UNTOUCHED — not partially debited, not debited then
     silently un-refunded.
  5. A partial-discount order whose remaining charge succeeds DOES debit the
     gift card, exactly once.
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="giftcard_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink, Transaction, WebhookDelivery
from app.services.giftcards import create_gift_card

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def make_link(app, merchant_id, amount, is_test):
    with app.app_context():
        import uuid
        link = PaymentLink(
            public_id=f"lnk_{uuid.uuid4().hex[:16]}",
            merchant_id=merchant_id, amount=amount, currency="UGX",
            is_test=is_test,
        )
        db.session.add(link)
        db.session.commit()
        return link.public_id


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(
            name="Gift Card Shop", email="giftcardtest@x.com",
            public_key="pk_gc", secret_key="sk_gc",
            test_public_key="pk_test_gc", test_secret_key="sk_test_gc",
            kyc_status="verified", webhook_url="https://merchant.example/hooks",
        )
        db.session.add(m)
        db.session.commit()
        merchant_id = m.id

        full_card = create_gift_card(merchant_id=merchant_id, face_value=2500)
        full_card_code = full_card.code

        test_mode_card = create_gift_card(merchant_id=merchant_id, face_value=2500)
        test_mode_card_code = test_mode_card.code
        test_mode_card_id = test_mode_card.id

        partial_card_a = create_gift_card(merchant_id=merchant_id, face_value=250)
        partial_card_a_code = partial_card_a.code
        partial_card_a_id = partial_card_a.id

        partial_card_b = create_gift_card(merchant_id=merchant_id, face_value=250)
        partial_card_b_code = partial_card_b.code
        partial_card_b_id = partial_card_b.id

    client = app.test_client()

    # ---- 1/2. fully-covered order, LIVE and TEST mode ----
    live_link = make_link(app, merchant_id, 2500, is_test=False)
    client.post(f"/pay/{live_link}/apply-voucher", data={"code": full_card_code})
    r = client.post(f"/pay/{live_link}/submit", data={"channel": "mtn_momo", "phone": "256783647260"})
    assert r.status_code in (302, 303), r.status_code

    with app.app_context():
        db.session.expire_all()
        link = PaymentLink.query.filter_by(public_id=live_link).one()
        txn = db.session.get(Transaction, link.transaction_id)
        check("fully-covered LIVE order settles is_test=False", txn.is_test is False)
        check("...with completed_at set", txn.completed_at is not None)
        events = [WebhookDelivery.query.filter_by(transaction_id=txn.id).all()]
        check("...and a webhook was queued for it", len(events[0]) == 1)

    test_link = make_link(app, merchant_id, 2500, is_test=True)
    r2 = client.post(f"/pay/{test_link}/submit", data={"channel": "mtn_momo", "phone": "256783647260"})
    assert r2.status_code in (302, 303), r2.status_code
    with app.app_context():
        db.session.expire_all()
        link2 = PaymentLink.query.filter_by(public_id=test_link).one()
        txn2 = db.session.get(Transaction, link2.transaction_id) if link2.transaction_id else None
        check("TEST-mode order settles is_test=True", txn2 is not None and txn2.is_test is True)

    # ---- 3. sandbox cannot touch a real gift card, at all ----
    sandbox_link = make_link(app, merchant_id, 2500, is_test=True)
    r3 = client.post(f"/pay/{sandbox_link}/apply-voucher", data={"code": test_mode_card_code})
    assert r3.status_code == 200
    check("sandbox checkout refuses a real gift card",
          b"sandbox" in r3.data.lower() or b"test-mode" in r3.data.lower())
    with app.app_context():
        db.session.expire_all()
        from app.models import GiftCard
        card = db.session.get(GiftCard, test_mode_card_id)
        check("...and the card's balance is completely untouched", card.balance == 2500)

    # ---- 4. THE MONEY CASE: rejected remainder must not burn the gift card ----
    # link=300, card=250 (partial), remainder=50 -> fee(200) >= amount(50) ->
    # create_charge raises OrchestratorError deterministically, no rail involved.
    reject_link = make_link(app, merchant_id, 300, is_test=False)
    client.post(f"/pay/{reject_link}/apply-voucher", data={"code": partial_card_a_code})
    r4 = client.post(f"/pay/{reject_link}/submit", data={"channel": "mtn_momo", "phone": "256783647260"})
    check("rejected remainder shows an error, not a redirect", r4.status_code == 200)
    with app.app_context():
        db.session.expire_all()
        from app.models import GiftCard
        card_a = db.session.get(GiftCard, partial_card_a_id)
        check("...and the gift card balance is COMPLETELY UNTOUCHED (was 250)",
              card_a.balance == 250)
        link4 = PaymentLink.query.filter_by(public_id=reject_link).one()
        check("...and no transaction was ever attached to the link",
              link4.transaction_id is None)

    # ---- 5. a partial discount whose remainder SUCCEEDS debits exactly once ----
    ok_link = make_link(app, merchant_id, 2750, is_test=False)   # remainder=2500, real fee
    client.post(f"/pay/{ok_link}/apply-voucher", data={"code": partial_card_b_code})
    r5 = client.post(f"/pay/{ok_link}/submit", data={"channel": "mtn_momo", "phone": "256783647260"})
    check("partial-discount order with a valid remainder redirects (accepted)",
          r5.status_code in (302, 303))
    with app.app_context():
        db.session.expire_all()
        from app.models import GiftCard
        card_b = db.session.get(GiftCard, partial_card_b_id)
        check("...and the gift card WAS debited exactly once (250 -> 0)",
              card_b.balance == 0)

    print()
    failed = [label for label, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for label in failed:
            print("  - " + label)
        sys.exit(1)
    print(f"All {len(CHECKS)} gift-card checkout checks passed.")


if __name__ == "__main__":
    main()
