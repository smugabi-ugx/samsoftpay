"""Public subscribe enrollment must dedupe against ANY still-billing status,
not just 'active'.

Bug (multi-agent hunt, CONFIRMED medium): subscribe_submit deduped by
status='active' only. A first charge the customer declines flips the
subscription to 'past_due' (dunning), which keeps retrying via the billing
beat. A customer who then re-submitted the public form got a SECOND live
subscription for the same phone+plan and was billed twice every cycle — there
is no unique constraint on (plan_id, customer_phone) to stop it. list_plans
already counts active+past_due as live subscribers, so the dedupe must too.

What this proves:
  1. Re-submitting the public subscribe form while an existing subscription is
     'past_due' does NOT create a second subscription (dedupe hit).
  2. The sole subscription is returned to the customer (success screen), and no
     second first-charge is fired.
  3. Control: with no existing subscription, the form DOES create one (the
     dedupe isn't over-broadly swallowing genuine new enrollments).
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="subdedupe_test_")
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
from app.models import Merchant, Subscription
from app.services import subscriptions_service as svc

# create_charge on the non-dedupe path may enqueue async work; with no Redis
# broker .delay would block. Stub it (same pattern as the other route tests).
import app.tasks.webhooks_task as _wt
_wt.deliver_webhook.delay = lambda *a, **k: None

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def live_count(plan_id, phone):
    return Subscription.query.filter(
        Subscription.plan_id == plan_id,
        Subscription.customer_phone == phone,
    ).count()


def main():
    app = create_app()
    # The subscriptions blueprint (unlike checkout) is NOT CSRF-exempt — the real
    # public form carries a session CSRF token from the rendered GET page. A raw
    # test POST has none, so disable CSRF here to exercise the handler directly.
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        m = Merchant(name="Sub Co", email="subdedupe@x.com",
                     public_key="pk", secret_key="sk_live",
                     handle="subco", is_active=True, kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        m_id = m.id
        # amount small so any non-deduped path fails create_charge synchronously
        # (fee>=amount) — no rail, no Redis — while still creating the 2nd row
        # first, which is exactly what the assertion must catch.
        plan = svc.create_plan(merchant_id=m_id, name="Tiny", description=None,
                               amount=100, currency="UGX", interval="monthly")
        plan_pub = plan.public_id
        plan_id = plan.id

        # An existing subscription already in the past_due dunning state.
        phone = "256700000123"
        sub = svc.subscribe(plan=plan, customer_phone=phone)
        sub.status = "past_due"
        sub.retry_count = 1
        db.session.commit()
        existing_id = sub.id

    client = app.test_client()

    # 1/2. Re-submit the public form for the SAME phone+plan while past_due.
    r = client.post(f"/pay/@subco/subscribe/{plan_pub}", data={"phone": phone})
    check("re-submit while past_due is accepted (no error page)",
          r.status_code == 200)
    with app.app_context():
        db.session.expire_all()
        check("no SECOND subscription was created for the past_due customer",
              live_count(plan_id, phone) == 1)
        only = Subscription.query.filter_by(plan_id=plan_id,
                                            customer_phone=phone).first()
        check("...and it is the original subscription (same id, still past_due)",
              only.id == existing_id and only.status == "past_due")

    # 3. Control: a brand-new phone DOES enroll (dedupe isn't over-broad).
    new_phone = "256700000999"
    with app.app_context():
        before = live_count(plan_id, new_phone)
    client.post(f"/pay/@subco/subscribe/{plan_pub}", data={"phone": new_phone})
    with app.app_context():
        db.session.expire_all()
        check("a genuinely new customer still creates a subscription",
              live_count(plan_id, new_phone) == before + 1)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SUBSCRIBE-DEDUPE TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
