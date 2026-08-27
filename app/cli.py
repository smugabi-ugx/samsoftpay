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
    @click.argument("email", required=False)
    def make_admin(email):
        """Promote an existing merchant to admin. EMAIL optional (prompts if omitted).

        Takes the email as an ARGUMENT so it works in a non-interactive shell
        (`flask make-admin you@example.com`); prompting only when omitted, since
        a prompt that cannot read a TTY leaves you locked out of /admin.
        Lists the known accounts when the email does not match, so a typo or a
        forgotten address does not turn into a dead end.
        """
        with app.app_context():
            if not email:
                email = click.prompt("Merchant email")
            m = Merchant.query.filter_by(email=(email or "").strip().lower()).first()
            if not m:
                print(f"No merchant found with email: {email}")
                known = [x.email for x in Merchant.query.order_by(Merchant.id).limit(20)]
                if known:
                    print("Known accounts: " + ", ".join(known))
                return
            m.role = "admin"
            m.email_verified = True
            db.session.commit()
            print(f"Done — {m.name} ({m.email}) is now an admin. "
                  f"Sign out and back in, then open /admin.")

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

    @app.cli.command("credit-sandbox")
    @click.argument("email")
    @click.argument("amount", type=int)
    @click.option("--currency", default="UGX", help="Currency (default UGX).")
    def credit_sandbox(email, amount, currency):
        """Credit a merchant's SANDBOX available balance for testing.

        Test money ONLY (is_test=True) — it can never touch the live/withdrawable
        ledger, so this cannot mint real funds. Lets an integrator (e.g. Backbone
        Payroll) test payouts without first building a sandbox top-up flow: a
        payout needs `available` funds (amount + fee) or it is rejected before a
        pout_ id is created.

        Example:  flask credit-sandbox tester@example.com 5000000
        """
        with app.app_context():
            from .services import ledger
            from .models import Account, AccountType
            m = Merchant.query.filter_by(email=email).first()
            if not m:
                print(f"No merchant found with email: {email}")
                return
            if amount <= 0:
                print("amount must be a positive integer")
                return
            # Zero-sum, sandbox-only: sandbox float in (+, debit-normal) and the
            # merchant's SANDBOX available up (-, credit-normal). Same convention
            # as a charge landing in available, minus any fee.
            rail = ledger.get_or_create_account(
                type=AccountType.RAIL_CLEARING, merchant_id=None,
                currency=currency, is_test=True)
            avail = ledger.get_or_create_account(
                type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id,
                currency=currency, is_test=True)
            ledger.post(
                [(rail, +amount), (avail, -amount)],
                currency=currency,
                memo=f"sandbox test credit for {email}")
            db.session.commit()
            db.session.refresh(avail)
            print(f"Credited {amount} {currency} to {m.name} ({email}) SANDBOX available. "
                  f"New sandbox available: {-int(avail.cached_balance)} {currency}. "
                  f"(Test money only — is_test=True; cannot be withdrawn as real funds.)")

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

    @app.cli.command("freeze-payouts")
    @click.argument("state", required=False, default="status")
    def freeze_payouts(state):
        """Platform-wide LIVE payout kill switch. STATE: on|off|status.

        `on` stops every live payout instantly (create_payout refuses with
        zero writes) — the incident response for a suspected drain. Test-mode
        payouts keep working. No deploy needed; flips a DB flag.
        """
        from .services import platform_flags
        with app.app_context():
            state = state.lower()
            if state == "status":
                frozen = platform_flags.payouts_frozen()
                print("payouts are " + ("FROZEN" if frozen else "flowing (not frozen)"))
                return
            if state not in ("on", "off"):
                print("usage: flask freeze-payouts [on|off|status]")
                return
            platform_flags.set_flag(platform_flags.FREEZE_PAYOUTS, state,
                                    updated_by="cli")
            from .services.audit import log_event
            log_event("payouts.freeze_" + state, detail={"via": "cli"})
            db.session.commit()
            if state == "on":
                print("PAYOUTS FROZEN platform-wide. Unfreeze: flask freeze-payouts off")
            else:
                print("Payouts unfrozen — live payouts flow again.")

    @app.cli.command("payout-anomalies")
    def payout_anomalies():
        """Run the payout anomaly scan on demand (the 10-min beat, by hand)."""
        from .services.anomaly import scan_payout_anomalies
        with app.app_context():
            findings = scan_payout_anomalies()
            if not findings:
                print("clear — no payout anomalies")
                return
            for f in findings:
                print(f"[{f['kind']}] {f}")

    @app.cli.command("regulator-pack")
    @click.argument("month")   # YYYY-MM
    def regulator_pack(month):
        """Export a regulator/audit bundle for one month into ./regulator_pack/.

        Flutterwave-Kenya lesson: a freeze inquiry answered in hours, not
        weeks. Contents: full journal (live ledger), merchant register with
        KYC status, and a flow-of-funds summary — all FROM THE LEDGER (Dash
        lesson: never show a number the journal can't prove).
        """
        import csv
        import os as _os
        from datetime import datetime, timedelta
        from sqlalchemy import func as safunc
        from .models import Account, JournalEntry, Payout, Transaction
        with app.app_context():
            try:
                start = datetime.strptime(month, "%Y-%m")
            except ValueError:
                print("usage: flask regulator-pack YYYY-MM")
                return
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            outdir = _os.path.join("regulator_pack", month)
            _os.makedirs(outdir, exist_ok=True)

            # 1. Journal — every live-ledger entry in the month.
            with open(_os.path.join(outdir, "journal.csv"), "w",
                      newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["entry_id", "journal_id", "created_at", "account_type",
                            "merchant_id", "amount", "currency", "memo"])
                q = (db.session.query(JournalEntry, Account)
                     .join(Account, JournalEntry.account_id == Account.id)
                     .filter(Account.is_test.is_(False),
                             JournalEntry.created_at >= start,
                             JournalEntry.created_at < end)
                     .order_by(JournalEntry.id))
                total = 0
                for je, acct in q.all():
                    total += je.amount
                    w.writerow([je.id, je.journal_id, je.created_at.isoformat(),
                                acct.type.value, acct.merchant_id, je.amount,
                                je.currency, je.memo or ""])

            # 2. Merchant register with KYC status.
            with open(_os.path.join(outdir, "merchants.csv"), "w",
                      newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["id", "name", "email", "handle", "kyc_status",
                            "is_active", "is_managed", "created_at"])
                for m in Merchant.query.order_by(Merchant.id).all():
                    w.writerow([m.id, m.name, m.email, m.handle, m.kyc_status,
                                m.is_active, getattr(m, "is_managed", False),
                                m.created_at.isoformat() if m.created_at else ""])

            # 3. Flow-of-funds summary (live money only).
            from .models import TxnStatus as _TS
            tin = (db.session.query(safunc.coalesce(safunc.sum(Transaction.amount), 0))
                   .filter(Transaction.is_test.is_(False),
                           Transaction.completed_at >= start,
                           Transaction.completed_at < end,
                           Transaction.status == _TS.SUCCEEDED)
                   .scalar() or 0)
            tout = (db.session.query(safunc.coalesce(safunc.sum(Payout.amount), 0))
                    .filter(Payout.is_test.is_(False),
                            Payout.created_at >= start,
                            Payout.created_at < end)
                    .scalar() or 0)
            with open(_os.path.join(outdir, "summary.txt"), "w", encoding="utf-8") as fh:
                fh.write(
                    f"Samsoftpay regulator pack — {month}\n"
                    f"Generated: {datetime.utcnow().isoformat()}Z\n\n"
                    f"Money IN (succeeded charges, live): {int(tin)}\n"
                    f"Money OUT (payouts initiated, live): {int(tout)}\n"
                    f"Journal entries sum for the month (must be 0): {total}\n\n"
                    "All figures generated from the double-entry journal.\n"
                    "Client money is ledgered per merchant (accounts.csv shows\n"
                    "the split); the journal is append-only — corrections are\n"
                    "reversing entries, never edits.\n")
            print(f"regulator pack written to {outdir}/ "
                  f"(journal sum for month = {total}; MUST be 0)")

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

    @app.cli.command("reset-vending")
    @click.argument("public_id")
    @click.option("--redispense", is_flag=True, default=False,
                  help="After confirming NO product came out, retry the dispense.")
    def reset_vending(public_id, redispense):
        """Recover a vending order stuck in DISPENSING (worker died mid-dispense).

        dispense_for_link claims the order pending->dispensing and only THEN calls
        the supplier. If the worker dies in that window, the order is stuck
        DISPENSING forever — retry_dispense refuses it (it assumes a live in-flight
        call) and no sweep clears it, while the customer's charge is SUCCEEDED.

        ONLY run this after you have confirmed the real outcome (the XY §2.2.3
        dispense-result callback, or a physical check) — the original
        ApplyExportGoods may already have reached the machine, so a blind retry
        risks a DOUBLE dispense. Default just clears the stuck state to `failed`
        (visible, refundable). Pass --redispense ONLY when you have confirmed
        nothing came out and want to send the product again.
        """
        from .models import PaymentLink, Transaction, TxnStatus
        from .services import vending

        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=public_id).first()
            if not link:
                print(f"No vending order {public_id}")
                return
            if link.vending_status != "dispensing":
                print(f"REFUSING: {public_id} is vending_status="
                      f"{link.vending_status!r}, not 'dispensing' — nothing stuck to reset.")
                return
            txn = (db.session.get(Transaction, link.transaction_id)
                   if link.transaction_id else None)
            if txn is None or txn.status != TxnStatus.SUCCEEDED:
                print(f"REFUSING: {public_id} has no SUCCEEDED charge.")
                return

            if redispense:
                # Operator has confirmed no product came out. Clear the stuck
                # claim to pending and dispense again through the normal guard.
                link.vending_status = "pending"
                db.session.commit()
                ok = vending.dispense_for_link(link, txn)
                print(f"{public_id}: re-dispense {'succeeded' if ok else 'failed'} "
                      f"(now {link.vending_status}).")
            else:
                link.vending_status = "failed"
                link.vending_error = "reset from stuck DISPENSING (worker died mid-dispense)"
                db.session.commit()
                print(f"{public_id}: cleared stuck DISPENSING -> failed. "
                      f"Refund the customer or re-run with --redispense once you have "
                      f"confirmed no product came out.")

    @app.cli.command("signing-profiles")
    def signing_profiles():
        """List machine-vendor signing profiles (Machine Integration Standard)."""
        from .models import SigningProfile
        with app.app_context():
            rows = SigningProfile.query.order_by(SigningProfile.vendor).all()
            print("DB signing profiles:")
            if not rows:
                print("  (none — XY runs from the built-in default profile)")
            for r in rows:
                print(f"  {r.vendor:12} {r.display_name}  legacy={r.is_legacy_shim} "
                      f"order={r.sign_order} replay={r.replay_window_seconds}s "
                      f"body={r.dispense_body_style}")
            print("Built-in defaults (used when no row): "
                  "xy (legacy shim), _default (clean Standard v1)")

    @app.cli.command("signing-profile-set")
    @click.argument("vendor")
    @click.option("--display-name", default=None)
    @click.option("--non-signed", default=None,
                  help="comma list of fields EXCLUDED from the sign base")
    @click.option("--alias", "aliases", multiple=True,
                  help="canonical=alt spelling pair (repeatable)")
    @click.option("--order", type=click.Choice(["alpha", "alpha_swap"]), default=None)
    @click.option("--swap", "swaps", multiple=True,
                  help="a,b key pair swapped for alpha_swap (repeatable)")
    @click.option("--replay-window", type=int, default=None,
                  help="seconds; 0 disables the freshness/replay check")
    @click.option("--dispense-path", default=None)
    @click.option("--body-style", default=None)
    @click.option("--extra", "extras", multiple=True,
                  help="key=value extra dispense-body field (repeatable)")
    @click.option("--legacy/--no-legacy", "legacy", default=None)
    def signing_profile_set(vendor, display_name, non_signed, aliases, order, swaps,
                            replay_window, dispense_path, body_style, extras, legacy):
        """Create or edit a vendor signing profile.

        A NEW profile starts from the CLEAN Standard v1 (strict alphabetical, no
        aliases, 5-min replay window) and applies your overrides — so a new
        machine vendor conforms to us, not the other way round.
        """
        import json as _json
        from .models import SigningProfile
        from .services.signing import CLEAN_PROFILE
        vendor = vendor.strip().lower()
        with app.app_context():
            row = SigningProfile.query.filter_by(vendor=vendor).first()
            created = row is None
            if created:
                row = SigningProfile(
                    vendor=vendor,
                    display_name=display_name or f"{vendor} machine vendor",
                    non_signed_fields=_json.dumps(sorted(CLEAN_PROFILE.non_signed)),
                    field_aliases="{}",
                    sign_order="alpha", sign_order_swaps="[]",
                    replay_window_seconds=CLEAN_PROFILE.replay_window_seconds,
                    dispense_path=CLEAN_PROFILE.dispense_path,
                    dispense_body_style=CLEAN_PROFILE.dispense_body_style,
                    dispense_extra=_json.dumps(CLEAN_PROFILE.dispense_extra),
                    is_legacy_shim=False)
                db.session.add(row)
            if display_name is not None:
                row.display_name = display_name
            if non_signed is not None:
                row.non_signed_fields = _json.dumps(
                    [f.strip() for f in non_signed.split(",") if f.strip()])
            if aliases:
                row.field_aliases = _json.dumps(dict(a.split("=", 1) for a in aliases))
            if order is not None:
                row.sign_order = order
            if swaps:
                row.sign_order_swaps = _json.dumps([list(s.split(",", 1)) for s in swaps])
            if replay_window is not None:
                row.replay_window_seconds = replay_window
            if dispense_path is not None:
                row.dispense_path = dispense_path
            if body_style is not None:
                row.dispense_body_style = body_style
            if extras:
                row.dispense_extra = _json.dumps(dict(e.split("=", 1) for e in extras))
            if legacy is not None:
                row.is_legacy_shim = legacy
            db.session.commit()
            print(f"{'Created' if created else 'Updated'} signing profile '{vendor}': "
                  f"{row.display_name} (order={row.sign_order}, "
                  f"replay={row.replay_window_seconds}s, body={row.dispense_body_style}).")

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


    @app.cli.command("repair-balances")
    @click.option("--dry-run", is_flag=True, default=False,
                  help="Report drift only — write nothing.")
    @click.option("--account-id", type=int, default=None,
                  help="Repair a single account instead of every account.")
    def repair_balances(dry_run, account_id):
        """Recompute every Account.cached_balance from the journal and repair drift.

        Nightly reconciliation DETECTS a cached_balance that disagrees with the
        journal but there was no tool to fix one, so remediation meant hand-written
        UPDATE statements against a live money database. This is that tool.

        The journal is the source of truth and is NEVER touched here: entries are
        append-only (a correction is a reversing posting, not an edit). The only
        column this command can write is the derived cache. Each repair takes the
        same row lock the payout path takes (ledger.lock_account_for_update) so a
        concurrent posting cannot be overwritten, re-reads the truth under that
        lock, and is written to the audit trail with the old and new values.

        Usage:
            flask repair-balances --dry-run          # report only
            flask repair-balances                    # repair every drifted account
            flask repair-balances --account-id 42    # repair just one
        """
        from .models import Account
        from .services import ledger
        from .services.audit import log_event

        with app.app_context():
            query = Account.query
            if account_id is not None:
                query = query.filter(Account.id == account_id)
            accounts = query.order_by(Account.id).all()
            if not accounts:
                if account_id is not None:
                    print(f"No account with id={account_id}.")
                else:
                    print("No accounts exist yet — nothing to check.")
                return

            def _label(a):
                type_name = a.type.value if hasattr(a.type, "value") else str(a.type)
                who = f"merchant#{a.merchant_id}" if a.merchant_id else "platform"
                return (f"account#{a.id} {type_name} {who} {a.currency} "
                        f"[{'test' if a.is_test else 'live'}]")

            drifted = []
            for acct in accounts:
                cached = int(acct.cached_balance or 0)
                truth = ledger.recompute_balance(acct)
                if truth != cached:
                    drifted.append(acct.id)
                    print(f"  DRIFT {_label(acct)}: cached={cached:,} "
                          f"journal={truth:,} delta={truth - cached:,}")

            print("-" * 64)
            print(f"Checked {len(accounts)} account(s); {len(drifted)} drifted.")
            if not drifted:
                print("Ledger cache agrees with the journal. Nothing to repair.")
                return
            if dry_run:
                print("--dry-run: nothing written. Re-run without --dry-run to repair.")
                return

            repaired = 0
            for acct_id in drifted:
                acct = db.session.get(Account, acct_id)
                if acct is None:
                    continue
                # Lock first, THEN re-read the truth: between the scan above and
                # now a real posting may have landed, and it must not be clobbered.
                ledger.lock_account_for_update(acct)
                old = int(acct.cached_balance or 0)
                new = ledger.recompute_balance(acct)
                if new == old:
                    db.session.rollback()
                    print(f"  SKIP  {_label(acct)}: drift resolved itself before the lock.")
                    continue
                acct.cached_balance = new
                db.session.commit()
                log_event(
                    "ledger.balance_repaired",
                    merchant_id=acct.merchant_id,
                    resource_id=f"account:{acct.id}",
                    detail={"old": old, "new": new, "delta": new - old,
                            "currency": acct.currency,
                            "is_test": bool(acct.is_test),
                            "source": "flask repair-balances"},
                )
                repaired += 1
                print(f"  FIXED {_label(acct)}: {old:,} -> {new:,}")

            print("-" * 64)
            print(f"Repaired {repaired} account(s). The journal was not modified.")
