"""Subscription plan management and public subscribe pages."""
from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db, limiter
from ..models import Channel, Subscription, SubscriptionPlan
from ..services.subscriptions_service import (
    cancel_subscription,
    create_plan,
    pause_subscription,
    resume_subscription,
    subscribe,
)
from ..utils import verified_required

bp = Blueprint("subscriptions", __name__)

def _live_channels():
    """Only channels a live billing cycle can actually charge.

    Offering Airtel/card here created plans whose every renewal silently
    failed — the orchestrator refuses simulated rails for live money
    (guardrail 14). Same gate the checkout page uses.
    """
    from ..models import Channel
    from ..services.orchestrator import _simulated_rail_forbidden
    options = [
        ("mtn_momo",     "MTN Mobile Money"),
        ("airtel_money", "Airtel Money"),
        ("card",         "Visa / Mastercard"),
    ]
    out = []
    for value, label in options:
        try:
            if _simulated_rail_forbidden(Channel(value)):
                continue
        except Exception:
            pass
        out.append((value, label))
    return out


def _channels_blocked_reason():
    """Why the channel list is empty, in words a merchant can act on.

    When MTN is running as a mock on Render (MOMO_USE_REAL unset), the
    simulated-rail guard correctly hides EVERY live channel — which used to
    render an empty <select> on a required field, so a merchant simply could
    not create a plan and nothing said why. Never fail silently again.
    """
    import os
    if not os.environ.get("RENDER"):
        return None
    from flask import current_app
    if current_app.config.get("MOMO_USE_REAL"):
        return None
    return ("MTN Mobile Money is running in sandbox on this deployment, so no "
            "live billing channel is available yet. Recurring plans will be "
            "creatable the moment MTN production credentials are switched on "
            "(set MOMO_USE_REAL=1).")

_INTERVALS = [
    ("weekly",  "Weekly"),
    ("monthly", "Monthly"),
    ("yearly",  "Yearly"),
]


# ── Merchant dashboard ────────────────────────────────────────────────────────���

@bp.get("/dashboard/subscriptions")
@login_required
@verified_required
def list_plans():
    plans = (SubscriptionPlan.query
             .filter_by(merchant_id=current_user.id)
             .order_by(SubscriptionPlan.created_at.desc())
             .all())
    # Live subscribers per plan (active + past_due are both still billing).
    counts = {
        p.id: Subscription.query.filter(
            Subscription.plan_id == p.id,
            Subscription.status.in_(("active", "past_due"))).count()
        for p in plans
    }
    # MRR: normalise each plan's live revenue to a monthly figure so the
    # merchant sees one number that means "recurring revenue" (Stripe/Paystack
    # show this front and centre — it's what makes a plan feel like it "brings"
    # something).
    _to_monthly = {"daily": 30.0, "weekly": 4.33, "monthly": 1.0, "yearly": 1 / 12.0}
    mrr = 0
    for p in plans:
        if p.is_active:
            mrr += int(counts.get(p.id, 0) * p.amount * _to_monthly.get(p.interval, 1.0))
    total_subs = Subscription.query.filter(
        Subscription.merchant_id == current_user.id,
        Subscription.status.in_(("active", "past_due"))).count()
    return render_template("subscriptions.html",
                           plans=plans, counts=counts,
                           total_subs=total_subs, mrr=mrr,
                           channels=_live_channels(),
                           channels_blocked=_channels_blocked_reason(),
                           intervals=_INTERVALS)


@bp.post("/dashboard/subscriptions/new-plan")
@login_required
@verified_required
def new_plan():
    try:
        amount = int(request.form["amount"])
        name = request.form["name"].strip()
        interval = request.form.get("interval", "monthly")
        channel = Channel(request.form.get("channel", "mtn_momo"))
        description = request.form.get("description", "").strip() or None
    except (KeyError, ValueError):
        return redirect(url_for("subscriptions.list_plans"))
    create_plan(
        merchant_id=current_user.id,
        name=name, description=description,
        amount=amount, interval=interval, channel=channel,
    )
    return redirect(url_for("subscriptions.list_plans"))


@bp.post("/dashboard/subscriptions/plans/<int:plan_id>/deactivate")
@login_required
@verified_required
def deactivate_plan(plan_id: int):
    plan = SubscriptionPlan.query.filter_by(
        id=plan_id, merchant_id=current_user.id
    ).first_or_404()
    plan.is_active = False
    db.session.commit()
    return redirect(url_for("subscriptions.list_plans"))


