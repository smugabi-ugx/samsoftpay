"""Celery application factory.

Import the `celery` instance here to decorate tasks:

    from app.celery_app import celery

    @celery.task(bind=True)
    def my_task(self, ...): ...

Call `init_celery(app)` inside `create_app()` to wire Flask config and app
context into every task execution.
"""
from celery import Celery
from celery.schedules import crontab

celery = Celery("samsoftpay")


def init_celery(app: object) -> Celery:
    """Configure Celery with Flask app settings and inject app context into tasks."""
    celery.conf.update(
        broker_url=app.config["REDIS_URL"],
        result_backend=app.config["REDIS_URL"],
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="Africa/Kampala",
        enable_utc=True,
        # Retry tasks that were in-flight when the worker died
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Periodic tasks (replaces the worker.py sleep loop)
        beat_schedule={
            "sweep-pending-webhooks": {
                "task": "app.tasks.webhooks_task.sweep_pending_webhooks",
                "schedule": 30.0,          # every 30 seconds
            },
            "process-due-subscriptions": {
                "task": "app.tasks.billing.process_due_subscriptions",
                "schedule": 60.0,          # every 60 seconds
            },
            "run-scheduled-payouts": {
                "task": "app.tasks.scheduled_payouts.run_scheduled_payouts",
                "schedule": 60.0,          # every 60 seconds — payroll autopilot
            },
            "email-monthly-statements": {
                "task": "app.tasks.statements.email_monthly_statements",
                "schedule": crontab(day_of_month=1, hour=6, minute=0),   # 1st of month
            },
            "auto-settlement-sweep": {
                "task": "app.tasks.sweep.auto_settlement_sweep",
                "schedule": 3600.0,        # every hour
            },
            "resolve-stale-transactions": {
                "task": "app.tasks.sweep.resolve_stale_transactions",
                "schedule": 3600.0,        # every hour — resolves poller stragglers from MTN's own answer
            },
            "resolve-stale-payouts": {
                "task": "app.tasks.sweep.resolve_stale_payouts",
                "schedule": 3600.0,        # every hour — OUTBOUND straggler net (payouts stuck AUTHORIZED)
            },
            "complete-pending-topups": {
                "task": "app.tasks.sweep.complete_pending_topups",
                "schedule": 900.0,         # every 15 min — finish top-ups whose payer closed the page
            },
            "prune-old-rows": {
                "task": "app.tasks.sweep.prune_old_rows",
                "schedule": 86400.0,       # daily — sent webhooks + aged idempotency keys (30d retention)
            },
            "nightly-ledger-reconciliation": {
                "task": "app.tasks.reconciliation.reconcile_ledger",
                "schedule": crontab(hour=2, minute=30),   # 02:30 Africa/Kampala
            },
            "hourly-mtn-reconciliation": {
                "task": "app.tasks.reconciliation.reconcile_mtn",
                "schedule": 3600.0,   # match our ledger against MTN's own records every hour
            },
            "worker-heartbeat": {
                "task": "app.tasks.monitoring.heartbeat",
                "schedule": 60.0,     # liveness ping — /ops/status reads this
            },
            "check-money-stuck": {
                "task": "app.tasks.monitoring.check_money_stuck",
                "schedule": 3600.0,   # hourly — alert on stranded payouts / stuck AUTHORIZED charges
            },
            "payout-anomaly-scan": {
                "task": "app.tasks.monitoring.payout_anomaly_scan",
                "schedule": 600.0,    # every 10 min — rolling-sum drain detection (aggregates, not per-payout)
            },
            "refund-outlier-scan": {
                "task": "app.tasks.monitoring.refund_outlier_scan",
                "schedule": 86400.0,  # daily — refunds-vs-charges outlier report per merchant
            },
        },
        # Worker settings
        worker_prefetch_multiplier=1,       # one task at a time per worker slot
        task_track_started=True,
    )

    class FlaskTask(celery.Task):
        """Base task that runs inside a Flask application context."""
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = FlaskTask
    celery.flask_app = app
    return celery
