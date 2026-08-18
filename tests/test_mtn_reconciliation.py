"""External reconciliation against MTN's own records.

The third leg the internal check can't provide: proving our ledger agrees
with MTN. The critical case is a LOST CALLBACK — MTN collected the money but
our callback never landed, so we show the charge as not-succeeded while MTN
says SUCCESSFUL. That is money in our float, unbooked, and invisible to every
internal check. This must surface as an exception.

What this proves:
  1. MTN SUCCESSFUL + our txn AUTHORIZED  -> critical exception (lost callback).
  2. MTN FAILED + our txn SUCCEEDED       -> critical exception (booked money MTN never took).
  3. MTN SUCCESSFUL + our txn SUCCEEDED    -> no exception (agreement).
  4. A network-unknown (None) answer       -> never fabricates an exception.
  5. A later run where records now AGREE   -> auto-resolves the open exception.
  6. A SUCCEEDED live txn with no reference -> a (warning) exception.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="recon_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


from datetime import timedelta

from app import create_app
from app.extensions import db
from app.models import Channel, Merchant, ReconException, Transaction, TxnStatus, utcnow

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


# Controllable fake MTN status, keyed by rail_reference.
MTN = {}


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Recon Shop", email="recon@x.com",
                     public_key="pk_r", secret_key="sk_r", kyc_status="verified")
        db.session.add(m); db.session.commit()
        mid = m.id

    # Patch the MTN status query the reconciler uses.
    import app.services.reconciliation as recon
    import app.services.sweep as sweep
    sweep._query_mtn_status = lambda ref: MTN.get(ref)

    old = utcnow() - timedelta(hours=1)   # old enough that MTN has a final answer

    def make_txn(app, ref, status, amount=2500):
        import uuid
        with app.app_context():
            t = Transaction(
                public_id=f"txn_{uuid.uuid4().hex[:12]}", merchant_id=mid,
                amount=amount, fee_amount=200, currency="UGX",
                channel=Channel.MTN_MOMO, status=status, is_test=False,
                rail_reference=ref, created_at=old,
            )
            db.session.add(t); db.session.commit()
            return t.public_id

    # 1. lost callback: MTN SUCCESSFUL, we're still AUTHORIZED
    make_txn(app, "ref-lost", TxnStatus.AUTHORIZED)
    MTN["ref-lost"] = "SUCCESSFUL"
    # 2. we booked, MTN failed
    make_txn(app, "ref-badbook", TxnStatus.SUCCEEDED)
    MTN["ref-badbook"] = "FAILED"
    # 3. agreement
    make_txn(app, "ref-ok", TxnStatus.SUCCEEDED)
    MTN["ref-ok"] = "SUCCESSFUL"
    # 4. network unknown
    make_txn(app, "ref-unknown", TxnStatus.AUTHORIZED)
    MTN["ref-unknown"] = None
    # 6. succeeded, no reference
    import uuid
    with app.app_context():
        t = Transaction(public_id=f"txn_{uuid.uuid4().hex[:12]}", merchant_id=mid,
                        amount=2500, fee_amount=200, currency="UGX",
                        channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                        is_test=False, rail_reference=None, created_at=old)
        db.session.add(t); db.session.commit()

    with app.app_context():
        summary = recon.reconcile_against_mtn()
        print("summary:", summary)
        exc = {e.rail_reference: e for e in ReconException.query.all()}

        check("lost callback -> critical exception",
              "ref-lost" in exc and exc["ref-lost"].severity == "critical"
              and exc["ref-lost"].kind == "mtn_succeeded_local_not")
        check("we-booked-MTN-failed -> critical exception",
              "ref-badbook" in exc and exc["ref-badbook"].kind == "local_succeeded_mtn_failed")
        check("agreement -> NO exception", "ref-ok" not in exc)
        check("network unknown -> NO exception (never fabricated)", "ref-unknown" not in exc)
        check("succeeded-with-no-reference -> warning exception",
              any(e.kind == "succeeded_no_reference" and e.severity == "warning"
                  for e in exc.values()))
        check("summary counts an open exception set", summary["open_exceptions"] >= 3)
        check("unknown was skipped, not failed", summary["skipped_unknown"] == 1)

    # 5. auto-resolve: the lost-callback txn now really is SUCCEEDED and MTN agrees
    with app.app_context():
        t = Transaction.query.filter_by(rail_reference="ref-lost").one()
        t.status = TxnStatus.SUCCEEDED
        db.session.commit()
    with app.app_context():
        recon.reconcile_against_mtn()
        row = ReconException.query.filter_by(rail_reference="ref-lost").one()
        check("a now-agreeing exception auto-resolves",
              row.status == "resolved" and row.resolved_by == "auto")

    print()
    failed = [l for l, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for l in failed:
            print("  - " + l)
        sys.exit(1)
    print(f"All {len(CHECKS)} MTN-reconciliation checks passed.")


if __name__ == "__main__":
    main()
