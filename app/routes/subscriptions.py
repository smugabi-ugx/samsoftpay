"""Subscription plan management and public subscribe pages."""
from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
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

_CHANNELS = [
    ("mtn_momo",     "MTN Mobile Money"),
    ("airtel_money", "Airtel Money"),
    ("card",         "Visa / Mastercard"),
]

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
    # Subscriber counts per plan
    counts = {
        p.id: Subscription.query.filter_by(plan_id=p.id, status="active").count()
        for p in plans
    }
    total_subs = Subscription.query.filter_by(merchant_id=current_user.id).count()
    return render_template("subscriptions.html",
                           plans=plans, counts=counts,
                           total_subs=total_subs,
                           channels=_CHANNELS, intervals=_INTERVALS)


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
    return render_template("subscription_subscribers.html", plan=plan, subs=subs)


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

    sub = subscribe(plan=plan, customer_phone=phone, customer_email=email)
    return render_template("subscribe.html", merchant=merchant, plan=plan,
                           success=True, sub=sub)


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
