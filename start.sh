#!/usr/bin/env bash
set -e
echo "==> Running database migrations..."
python -m flask --app run.py db upgrade
echo "==> Starting Gunicorn..."
exec gunicorn run:app --bind "0.0.0.0:${PORT:-5000}" --workers 1 --timeout 120 --access-logfile -
