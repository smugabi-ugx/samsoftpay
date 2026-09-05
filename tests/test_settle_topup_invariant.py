"""_settle_topup hardening (audit: ledger-integrity double-credit vector).

The real race (a concurrent settlement sweep double-crediting a top-up) is
Postgres-only - SQLite no-ops with_for_update and is single-writer, so the TOCTOU
can't be reproduced here. What this pins is the behaviour the fix must keep/add:

  [1] A normal settle moves net (amount-fee) pending->available exactly once, and
      a SECOND call (repeat poll / post-sweep) is a no-op via the settled_at guard
      - no double credit. Ledger sums to zero throughout.
  [2] The new release invariant fires: if a release would drive merchant_pending
      POSITIVE (a double release), _settle_topup raises instead of minting, so a
      caller rollback leaves available uncredited.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="settletopup_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import (Account, AccountType, Channel, JournalEntry, Merchant,
                        Transaction, TxnStatus)
from app.services import ledger
from app.routes.wallet import _settle_topup

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def available(mid):
    a = Account.query.filter_by(merchant_id=mid, type=AccountType.MERCHANT_AVAILABLE,
                                is_test=False).first()
    return -a.cached_balance if a else 0


def journal_total():
    return sum(e.amount for e in JournalEntry.query.all())


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    with app.app_context():
        db.create_all()

    # [1] normal settle + repeat (settle-once) — net=1000-200=800
    with app.app_context():
        m = Merchant(name="Topup Co", email="tc@x.com", public_key="pk", secret_key="sk",
                     kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        pending = ledger.get_or_create_account(type=AccountType.MERCHANT_PENDING,
                                               merchant_id=mid, currency="UGX", is_test=False)
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                            merchant_id=None, currency="UGX", is_test=False)
        # simulate the top-up collection crediting pending -800
        ledger.post([(rail, +800), (pending, -800)], currency="UGX", memo="collect")
        txn = Transaction(public_id="txn_tu1", merchant_id=mid, amount=1000, fee_amount=200,
                          currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                          is_test=False)
        db.session.add(txn)
        db.session.commit()

        _settle_topup(mid, txn, "topup_tu1")
        db.session.commit()
        check("[1] first settle credits available by net (800)", available(mid) == 800)
        check("[1] txn is marked settled", txn.settled_at is not None)

        _settle_topup(mid, txn, "topup_tu1")   # repeat poll — must no-op
        db.session.commit()
        check("[1] a second settle does NOT double-credit (still 800)", available(mid) == 800)
        check("[1] ledger sums to zero", journal_total() == 0)

    # [2] invariant fires when a release would drive pending positive
    with app.app_context():
        m2 = Merchant(name="Bad Co", email="bc@x.com", public_key="pk2", secret_key="sk2",
                      kyc_status="verified")
        db.session.add(m2)
        db.session.commit()
        mid2 = m2.id
        # NOTE: pending is NOT credited here (starts at 0), so releasing net would
        # drive it positive — exactly the double-release signature.
        txn2 = Transaction(public_id="txn_tu2", merchant_id=mid2, amount=1000, fee_amount=200,
                           currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                           is_test=False)
        db.session.add(txn2)
        db.session.commit()
        raised = False
        try:
            _settle_topup(mid2, txn2, "topup_tu2")
        except RuntimeError:
            raised = True
            db.session.rollback()   # what the caller / Flask teardown does
        check("[2] invariant raises when release would over-credit", raised)
        check("[2] after rollback, available was NOT credited", available(mid2) == 0)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SETTLE-TOPUP INVARIANT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