@bp.get("/dashboard/subscriptions/plans/<int:plan_id>/subscribers")
@login_required
@verified_required
def plan_subscribers(plan_id: int):
    plan = SubscriptionPlan.query.filter_by(
        id=plan_id, merchant_id=current_user.id
    ).first_or_404()
    subs = (Subscription.query.filter_by(plan_id=plan_id)
            .order_by(Subscription.created_at.desc()).all())
    # Per-subscriber payment history (read-only), so the merchant can see money
    # actually collected — the audit flagged the page as a "dead end".
    from ..models import Transaction, TxnStatus
    from sqlalchemy import func as safunc
    pay = {}
    collected_total = 0
    for s in subs:
        rows = (Transaction.query
                .filter(Transaction.merchant_id == current_user.id,
                        Transaction.merchant_reference == f"sub_{s.public_id}")
                .order_by(Transaction.created_at.desc()).all())
        paid = sum(t.amount for t in rows if t.status == TxnStatus.SUCCEEDED)
        collected_total += paid
        last = next((t for t in rows if t.status == TxnStatus.SUCCEEDED), None)
        pay[s.id] = {
            "total_paid": paid,
            "charge_count": len(rows),
            "last_paid_at": last.completed_at if last else None,
        }
    return render_template("subscription_subscribers.html", plan=plan, subs=subs,
                           pay=pay, collected_total=collected_total)


@bp.post("/dashboard/subscriptions/<int:sub_id>/cancel")
@login_required
@verified_required
def cancel(sub_id: int):
    sub = Subscription.query.filter_by(
        id=sub_id, merchant_id=current_user.id
    ).first_or_404()
    cancel_subscription(sub.id)
    return redirect(url_for("subscriptions.plan_subscribers", plan_id=sub.plan_id))


@bp.post("/dashboard/subscriptions/<int:sub_id>/pause")
@login_required
@verified_required
def pause(sub_id: int):
    sub = Subscription.query.filter_by(
        id=sub_id, merchant_id=current_user.id
    ).first_or_404()
    pause_subscription(sub.id)
    return redirect(url_for("subscriptions.plan_subscribers", plan_id=sub.plan_id))


@bp.post("/dashboard/subscriptions/<int:sub_id>/resume")
@login_required
@verified_required
def resume(sub_id: int):
    sub = Subscription.query.filter_by(
        id=sub_id, merchant_id=current_user.id
    ).first_or_404()
    resume_subscription(sub.id)
    return redirect(url_for("subscriptions.plan_subscribers", plan_id=sub.plan_id))


# ── Public subscribe page ──────────────────────────────────────────────────────

@bp.get("/pay/@<handle>/subscribe/<plan_public_id>")
def subscribe_page(handle: str, plan_public_id: str):
    from ..models import Merchant
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    plan = SubscriptionPlan.query.filter_by(
        public_id=plan_public_id, merchant_id=merchant.id, is_active=True
    ).one_or_none()
    if plan is None:
        abort(404)
    return render_template("subscribe.html", merchant=merchant, plan=plan)


