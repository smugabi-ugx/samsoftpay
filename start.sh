#!/usr/bin/env bash
# Production start script for Render / Linux deployments
set -e

echo "Running database migrations..."
python -m flask --app run.py db upgrade

echo "Starting Gunicorn..."
exec gunicorn "app:create_app()" \
  --bind "0.0.0.0:${PORT:-5000}" \
  --workers 2 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
