"""A refund must emit a charge.refunded webhook.

Before this, refund_charge fired no charge-scoped event — a merchant's cart could
not tell a sale was refunded (the payout.* webhook is keyed to the refund
disbursement, not the original charge_id). Proves charge.refunded is enqueued,
carrying the original charge id + refund amount.
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="refund_hook_")
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
from app.models import Merchant, Transaction, TxnStatus, Channel, AccountType
from app.services import ledger, refunds, webhooks

CHECKS = []
CAPTURED = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Ref Co", email="ref@x.com", public_key="pk_test_ref",
                     secret_key="sk_live_ref", test_secret_key="sk_test_ref",
                     kyc_status="verified", webhook_url="https://example.com/hook")
        db.session.add(m); db.session.commit()
        mid = m.id

        # fund the merchant's available so the refund disbursement can be funded
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None,
                                            currency="UGX", is_test=False)
        avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid,
                                             currency="UGX", is_test=False)
        ledger.post([(rail, +100000), (avail, -100000)], currency="UGX", memo="fund")
        db.session.commit()

        txn = Transaction(public_id=f"txn_{uuid.uuid4().hex[:16]}", merchant_id=mid,
                          amount=10000, fee_amount=150, currency="UGX",
                          channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                          is_test=False, customer_phone="256700000000",
                          merchant_reference="ORDER-77")
        db.session.add(txn); db.session.commit()
        cid = txn.public_id

        # capture enqueue instead of hitting Redis/delivery
        def _cap(merchant, event, data, **kw):
            CAPTURED.append({"event": event, "data": data})
            return True
        webhooks.enqueue = _cap

        with app.test_request_context():
            res = refunds.refund_charge(txn, m)

        check("refund succeeded", res.get("ok") is True)
        hook = next((c for c in CAPTURED if c["event"] == "charge.refunded"), None)
        check("charge.refunded webhook enqueued", hook is not None)
        if hook:
            d = hook["data"]
            check("  carries the original charge id", d.get("id") == cid)
            check("  status = refunded", d.get("status") == "refunded")
            check("  refund_amount = 10000", d.get("refund_amount") == 10000)
            check("  reference preserved", d.get("reference") == "ORDER-77")

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