@bp.post("/pay/@<handle>/subscribe/<plan_public_id>")
@limiter.limit("10 per minute;60 per hour")   # unauthenticated, enrolls recurring MoMo billing
def subscribe_submit(handle: str, plan_public_id: str):
    from ..models import Merchant
    merchant = Merchant.query.filter_by(handle=handle).one_or_none()
    if merchant is None:
        abort(404)
    plan = SubscriptionPlan.query.filter_by(
        public_id=plan_public_id, merchant_id=merchant.id, is_active=True
    ).one_or_none()
    if plan is None:
        abort(404)

    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip() or None

    if not phone:
        return render_template("subscribe.html", merchant=merchant, plan=plan,
                               error="Phone number is required.")

    # Dedupe: a double-submit (or resubmitted form) created a SECOND live
    # subscription for the same phone+plan, each billing independently every
    # cycle — the customer was charged twice forever.
    #
    # Match ANY still-billing status, not just 'active'. A first charge that the
    # customer declines flips the subscription to 'past_due' (dunning), which
    # keeps retrying via the billing beat — exactly as list_plans counts live
    # subscribers (active + past_due). A dedupe of 'active' only missed the
    # past_due case: a customer who re-submitted after a declined first charge
    # got a SECOND live subscription and was then billed twice every cycle.
    from ..models import Subscription
    existing = Subscription.query.filter(
        Subscription.plan_id == plan.id,
        Subscription.customer_phone == phone,
        Subscription.status.in_(("active", "past_due")),
    ).first()
    if existing:
        return render_template("subscribe.html", merchant=merchant, plan=plan,
                               success=True, sub=existing)

    sub = subscribe(plan=plan, customer_phone=phone, customer_email=email)

    # Fire the FIRST charge synchronously so the customer gets the MoMo prompt
    # NOW, at the moment they subscribe — instead of a "success" screen with
    # nothing happening until the 60s beat (the "the plan doesn't bring
    # anything" report). Then advance next_billing_at by one interval so the
    # billing beat never re-charges this same first cycle. Never raises out to
    # the customer: a rail hiccup leaves the subscription for the beat to bill.
    from datetime import datetime, timezone
    from flask import g
    from ..services.orchestrator import OrchestratorError, create_charge
    from ..services.subscriptions_service import _next_billing
    charge_ok = False
    try:
        g.api_mode = "live"
        txn = create_charge(
            merchant=merchant,
            amount=plan.amount,
            currency=plan.currency,
            channel=plan.channel,
            customer_phone=phone,
            customer_email=email,
            merchant_reference=f"sub_{sub.public_id}",
        )
        charge_ok = txn is not None and txn.status.value != "failed"
        now = datetime.now(timezone.utc)
        sub.current_period_start = now
        sub.next_billing_at = _next_billing(now, plan.interval)
        db.session.commit()
    except OrchestratorError:
        # Plan channel not available / merchant issue — leave the sub for the
        # beat; show success (enrollment is recorded) without a false prompt.
        db.session.rollback()
    except Exception:
        db.session.rollback()
        from flask import current_app
        current_app.logger.exception("subscription first-charge failed")

    return render_template("subscribe.html", merchant=merchant, plan=plan,
                           success=True, sub=sub, prompt_sent=charge_ok)


# ── API endpoints ──────────────────────────────────────────────────────────────

@bp.post("/v1/subscription-plans")
def api_create_plan():
    from flask import g, jsonify
    from ..extensions import limiter
    from ..routes.api import _auth, _check_timestamp

    _check_timestamp()
    merchant = _auth()
    body = request.get_json(silent=True) or {}

    try:
        amount   = int(body["amount"])
        name     = str(body["name"]).strip()
        interval = body.get("interval", "monthly")
        channel  = Channel(body.get("channel", "mtn_momo"))
    except (KeyError, ValueError, TypeError) as exc:
        from flask import abort
        abort(400, description=f"invalid request: {exc}")

    plan = create_plan(
        merchant_id=merchant.id,
        name=name,
        description=body.get("description"),
        amount=amount,
        interval=interval,
        channel=channel,
    )
    return jsonify(
        id=plan.public_id,
        name=plan.name,
        amount=plan.amount,
        interval=plan.interval,
        subscribe_url=url_for("subscriptions.subscribe_page",
                              handle=merchant.handle or str(merchant.id),
                              plan_public_id=plan.public_id,
                              _external=True),
    ), 201


@bp.get("/v1/subscription-plans")
def api_list_plans():
    from flask import jsonify
    from ..routes.api import _auth
    merchant = _auth()
    plans = SubscriptionPlan.query.filter_by(merchant_id=merchant.id).all()
    return jsonify([{
        "id": p.public_id, "name": p.name,
        "amount": p.amount, "interval": p.interval,
        "is_active": p.is_active,
    } for p in plans])


@bp.get("/v1/subscriptions/<public_id>")
def api_get_subscription(public_id: str):
    from flask import jsonify
    from ..routes.api import _auth
    merchant = _auth()
    sub = Subscription.query.filter_by(
        public_id=public_id, merchant_id=merchant.id
    ).one_or_none()
    if sub is None:
        from flask import abort
        abort(404)
    return jsonify(
        id=sub.public_id,
        status=sub.status,
        customer_phone=sub.customer_phone,
        next_billing_at=sub.next_billing_at.isoformat(),
        created_at=sub.created_at.isoformat(),
    )
