"""A refund must NOT trip the nightly external ledger-reconciliation alert.

A refund returns money via a SEPARATE disbursement; the original collection
really happened, so its rail 'succeeded' event stands. Counting only SUCCEEDED
transactions (not REFUNDED) drifted the per-channel tally by one on every refund
and fired a FALSE 'critical' alert every night. The fix counts SUCCEEDED +
REFUNDED against the rail 'succeeded' events. This proves the two sides balance
after a refund.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="recon_refund_")
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
from app.models import Channel, Merchant, RailEvent, Transaction, TxnStatus, utcnow
from app.services.reconciliation import run_reconciliation


def _charge(mid, status, ref):
    t = Transaction(public_id=f"txn_{ref}", merchant_id=mid, amount=10000,
                    fee_amount=200, currency="UGX", channel=Channel.MTN_MOMO,
                    status=status, rail_reference=f"rail_{ref}",
                    completed_at=utcnow())
    db.session.add(t)
    # every collected charge writes a 'succeeded' rail event; a refund does NOT
    # remove it (the money really moved).
    db.session.add(RailEvent(rail=Channel.MTN_MOMO, rail_reference=f"rail_{ref}",
                             event_type="succeeded", amount=10000, currency="UGX",
                             raw_payload="{}"))
    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="R Co", email="r@x.com", public_key="pk_live_r",
                     test_secret_key="sk_test_r", secret_key="sk_live_r")
        db.session.add(m); db.session.commit()
        mid = m.id

        _charge(mid, TxnStatus.SUCCEEDED, "a")   # a plain succeeded charge
        _charge(mid, TxnStatus.REFUNDED,  "b")   # a charge that was later refunded

        report = run_reconciliation()
        mtn = report["external"][Channel.MTN_MOMO.value]
        assert mtn["rail_succeeded_events"] == 2, mtn
        assert mtn["transactions_succeeded"] == 2, mtn   # SUCCEEDED + REFUNDED both count
        assert mtn["match"] is True, f"refund tripped a false reconciliation mismatch: {mtn}"
        print(f"[1] after a refund, external MTN recon balances: {mtn} -> match=True (no false alert)")

    print("\nReconciliation refund-awareness check passed.")


if __name__ == "__main__":
    main()
