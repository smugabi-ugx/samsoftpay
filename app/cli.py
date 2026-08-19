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

    @app.cli.command("stranded-payouts")
    def stranded_payouts():
        """List payouts that took the merchant's money but never reached a rail.

        Until the guard in services/payouts.py landed, a payout on an unsupported
        channel (or one where the rail call raised) was rejected AFTER the ledger
        earmark had been staged — and the caller's commit persisted it. The result
        is a payout stuck PENDING with no rail_reference, its amount sitting in
        the merchant's SUSPENSE account. This finds them; `reverse-payout` undoes one.
        """
        from .models import Account, AccountType, Payout, PayoutStatus

        with app.app_context():
            rows = (Payout.query
                    .filter(Payout.status == PayoutStatus.PENDING,
                            Payout.rail_reference.is_(None))
                    .order_by(Payout.created_at)
                    .all())
            if not rows:
                print("No stranded payouts.")
            total = 0
            for p in rows:
                total += p.amount
                print(f"{p.public_id}  merchant={p.merchant_id}  {p.amount} {p.currency}"
                      f"  channel={p.channel.value}  created={p.created_at}")
            if rows:
                print(f"\n{len(rows)} stranded, {total} total held in suspense")

            print("\nSUSPENSE balances by merchant:")
            for a in Account.query.filter_by(type=AccountType.SUSPENSE).all():
                held = -a.cached_balance
                if held:
                    mode = "sandbox" if a.is_test else "LIVE"
                    print(f"  merchant={a.merchant_id}  {held} {a.currency}  [{mode}]")

    @app.cli.command("reverse-payout")
    @click.argument("public_id")
    def reverse_payout(public_id):
        """Return a stranded payout's money to the merchant's available balance.

        Reverses the earmark (suspense -> available, and the fee back out of PSP
        revenue) and marks the payout FAILED. Refuses if the payout ever reached
        a rail, so this can never claw back money that actually went out.
        """
        from .models import AccountType, Payout, PayoutStatus
        from .services import ledger

        with app.app_context():
            p = Payout.query.filter_by(public_id=public_id).first()
            if not p:
                print(f"No payout {public_id}")
                return
            if p.rail_reference:
                print(f"REFUSING: {public_id} has rail_reference={p.rail_reference} — "
                      "it reached the rail. Check with the provider before touching it.")
                return
            if p.status != PayoutStatus.PENDING:
                print(f"REFUSING: {public_id} is {p.status.value}, not pending.")
                return

            # Reverse into the SAME ledger the payout was earmarked from.
            mode = bool(p.is_test)
            avail = ledger.get_or_create_account(
                type=AccountType.MERCHANT_AVAILABLE, merchant_id=p.merchant_id,
                currency=p.currency, is_test=mode)
            suspense = ledger.get_or_create_account(
                type=AccountType.SUSPENSE, merchant_id=p.merchant_id,
                currency=p.currency, is_test=mode)
            revenue = ledger.get_or_create_account(
                type=AccountType.PSP_REVENUE, merchant_id=None,
                currency=p.currency, is_test=mode)
            ledger.post(
                [
                    (suspense, +p.amount),
                    (revenue, +p.fee_amount),
                    (avail, -(p.amount + p.fee_amount)),
                ],
                currency=p.currency,
                memo=f"payout {p.public_id} reversed (never reached a rail)",
            )
            p.status = PayoutStatus.FAILED
            p.failure_reason = "reversed: never reached a rail"
            db.session.commit()
            print(f"Reversed {public_id}: {p.amount + p.fee_amount} {p.currency} "
                  f"returned to merchant {p.merchant_id}")

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

    @app.cli.command("reset-password")
    @click.argument("email")
    @click.option("--password", default=None, help="New password (generated if omitted)")
    def reset_password(email, password):
        """Set a NEW password for a merchant by EMAIL.

        The no-email recovery path until the self-service email reset flow
        ships: a locked-out merchant on a live money dashboard otherwise has
        no way back in. Also clears the failed-login lockout counter.
        """
        import secrets as _secrets
        from werkzeug.security import generate_password_hash
        with app.app_context():
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            new_pw = password or _secrets.token_urlsafe(12)
            m.password_hash = generate_password_hash(new_pw)
            for attr in ("failed_login_attempts", "locked_until", "otp_code", "otp_expires_at"):
                if hasattr(m, attr):
                    setattr(m, attr, None if "until" in attr or "code" in attr or "expires" in attr else 0)
            db.session.commit()
            print(f"password reset for {email}")
            if not password:
                print(f"new password: {new_pw}")

    @app.cli.command("issue-collections-key")
    @click.argument("email")
    @click.option("--live", is_flag=True, help="Issue the LIVE collections key (default: test).")
    def issue_collections_key(email, live):
        """Generate a collections-only (kiosk-safe) key for a merchant by EMAIL.

        A collections key takes payments but is 403'd on payouts and refunds, so
        it is safe to embed on a public machine. Prints the full key once.
        """
        import secrets as _secrets
        with app.app_context():
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            if live:
                m.collections_key = "sk_live_col_" + _secrets.token_urlsafe(24)
                key = m.collections_key
            else:
                m.test_collections_key = "sk_test_col_" + _secrets.token_urlsafe(24)
                key = m.test_collections_key
            db.session.commit()
            print(f"{'LIVE' if live else 'TEST'} collections key for {email}:")
            print(f"  {key}")
            print("  (takes payments only — cannot move money out)")

    @app.cli.command("preflight")
    @click.option("--skip-network", is_flag=True,
                  help="Skip live network checks (MTN tokens, Redis ping).")
    def preflight(skip_network):
        """Go-live preflight: verify secrets, DB, Redis, MTN credentials and the
        money guards in ONE command. Exit 0 = ready; exit 1 = at least one FAIL.

        Run after every deploy and BEFORE flipping MTN production on. This is
        also the artifact to show a partner: 'here is our go-live check.'
        """
        import os
        import sys as _sys
        from sqlalchemy import text as _text

        results = []

        def check(name, ok, detail="", warn=False):
            status = "PASS" if ok else ("WARN" if warn else "FAIL")
            results.append((status, name, detail if not ok else ""))

        with app.app_context():
            cfg = app.config
            on_render = bool(os.environ.get("RENDER"))
            momo_real = bool(cfg.get("MOMO_USE_REAL"))
            base_url = str(cfg.get("MOMO_BASE_URL") or "")
            sandbox_url = "sandbox.momodeveloper" in base_url

            # ── secrets ──
            sk = str(cfg.get("SECRET_KEY") or "")
            check("SECRET_KEY strong",
                  bool(sk) and sk != "dev-only-do-not-use-in-prod" and len(sk) >= 32,
                  "set a 32+ char random value")
            ws = str(cfg.get("WEBHOOK_SIGNING_SECRET") or "")
            check("WEBHOOK_SIGNING_SECRET strong",
                  bool(ws) and not ws.startswith("whsec_demo")
                  and ws != "whsec_change_me_in_production" and len(ws) >= 24,
                  "set a strong inbound-only secret")
            # Key separation: the inbound secret must never be any merchant's
            # outbound whsec_ (guardrail 22).
            clash = Merchant.query.filter(Merchant.webhook_secret == ws).count() if ws else 0
            check("inbound secret differs from every merchant webhook_secret",
                  clash == 0, f"{clash} merchant(s) share the global secret — rotate")

            base = str(cfg.get("BASE_URL") or "")
            check("BASE_URL is https", base.startswith("https://"),
                  f"BASE_URL={base or '(unset)'}", warn=not on_render)

            # ── database ──
            db_url = str(cfg.get("SQLALCHEMY_DATABASE_URI") or "")
            check("DATABASE_URL is not SQLite (production)",
                  not db_url.startswith("sqlite"), db_url.split("://")[0],
                  warn=not on_render)
            try:
                db.session.execute(_text("SELECT 1"))
                check("database reachable", True)
            except Exception as exc:
                check("database reachable", False, str(exc)[:120])

            # ── migrations: db version vs script heads ──
            try:
                from alembic.config import Config as _ACfg
                from alembic.script import ScriptDirectory
                acfg = _ACfg()
                acfg.set_main_option("script_location", "migrations")
                heads = set(ScriptDirectory.from_config(acfg).get_heads())
                try:
                    db_vers = set(db.session.execute(
                        _text("SELECT version_num FROM alembic_version")).scalars())
                except Exception:
                    db_vers = set()
                check("single migration head",
                      len(heads) == 1, f"heads={sorted(heads)} — run `flask db merge heads`",
                      warn=True)
                check("database is at the migration head",
                      bool(db_vers) and db_vers <= heads,
                      f"db={sorted(db_vers) or 'none'} vs heads={sorted(heads)} — run `flask db upgrade`")
            except Exception as exc:
                check("migration state readable", False, str(exc)[:120], warn=True)

            # ── redis ──
            if skip_network:
                check("redis reachable", True, "skipped (--skip-network)", warn=True)
            else:
                try:
                    import redis as _redis
                    _redis.from_url(cfg.get("REDIS_URL"), socket_timeout=3,
                                    socket_connect_timeout=3).ping()
                    check("redis reachable", True)
                except Exception as exc:
                    check("redis reachable", False, str(exc)[:120], warn=not on_render)

            # ── rails / money guards ──
            check("MOMO_USE_REAL enabled", momo_real,
                  "mock rails only — fine for dev, NOT for live money",
                  warn=not on_render)
            check("ALLOW_SIMULATED_RAILS not set in production",
                  not (on_render and cfg.get("ALLOW_SIMULATED_RAILS")),
                  "unset ALLOW_SIMULATED_RAILS — it lets mocks settle live money")
            check("sandbox is deterministic (RAIL_SUCCESS_PROBABILITY=1.0)",
                  float(cfg.get("RAIL_SUCCESS_PROBABILITY", 1.0)) == 1.0,
                  "guardrail 16 — do not lower it", warn=True)

            if momo_real:
                currency = str(cfg.get("MOMO_CURRENCY") or "")
                if sandbox_url:
                    check("MTN env: SANDBOX (EUR expected)", currency == "EUR",
                          f"MOMO_CURRENCY={currency} — sandbox uses EUR", warn=True)
                else:
                    check("MTN env: PRODUCTION uses UGX", currency == "UGX",
                          f"MOMO_CURRENCY={currency} — production must be UGX")
                    check("MOMO_TARGET_ENV is not 'sandbox' in production",
                          str(cfg.get("MOMO_TARGET_ENV")) != "sandbox",
                          "set the production target environment MTN assigns")
                for label, keys in (
                    ("collections credentials present",
                     ("MOMO_SUBSCRIPTION_KEY", "MOMO_API_USER", "MOMO_API_KEY")),
                    ("disbursement credentials present",
                     ("MOMO_DISBURSEMENT_SUBSCRIPTION_KEY",
                      "MOMO_DISBURSEMENT_API_USER", "MOMO_DISBURSEMENT_API_KEY")),
                ):
                    missing = [k for k in keys if not cfg.get(k)]
                    check(label, not missing, f"missing: {', '.join(missing)}")

                if not skip_network:
                    try:
                        from .services.rails_mtn_real import _get_token as _col_token
                        _col_token(subscription_key=cfg["MOMO_SUBSCRIPTION_KEY"],
                                   api_user=cfg["MOMO_API_USER"],
                                   api_key=cfg["MOMO_API_KEY"], base_url=base_url)
                        check("MTN collections token fetch", True)
                    except Exception as exc:
                        check("MTN collections token fetch", False, str(exc)[:120])
                    try:
                        from .services.rails_mtn_disbursement import _get_token as _dis_token
                        _dis_token(subscription_key=cfg["MOMO_DISBURSEMENT_SUBSCRIPTION_KEY"],
                                   api_user=cfg["MOMO_DISBURSEMENT_API_USER"],
                                   api_key=cfg["MOMO_DISBURSEMENT_API_KEY"],
                                   base_url=base_url)
                        check("MTN disbursement token fetch", True)
                    except Exception as exc:
                        check("MTN disbursement token fetch", False, str(exc)[:120])

            # ── key hygiene ──
            unhashed = Merchant.query.filter(
                Merchant.secret_key.isnot(None),
                Merchant.secret_key_hash.is_(None)).count()
            check("all live keys hashed at rest", unhashed == 0,
                  f"{unhashed} merchant(s) unhashed — run `flask backfill-key-hashes`",
                  warn=True)

        # ── report ──
        icons = {"PASS": "[ ok ]", "WARN": "[warn]", "FAIL": "[FAIL]"}
        print("\nSAMSOFTPAY GO-LIVE PREFLIGHT")
        print("=" * 60)
        for status, name, detail in results:
            line = f"{icons[status]} {name}"
            if detail:
                line += f"  -> {detail}"
            print(line)
        fails = sum(1 for s, _, _ in results if s == "FAIL")
        warns = sum(1 for s, _, _ in results if s == "WARN")
        print("=" * 60)
        print(f"{len(results)} checks: {len(results) - fails - warns} pass, "
              f"{warns} warn, {fails} FAIL")
        if fails:
            print("NOT READY — fix the FAIL items above before going live.")
            _sys.exit(1)
        print("READY" + (" (with warnings)" if warns else ""))

    @app.cli.command("reconcile-mtn")
    def reconcile_mtn():
        """Reconcile live MTN charges against MTN's OWN status, and list open exceptions."""
        with app.app_context():
            from .services.reconciliation import reconcile_against_mtn
            from .models import ReconException
            summary = reconcile_against_mtn()
            print("MTN reconciliation:", summary)
            open_rows = ReconException.query.filter_by(status="open").order_by(
                ReconException.severity, ReconException.created_at).all()
            if not open_rows:
                print("No open exceptions — books agree with MTN.")
            for r in open_rows:
                print(f"  [{r.severity}] {r.kind} ref={r.rail_reference} "
                      f"txn={r.txn_public_id} ours={r.our_status} mtn={r.mtn_status}")

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
