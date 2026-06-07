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
