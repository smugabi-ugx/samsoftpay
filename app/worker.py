"""Background worker — LEGACY (kept for reference only).

Celery now handles all background work. Run these instead:

  # Terminal 1 — task worker
  celery -A app.celery_app:celery worker --loglevel=info --concurrency=2

  # Terminal 2 — beat scheduler (periodic tasks)
  celery -A app.celery_app:celery beat --loglevel=info

On Render, both are configured as separate worker services in render.yaml.
Redis must be running (locally: redis-server, on Render: managed Redis add-on).
"""
