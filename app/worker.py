"""Background worker — LEGACY (kept for reference only).

Celery now handles all background work. Run these instead:

  # Terminal 1 — task worker  (target app.celery_worker, NOT app.celery_app —
  # app.celery_app is the UNCONFIGURED factory: default RabbitMQ broker, ZERO
  # registered tasks. See CLAUDE.md guardrail #2.)
  celery -A app.celery_worker:celery worker --loglevel=info --concurrency=2

  # Terminal 2 — beat scheduler (periodic tasks)
  celery -A app.celery_worker:celery beat --loglevel=info

On Render, both are configured as separate worker services in render.yaml.
Redis must be running (locally: redis-server, on Render: managed Redis add-on).
"""
