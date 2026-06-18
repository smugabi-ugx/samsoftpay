"""Flask CLI commands."""
import secrets

import click
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
                changed = False
                if not existing.test_secret_key:
                    existing.test_public_key = "pk_test_demo123"
                    existing.test_secret_key = "sk_test_demo123"
                    changed = True
                if not existing.handle:
                    existing.handle = "demo-merchant"
                    changed = True
                if changed:
                    db.session.commit()
                    print(f"backfilled missing fields for id={existing.id}")
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
                handle="demo-merchant",
                webhook_url=app.config.get("BASE_URL", "http://localhost:5000") + "/inbound/mtn_momo",
            )
            db.session.add(m)
            db.session.commit()
            print(f"created merchant id={m.id}")
            print(f"public_key={m.public_key}")
            print(f"secret_key={m.secret_key}")

    @app.cli.command("make-admin")
    def make_admin():
        """Promote an existing merchant to admin role (interactive)."""
        import click
        with app.app_context():
            email = click.prompt("Merchant email")
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            m.role = "admin"
            m.email_verified = True
            db.session.commit()
            print(f"Done — {m.name} ({m.email}) is now an admin.")

    @app.cli.command("create-admin")
    def create_admin():
        """Create a new admin account (interactive)."""
        import click, secrets
        from werkzeug.security import generate_password_hash
        with app.app_context():
            name  = click.prompt("Full name")
            email = click.prompt("Email")
            pwd   = click.prompt("Password", hide_input=True, confirmation_prompt=True)
            if Merchant.query.filter_by(email=email).first():
                print("Account with that email already exists. Use make-admin instead.")
                return
            m = Merchant(
                name=name, email=email,
                password_hash=generate_password_hash(pwd),
                public_key="pk_live_" + secrets.token_urlsafe(20),
                secret_key="sk_live_" + secrets.token_urlsafe(28),
                test_public_key="pk_test_" + secrets.token_urlsafe(20),
                test_secret_key="sk_test_" + secrets.token_urlsafe(28),
                handle=email.split("@")[0],
                role="admin", kyc_status="verified",
                email_verified=True, two_fa_enabled=False,
            )
            db.session.add(m)
            db.session.commit()
            print(f"Admin created: {email} (id={m.id})")

    @app.cli.command("backfill-key-hashes")
    def backfill_key_hashes():
        """Populate secret_key_hash / test_secret_key_hash for existing merchants.

        Safe to run repeatedly. Run once after deploying the hash columns; auth then
        uses hash lookup and the plaintext fallback can eventually be removed.
        """
        from .models import hash_api_key
        with app.app_context():
            merchants = Merchant.query.all()
            changed = 0
            for m in merchants:
                new_secret = hash_api_key(m.secret_key)
                new_test = hash_api_key(m.test_secret_key)
                if m.secret_key_hash != new_secret or m.test_secret_key_hash != new_test:
                    m.secret_key_hash = new_secret
                    m.test_secret_key_hash = new_test
                    changed += 1
            db.session.commit()
            print(f"backfilled key hashes for {changed} of {len(merchants)} merchant(s)")

    @app.cli.command("delete-merchant")
    @click.argument("email")
    def delete_merchant(email):
        """Delete a merchant that has NO transactions/payouts (e.g. a stray test account)."""
        from .models import Transaction, Payout, Account
        with app.app_context():
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            if (Transaction.query.filter_by(merchant_id=m.id).first()
                    or Payout.query.filter_by(merchant_id=m.id).first()):
                print(f"Refusing: {email} has transactions/payouts. Deactivate it instead.")
                return
            Account.query.filter_by(merchant_id=m.id).delete()
            db.session.delete(m)
            db.session.commit()
            print(f"deleted merchant: {email}")

    @app.cli.command("verify-merchant")
    @click.argument("email")
    def verify_merchant(email):
        """Mark a merchant KYC-verified (enables live keys). e.g. for KarlPOS / TK Vending."""
        with app.app_context():
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            m.kyc_status = "verified"
            db.session.commit()
            print(f"verified: {m.name} ({m.email}) — live keys enabled")

    @app.cli.command("set-instant-settlement")
    @click.argument("email")
    @click.argument("state", required=False, default="on")
    def set_instant_settlement(email, state):
        """Toggle instant settlement (skip the 24h hold) for a merchant. STATE: on|off."""
        with app.app_context():
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            m.instant_settlement = state.lower() in ("on", "true", "1", "yes")
            db.session.commit()
            print(f"{m.name} ({m.email}) instant_settlement = {m.instant_settlement}")

    @app.cli.command("disable-2fa")
    @click.argument("email", required=False)
    def disable_2fa(email):
        """Turn OFF email-OTP 2FA for a merchant (by EMAIL), or for ALL if no email.

        Use this to unlock accounts stuck at the 2FA email screen.
        """
        with app.app_context():
            q = Merchant.query.filter_by(email=email) if email else Merchant.query
            merchants = q.all()
            if email and not merchants:
                print(f"No merchant found with email: {email}")
                return
            n = 0
            for m in merchants:
                if m.two_fa_enabled:
                    m.two_fa_enabled = False
                    m.otp_code = None
                    m.otp_expires_at = None
                    n += 1
            db.session.commit()
            print(f"disabled 2FA for {n} of {len(merchants)} merchant(s)")

    @app.cli.command("reconcile")
    def reconcile():
        """Run ledger reconciliation now and print the result."""
        from .tasks.reconciliation import _problems
        from .services.reconciliation import run_reconciliation
        with app.app_context():
            report = run_reconciliation()
            problems = _problems(report)
            if problems:
                print("RECONCILIATION FAILED:")
                for p in problems:
                    print(f"  - {p}")
            else:
                print("Reconciliation OK — ledger is consistent.")
            print(f"journal sums: {report['internal']['journal_sum_by_currency']}")

    @app.cli.command("bill-subscriptions")
    def bill_subscriptions():
        """Manually trigger billing for all due subscriptions."""
        from .services.subscriptions_service import bill_due
        with app.app_context():
            result = bill_due()
            print(f"Billed: {result['attempted']} attempted, "
                  f"{result['succeeded']} succeeded, {result['failed']} failed")

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

    @app.cli.command("create-merchant")
    @click.argument("name")
    @click.argument("email")
    @click.option("--webhook", default=None, help="Webhook URL for transaction events")
    @click.option("--handle", default=None, help="Unique URL handle (defaults from email)")
    def create_merchant(name, email, webhook, handle):
        """Create a production merchant with real random keys.

        Usage:
            flask create-merchant "TK Vending" billing@tkvending.com \\
                --webhook https://tkvending.example.com/hooks/samsoftpay
        """
        import click as _click
        with app.app_context():
            if Merchant.query.filter_by(email=email).first():
                print(f"A merchant with email {email} already exists. Aborting.")
                return
            derived_handle = (handle or email.split("@")[0]).lower()
            derived_handle = "".join(c for c in derived_handle if c.isalnum() or c == "-")[:40]
            m = Merchant(
                name=name,
                email=email,
                public_key="pk_live_" + secrets.token_urlsafe(20),
                secret_key="sk_live_" + secrets.token_urlsafe(28),
                test_public_key="pk_test_" + secrets.token_urlsafe(20),
                test_secret_key="sk_test_" + secrets.token_urlsafe(28),
                handle=derived_handle,
                webhook_url=webhook,
                kyc_status="verified",
                email_verified=True,
                two_fa_enabled=False,
            )
            db.session.add(m)
            db.session.commit()
            print("=" * 60)
            print(f"  Merchant created: {m.name} (id={m.id})")
            print("=" * 60)
            print(f"  LIVE public key : {m.public_key}")
            print(f"  LIVE secret key : {m.secret_key}")
            print(f"  TEST public key : {m.test_public_key}")
            print(f"  TEST secret key : {m.test_secret_key}")
            print(f"  Handle          : {m.handle}")
            print(f"  Webhook         : {m.webhook_url or '(none)'}")
            print("=" * 60)
            print("  Store the secret keys securely. They are shown only once here.")
            print("=" * 60)
