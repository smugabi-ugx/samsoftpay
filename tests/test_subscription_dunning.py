"""A failed subscription charge must NOT churn the plan on the first miss.

Persona sweep priority #7. Before: any failed charge set status='failed'
forever. In Uganda, where wallets top up irregularly, one bad-timing charge
killed recurring revenue permanently. Now a failure goes 'past_due' and is
retried on a backoff (1,2,3,5 days) before finally churning to 'failed'.

What this proves:
  1. A synchronous failure (fee>=amount) makes the sub past_due, not failed,
     with retry_count=1 and a scheduled next_retry_at.
  2. bill_due does NOT retry before next_retry_at is due.
  3. Once due, bill_due retries; after the last retry the sub finally 'failed'.
  4. The async completion hook: a failed subscription charge -> past_due; a
     succeeded one -> active + dunning reset (the common insufficient-funds case
     resolves asynchronously via complete_transaction).
  5. resume_subscription clears the dunning state.
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db
from app.models import (
    Channel, Merchant, Subscription, SubscriptionPlan, Transaction, TxnStatus, utcnow,
)
from app.services import subscriptions_service as svc
from app.services.orchestrator import _maybe_update_subscription

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Sub Co", email="sub@x.com", public_key="pk", secret_key="sk_live",
                     handle="sub", is_active=True, kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        # amount=100 -> fee (min 200) >= amount -> create_charge raises
        # OrchestratorError synchronously, giving us a deterministic failure.
        plan = svc.create_plan(merchant_id=m.id, name="Tiny", description=None,
                               amount=100, currency="UGX", interval="monthly")
        sub = svc.subscribe(plan=plan, customer_phone="256700000000")
        sub_id = sub.id
        m_id, plan_id = m.id, plan.id

        # 1. First failure -> past_due, retry_count 1, next_retry_at set.
        svc.bill_due()
        sub = db.session.get(Subscription, sub_id)
        check("first failure -> past_due (not failed)", sub.status == "past_due")
        check("retry_count is 1", sub.retry_count == 1)
        check("next_retry_at scheduled", sub.next_retry_at is not None)

        # 2. Not retried before next_retry_at is due.
        svc.bill_due()
        sub = db.session.get(Subscription, sub_id)
        check("not retried before next_retry_at due (retry_count still 1)", sub.retry_count == 1)

        # 3. Force each retry due until the plan finally churns.
        for expected_rc in (2, 3, 4):
            sub = db.session.get(Subscription, sub_id)
            sub.next_retry_at = utcnow() - timedelta(minutes=1)
            db.session.commit()
            svc.bill_due()
            sub = db.session.get(Subscription, sub_id)
            check(f"retry brought retry_count to {expected_rc}, still past_due",
                  sub.retry_count == expected_rc and sub.status == "past_due")

        # The next forced retry is the 5th failure (> MAX 4) -> failed.
        sub.next_retry_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        svc.bill_due()
        sub = db.session.get(Subscription, sub_id)
        check("after the last retry the sub finally churns to 'failed'", sub.status == "failed")
        check("failed sub has no further retry scheduled", sub.next_retry_at is None)

    # 4. Async completion hook.
    with app.app_context():
        sub2 = Subscription(public_id="sub_async01", merchant_id=m_id, plan_id=plan_id,
                            customer_phone="256700000000", status="active",
                            current_period_start=utcnow(), next_billing_at=utcnow())
        db.session.add(sub2)
        db.session.commit()
        # a charge whose reference points at this subscription
        txn = Transaction(public_id="txn_s1", merchant_id=m_id, amount=100, fee_amount=0,
                          currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.FAILED,
                          merchant_reference="sub_sub_async01", failure_reason="insufficient_funds")
        db.session.add(txn)
        db.session.commit()

        _maybe_update_subscription(txn, success=False)
        sub2 = Subscription.query.filter_by(public_id="sub_async01").first()
        check("async FAILED charge -> sub past_due", sub2.status == "past_due" and sub2.retry_count == 1)

        _maybe_update_subscription(txn, success=True)
        sub2 = Subscription.query.filter_by(public_id="sub_async01").first()
        check("async SUCCEEDED charge -> sub active + reset",
              sub2.status == "active" and sub2.retry_count == 0 and sub2.next_retry_at is None)

        # 5. resume clears dunning state.
        sub2.status = "past_due"
        sub2.retry_count = 3
        sub2.next_retry_at = utcnow()
        db.session.commit()
        svc.resume_subscription(sub2.id)
        sub2 = Subscription.query.filter_by(public_id="sub_async01").first()
        check("resume from past_due clears dunning state",
              sub2.status == "active" and sub2.retry_count == 0 and sub2.next_retry_at is None)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SUBSCRIPTION-DUNNING TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
