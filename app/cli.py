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
            existing = Merchant.query.filter_by(email="demo@pesademo.local").first()
            if existing:
                print(f"already exists: id={existing.id} secret={existing.secret_key}")
                return
            m = Merchant(
                name="Demo Merchant Ltd",
                email="demo@pesademo.local",
                public_key="pk_test_demo123",
                secret_key="sk_test_demo123",
                kyc_status="verified",
                webhook_url="http://localhost:5000/inbound/mtn_momo",  # loopback for demo
            )
            db.session.add(m)
            db.session.commit()
            print(f"created merchant id={m.id}")
            print(f"public_key={m.public_key}")
            print(f"secret_key={m.secret_key}")

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
