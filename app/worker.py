"""Background worker — runs the webhook delivery + recon + settlement loop.

Run with: python -m app.worker

In production this would be Celery / RQ / Dramatiq with a Redis broker, multiple
worker processes, and a separate beat scheduler. Here we just sleep-loop.
"""
import time

from . import create_app
from .services.webhooks import deliver_pending_webhooks


def main():
    app = create_app()
    with app.app_context():
        print("worker started")
        while True:
            try:
                n = deliver_pending_webhooks()
                if n:
                    print(f"delivered {n} webhooks")
            except Exception as exc:  # don't kill the loop on errors
                print(f"worker error: {exc}")
            time.sleep(2)


if __name__ == "__main__":
    main()
