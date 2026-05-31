"""Application factory.

Keeping this thin on purpose. All real logic lives in services/.
"""
import os
import sys

# Load .env before anything else reads os.environ.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask
from .extensions import db


def _fix_db_url(url: str) -> str:
    """Render provides postgres:// — SQLAlchemy 2.x requires postgresql://"""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def _assert_production_env() -> None:
    """Fail fast on Render if DATABASE_URL was not injected (would silently use SQLite)."""
    if os.environ.get("RENDER") and "sqlite" in os.environ.get("DATABASE_URL", "sqlite://"):
        sys.exit(
            "FATAL: DATABASE_URL is missing or still points to SQLite on Render. "
            "Attach the samsoftpay-db database in the Render dashboard."
        )


def create_app(config: dict | None = None) -> Flask:
    _assert_production_env()
    app = Flask(__name__, template_folder="templates")
    from datetime import timedelta
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-do-not-use-in-prod"),
        # ── Secure session cookies ──────────────────────────────────────────
        SESSION_COOKIE_NAME="ssp_sid",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # True on Render, False locally
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),  # idle timeout
        SESSION_REFRESH_EACH_REQUEST=True,       # reset 30-min window on activity
        SQLALCHEMY_DATABASE_URI=_fix_db_url(
            os.environ.get("DATABASE_URL", "sqlite:///samsoftpay.db")
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        WEBHOOK_SIGNING_SECRET=os.environ.get(
            "WEBHOOK_SIGNING_SECRET", "whsec_demo_replace_me"
        ),
        RAIL_CALLBACK_DELAY_SECONDS=int(
            os.environ.get("RAIL_CALLBACK_DELAY_SECONDS", "5")
        ),
        RAIL_SUCCESS_PROBABILITY=float(
            os.environ.get("RAIL_SUCCESS_PROBABILITY", "0.85")
        ),
        # ---- MTN MoMo real-rail config (only used when MOMO_USE_REAL=1) ----
        MOMO_USE_REAL=os.environ.get("MOMO_USE_REAL", "0") == "1",
        MOMO_BASE_URL=os.environ.get(
            "MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com"
        ),
        MOMO_TARGET_ENV=os.environ.get("MOMO_TARGET_ENV", "sandbox"),
        MOMO_CURRENCY=os.environ.get("MOMO_CURRENCY", "EUR"),  # sandbox: EUR
        MOMO_SUBSCRIPTION_KEY=os.environ.get("MOMO_SUBSCRIPTION_KEY", ""),
        MOMO_API_USER=os.environ.get("MOMO_API_USER", ""),
        MOMO_API_KEY=os.environ.get("MOMO_API_KEY", ""),
        # ---- MTN MoMo Disbursement (payout) credentials ----
        MOMO_DISBURSEMENT_SUBSCRIPTION_KEY=os.environ.get(
            "MOMO_DISBURSEMENT_SUBSCRIPTION_KEY", ""
        ),
        MOMO_DISBURSEMENT_API_USER=os.environ.get("MOMO_DISBURSEMENT_API_USER", ""),
        MOMO_DISBURSEMENT_API_KEY=os.environ.get("MOMO_DISBURSEMENT_API_KEY", ""),
        # ---- Email / 2FA ----
        MAIL_HOST=os.environ.get("MAIL_HOST", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", "noreply@samsoftpay.com"),
        # ---- Visa / Card (Flutterwave) ----
        FLUTTERWAVE_SECRET_KEY=os.environ.get("FLUTTERWAVE_SECRET_KEY", ""),
        BASE_URL=os.environ.get("BASE_URL", "http://localhost:5000"),
        # ---- Crypto via ChangeNow ----
        CHANGENOW_API_KEY=os.environ.get("CHANGENOW_API_KEY", ""),
        CHANGENOW_RECEIVING_ADDRESS=os.environ.get("CHANGENOW_RECEIVING_ADDRESS", ""),
        CHANGENOW_RECEIVING_NETWORK=os.environ.get("CHANGENOW_RECEIVING_NETWORK", "bsc"),
    )
    if config:
        app.config.update(config)

    db.init_app(app)

    from .extensions import limiter, login_manager, migrate
    limiter.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    from .models import Merchant

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(Merchant, int(user_id))

    from .routes.api import bp as api_bp
    from .routes.dashboard import bp as dash_bp
    from .routes.webhooks_inbound import bp as inbound_bp
    from .routes.checkout import bp as checkout_bp
    from .routes.auth import bp as auth_bp
    from .routes.docs import bp as docs_bp
    from .routes.kyc import bp as kyc_bp
    from .routes.giftcards import bp as giftcards_bp
    from .routes.seo import bp as seo_bp
    from .routes.subscriptions import bp as subs_bp
    from .routes.wallet import bp as wallet_bp
    from .routes.bills import bp as bills_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(inbound_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(kyc_bp)
    app.register_blueprint(giftcards_bp)
    app.register_blueprint(seo_bp)
    app.register_blueprint(subs_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(bills_bp)

    # CSRF protection for authenticated browser forms
    from flask_wtf.csrf import CSRFProtect
    csrf = CSRFProtect(app)
    # Exempt blueprints that are public (no session) or use Bearer tokens
    csrf.exempt(api_bp)
    csrf.exempt(inbound_bp)
    csrf.exempt(checkout_bp)   # public payment pages — no login session

    from . import cli  # noqa: F401
    cli.register(app)

    @app.errorhandler(403)
    def forbidden(e):
        from flask import render_template as _rt
        return _rt("403.html"), 403

    return app
