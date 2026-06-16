"""Celery entrypoint for the worker and beat processes.

Start with:
    celery -A app.celery_worker:celery worker --loglevel=info --concurrency=2
    celery -A app.celery_worker:celery beat   --loglevel=info

Why this module exists:
`app/celery_app.py` only creates a bare `celery` object. Its broker, result
backend, beat schedule, and Flask-app-context task wrapper are all applied inside
`init_celery()`, which runs as part of `create_app()`. The web process gets that
for free via run.py, but the worker/beat processes do NOT call create_app() on
their own. Pointing Celery at THIS module fixes both gaps:

  1. create_app() runs -> init_celery() configures broker=Redis, result backend,
     beat_schedule, and the FlaskTask app-context wrapper.
  2. The task modules are imported so their @celery.task registrations attach to
     the celery object (otherwise the worker would know about zero tasks).
"""
from . import create_app
from .celery_app import celery

# Build the Flask app — this calls init_celery(app), wiring broker/backend/
# beat_schedule/FlaskTask onto the `celery` object imported above.
flask_app = create_app()

# Import task modules for their side effect: registering the tasks on `celery`.
# Without these imports the worker starts with no tasks and beat schedules nothing.
from .tasks import (  # noqa: E402,F401
    billing,
    polling,
    reconciliation,
    sweep,
    webhooks_task,
)

__all__ = ["celery", "flask_app"]
