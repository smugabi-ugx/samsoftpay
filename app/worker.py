"""Background worker — webhook delivery + subscription billing loop.

Run with: python -m app.worker

In production use Celery / RQ with Redis so jobs survive restarts and
multiple worker processes can run in parallel.
"""
import time

from . import create_app
from .services.webhooks import deliver_pending_webhooks


def main():
    app = create_app()
    tick = 0
    with app.app_context():
        print("worker started — webhooks every 2s, subscriptions every 60s")
        while True:
            try:
                n = deliver_pending_webhooks()
                if n:
                    print(f"delivered {n} webhook(s)")
            except Exception as exc:
                print(f"webhook worker error: {exc}")

            # Bill due subscriptions every 60 seconds
            if tick % 30 == 0:
                try:
                    from .services.subscriptions_service import bill_due
                    result = bill_due()
                    if result["attempted"]:
                        print(
                            f"subscriptions billed: {result['attempted']} attempted, "
                            f"{result['succeeded']} ok, {result['failed']} failed"
                        )
                except Exception as exc:
                    print(f"subscription billing error: {exc}")

            tick += 1
            time.sleep(2)


if __name__ == "__main__":
    main()
