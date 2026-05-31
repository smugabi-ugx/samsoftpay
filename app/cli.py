"""Flask CLI commands."""
import secrets
from flask import Flask

from .extensions import db
from .models import Merchant


def register(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db():
        """Create all tables."""
        with app.app_context():
            db.create_all()
        print("database initialized")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create a demo merchant with predictable keys."""
        with app.app_context():
            existing = Merchant.query.filter_by(email="demo@samsoftpay.local").first()
            if existing:
                # Backfill test keys if missing (for merchants seeded before sandbox feature)
                if not existing.test_secret_key:
                    existing.test_public_key = "pk_test_demo123"
                    existing.test_secret_key = "sk_test_demo123"
                    db.session.commit()
                    print(f"backfilled test keys for id={existing.id}")
                else:
                    print(f"already exists: id={existing.id} secret={existing.secret_key}")
                return
            m = Merchant(
                name="Demo Merchant Ltd",
                email="demo@samsoftpay.local",
                public_key="pk_live_demo123",
                secret_key="sk_live_demo123",
                test_public_key="pk_test_demo123",
                test_secret_key="sk_test_demo123",
                kyc_status="verified",
                email_verified=True,
                two_fa_enabled=False,
                webhook_url="http://localhost:5000/inbound/mtn_momo",
            )
            db.session.add(m)
            db.session.commit()
            print(f"created merchant id={m.id}")
            print(f"public_key={m.public_key}")
            print(f"secret_key={m.secret_key}")

    @app.cli.command("make-admin")
    def make_admin():
        """Promote a merchant to the admin role (interactive)."""
        import click
        with app.app_context():
            email = click.prompt("Merchant email")
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            m.role = "admin"
            db.session.commit()
            print(f"Done — {m.name} ({m.email}) is now an admin.")

    @app.cli.command("sweep-pending")
    def sweep_pending():
        """Expire stale PENDING/AUTHORIZED transactions (older than 10 min)."""
        from .services.sweep import sweep_stale_transactions
        with app.app_context():
            result = sweep_stale_transactions(stale_minutes=10)
            print(f"Swept {result['swept']} transaction(s): "
                  f"{result['succeeded']} succeeded, {result['failed']} failed/expired")
            for item in result["items"]:
                print(f"  {item['id']} -> {item['result']}")

    @app.cli.command("new-merchant")
    def new_merchant():
        """Generate a fresh merchant with random keys."""
        with app.app_context():
            m = Merchant(
                name=f"Merchant {secrets.token_hex(3)}",
                email=f"m+{secrets.token_hex(4)}@example.com",
                public_key="pk_live_" + secrets.token_urlsafe(20),
                secret_key="sk_live_" + secrets.token_urlsafe(28),
                kyc_status="verified",
            )
            db.session.add(m)
            db.session.commit()
            print(f"id={m.id} public={m.public_key} secret={m.secret_key}")
