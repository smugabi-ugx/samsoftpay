"""Application factory.

Keeping this thin on purpose. All real logic lives in services/.
"""
import logging
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


def _init_sentry() -> None:
    """Initialise Sentry error tracking IF a SENTRY_DSN is configured.

    No-op when SENTRY_DSN is unset (local/dev) or the SDK isn't installed, so this
    is safe to always call. Captures unhandled exceptions from Flask and Celery.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.celery import CeleryIntegration
    except ImportError:
        return
    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration(), CeleryIntegration()],
        environment=os.environ.get("SENTRY_ENV", "production" if os.environ.get("RENDER") else "dev"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,   # never ship customer PII to Sentry
    )


def _assert_production_env() -> None:
    """Fail fast on Render if critical secrets/config are missing or insecure."""
    if not os.environ.get("RENDER"):
        return   # local dev — skip
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or "sqlite" in db_url:
        sys.exit(
            "FATAL: DATABASE_URL is missing or empty on Render.\n"
            "Fix: go to your Render service → Environment → add DATABASE_URL "
            "with the Internal Connection String from your PostgreSQL database.\n"
            "If you have not created a database yet: Render dashboard → New + → PostgreSQL."
        )
    # Real-money safety: refuse to boot in production with default/placeholder secrets.
    insecure = []
    secret_key = os.environ.get("SECRET_KEY", "")
    if not secret_key or secret_key == "dev-only-do-not-use-in-prod":
        insecure.append("SECRET_KEY")
    wh_secret = os.environ.get("WEBHOOK_SIGNING_SECRET", "")
    if not wh_secret or wh_secret.startswith("whsec_demo") or wh_secret == "whsec_change_me_in_production":
        insecure.append("WEBHOOK_SIGNING_SECRET")
    if insecure:
        sys.exit(
            "FATAL: the following secrets are missing or still set to insecure defaults "
            f"on Render: {', '.join(insecure)}.\n"
            "Set strong random values in Render → Environment before going live. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


def create_app(config: dict | None = None) -> Flask:
    _assert_production_env()
    app = Flask(__name__, template_folder="templates")
    from datetime import timedelta

    _db_uri = _fix_db_url(os.environ.get("DATABASE_URL", "sqlite:///samsoftpay.db"))
    # Pool tuning only applies to server databases. SQLite uses a different pool
    # implementation that does not accept pool_size/max_overflow.
    if _db_uri.startswith("postgresql"):
        _engine_options = {
            "pool_size": int(os.environ.get("DB_POOL_SIZE", "20")),
            "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "20")),
            "pool_recycle": 1800,    # recycle connections every 30 min (avoid stale)
            "pool_pre_ping": True,   # check a connection is alive before using it
            "pool_timeout": 30,
        }
    else:
        _engine_options = {"pool_pre_ping": True}

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-do-not-use-in-prod"),
        # ── Secure session cookies ──────────────────────────────────────────
        SESSION_COOKIE_NAME="ssp_sid",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # True on Render, False locally
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),  # idle timeout
        SESSION_REFRESH_EACH_REQUEST=True,       # reset 30-min window on activity
        SQLALCHEMY_DATABASE_URI=_db_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # Connection pool — the default (5 + 10 overflow) saturates under load and
        # causes request timeouts. Only valid for server DBs (Postgres); SQLite
        # rejects these kwargs, so we apply them conditionally below.
        SQLALCHEMY_ENGINE_OPTIONS=_engine_options,
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
        # ---- Celery / Redis ----
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        # Rate-limit storage: shared Redis in production so limits hold ACROSS
        # gunicorn workers (in-memory would let each worker keep its own counter,
        # multiplying the effective limit). Local dev stays in-memory.
        RATELIMIT_STORAGE_URI=(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            if os.environ.get("RENDER")
            else "memory://"
        ),
        RATELIMIT_HEADERS_ENABLED=True,
    )
    if config:
        app.config.update(config)

    _init_sentry()

    db.init_app(app)

    from .celery_app import init_celery
    init_celery(app)

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

    # ---- Request IDs: tag every request so logs can be traced end to end ----
    import uuid as _uuid
    from flask import g, request

    @app.before_request
    def _assign_request_id():
        g.request_id = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:16]

    @app.after_request
    def _echo_request_id(response):
        rid = g.get("request_id")
        if rid:
            response.headers["X-Request-ID"] = rid
        return response

    # ---- Security headers (defense-in-depth; Cloudflare also fronts the app) ----
    # CSP is permissive on purpose: the existing templates use inline scripts/styles,
    # Google Fonts, and CDN libs. It still blocks framing by others, locks form-action
    # and base-uri to self, and restricts sources. Override via the CONTENT_SECURITY_POLICY
    # env var (or set CSP_ENABLED=0) if a page ever breaks — no redeploy of code needed.
    _default_csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "font-src 'self' https: data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https:; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    )
    _csp = os.environ.get("CONTENT_SECURITY_POLICY", _default_csp)
    _csp_enabled = os.environ.get("CSP_ENABLED", "1") != "0"

    @app.after_request
    def _security_headers(response):
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # HSTS only in production (always HTTPS via Render/Cloudflare); browsers ignore it on HTTP.
        if os.environ.get("RENDER"):
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if _csp_enabled:
            h.setdefault("Content-Security-Policy", _csp)
        return response

    class _RequestIdFilter(logging.Filter):
        def filter(self, record):
            try:
                from flask import g as _g, has_request_context
                record.request_id = _g.get("request_id", "-") if has_request_context() else "-"
            except Exception:
                record.request_id = "-"
            return True

    # Attach the filter + a format that includes the request id, without clobbering
    # any handler gunicorn/Render already installed.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [req:%(request_id)s] %(name)s: %(message)s"
    ))
    _handler.addFilter(_RequestIdFilter())
    app.logger.addHandler(_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False

    # ---- Health checks (for Render + external uptime monitors) ----
    @app.get("/healthz")
    def healthz():
        """Liveness + DB connectivity. Returns 200 only if the database answers."""
        from flask import jsonify
        from sqlalchemy import text
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", database="up"), 200
        except Exception as exc:  # pragma: no cover
            app.logger.error("healthz DB check failed: %s", exc)
            return jsonify(status="degraded", database="down"), 503

    @app.get("/livez")
    def livez():
        """Pure liveness — process is up. No external dependencies checked."""
        from flask import jsonify
        return jsonify(status="ok"), 200

    return app
