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


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}


def _momo_use_real() -> bool:
    """Is the REAL MTN rail switched on?

    Tolerant parsing on purpose: an exact `== "1"` meant that setting this to
    "true" (or leaving a stray space) silently kept MTN mocked, which hides it
    from every live payment surface with no error anywhere. Anything we do not
    recognise is treated as OFF but logged loudly, because a misconfigured
    payment rail must never fail quietly.
    """
    raw = os.environ.get("MOMO_USE_REAL", "0")
    val = raw.strip().strip('"').strip("'").lower()
    if val in _TRUTHY:
        return True
    if val not in _FALSY:
        logging.getLogger(__name__).error(
            "MOMO_USE_REAL=%r is not a recognised boolean — treating the MTN "
            "rail as MOCKED. Use 1/true/yes/on to enable the real rail.", raw)
    return False


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
        # Static assets (font, CSS, JS, logo) get a 1-day browser cache AND
        # become edge-cacheable by Cloudflare — without this every navigation
        # re-fetched every asset across the ocean (Kampala->Oregon ~300ms RTT
        # each), which is what made page-to-page movement feel sticky.
        SEND_FILE_MAX_AGE_DEFAULT=86400,
        # KYC documents go on a mounted persistent disk on Render (survives
        # redeploys); unset locally -> instance_path. See render.yaml disk.
        KYC_UPLOAD_ROOT=os.environ.get("KYC_UPLOAD_ROOT"),
        # Enforce the '10MB per file' promise server-side (was unbounded).
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)),
        # ── Secure session cookies ──────────────────────────────────────────
        SESSION_COOKIE_NAME="ssp_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # True on Render, False locally
        # The app now answers on BOTH samsoftpay.com (apex) and
        # api.samsoftpay.com. Cookies are per-host by default, so logging in on
        # one host looked like a logout the moment navigation crossed to the
        # other (the "clicked wallet, got the login page" bug). A parent-domain
        # cookie makes one session span apex + www + api. Local dev keeps
        # host-only cookies so localhost works.
        SESSION_COOKIE_DOMAIN=(os.environ.get("SESSION_COOKIE_DOMAIN")
                               or (".samsoftpay.com" if os.environ.get("RENDER") else None)),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),  # sliding window; remember cookie re-auths after
        SESSION_REFRESH_EACH_REQUEST=True,       # reset the window on activity
        # ── Persistent "remember me" cookie ─────────────────────────────────
        # login_user(remember=True) writes this. Without it the session was a
        # BROWSER-SESSION cookie (no expiry) that mobile browsers evict the
        # moment they background the tab — the merchant looked logged out and
        # every protected page bounced to /login ("Please log in..." stacking
        # up). The remember cookie survives tab eviction and re-establishes the
        # session. It mirrors the session cookie's security exactly (same
        # parent domain, Secure on Render, HttpOnly, SameSite=Lax) so it can
        # never become the stale-shadow-cookie problem the session rename fixed.
        REMEMBER_COOKIE_NAME="ssp_remember",
        REMEMBER_COOKIE_DURATION=timedelta(days=14),
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=bool(os.environ.get("RENDER")),
        REMEMBER_COOKIE_DOMAIN=(os.environ.get("SESSION_COOKIE_DOMAIN")
                                or (".samsoftpay.com" if os.environ.get("RENDER") else None)),
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
        # Default is 1.0, not a coin flip: an ordinary sandbox test number should
        # never randomly fail. Deliberate failure comes from a TEST_PHONE_OUTCOMES
        # magic number (rails.py), the way Stripe/Pesapal test cards work. This
        # knob still exists for anyone who wants old-style randomness on purpose.
        RAIL_SUCCESS_PROBABILITY=float(
            os.environ.get("RAIL_SUCCESS_PROBABILITY", "1.0")
        ),
        # Hard upper bound on a single charge/payout amount. Default is the safe
        # 64-bit BigInteger ceiling (prevents an INSERT overflow crash); set
        # MAX_TXN_AMOUNT to MTN's real per-transaction limit to tighten it.
        MAX_TXN_AMOUNT=int(os.environ.get("MAX_TXN_AMOUNT", str((1 << 63) - 1))),
        # Per-KEY API rate limits (checkout's public 10/min limits are separate).
        # Raised from 30/200 + 10/100: a PLATFORM (KarlPOS: 61 shops on one key)
        # pools all traffic through one key, so the old values were a
        # platform-wide ceiling. Override per deployment via env.
        CHARGE_RATE_LIMIT=os.environ.get("CHARGE_RATE_LIMIT", "120 per minute;3000 per hour"),
        PAYOUT_RATE_LIMIT=os.environ.get("PAYOUT_RATE_LIMIT", "30 per minute;600 per hour"),
        # ---- MTN MoMo real-rail config (only used when MOMO_USE_REAL is on) ----
        # Accepts 1/true/yes/on (any case, whitespace-tolerant). This used to be
        # an exact `== "1"`, so setting it to "true" or leaving a trailing space
        # silently left the rail MOCKED — which hides MTN from checkout, empties
        # the subscription channel dropdown and sends top-ups to the sandbox
        # ledger, with nothing anywhere saying why. _momo_use_real() also warns
        # loudly on a value it does not recognise.
        MOMO_USE_REAL=_momo_use_real(),
        MOMO_BASE_URL=os.environ.get(
            "MOMO_BASE_URL", "https://sandbox.momodeveloper.mtn.com"
        ),
        MOMO_TARGET_ENV=os.environ.get("MOMO_TARGET_ENV", "sandbox"),
        MOMO_CURRENCY=os.environ.get("MOMO_CURRENCY", "EUR"),  # sandbox: EUR
        MOMO_SUBSCRIPTION_KEY=os.environ.get("MOMO_SUBSCRIPTION_KEY", ""),
        MOMO_API_USER=os.environ.get("MOMO_API_USER", ""),
        MOMO_API_KEY=os.environ.get("MOMO_API_KEY", ""),
        # Opt-in: set ONLY once MTN provisions the API user with a matching
        # providerCallbackHost (see rails_mtn_real). Enables instant completion
        # via /inbound/mtn/callback instead of poller latency.
        MOMO_CALLBACK_URL=os.environ.get("MOMO_CALLBACK_URL", ""),
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
        # ---- Operational alerting (see services/alerts.py) ----
        # Where critical conditions (reconciliation drift, stuck money, dead
        # worker) get routed. All optional — unset channels are simply skipped.
        ALERTS_ENABLED=os.environ.get("ALERTS_ENABLED", "1"),
        SLACK_WEBHOOK_URL=os.environ.get("SLACK_WEBHOOK_URL", ""),
        ALERT_EMAIL=os.environ.get("ALERT_EMAIL", os.environ.get("ADMIN_EMAIL", "")),
        # Worker/beat is considered dead if its heartbeat is older than this many
        # seconds (the heartbeat task runs every 60s). /ops/status returns 503 then.
        HEARTBEAT_STALE_SECONDS=int(os.environ.get("HEARTBEAT_STALE_SECONDS", "300")),
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
        # A Redis blip must NEVER 500 the payment API (audit-confirmed: with
        # Redis down, every rate-limited endpoint — the whole /v1 surface and
        # checkout — hard-500'd). Swallow limiter storage errors and fall back
        # to per-worker in-memory counting until Redis returns: briefly looser
        # limits beat a total outage.
        RATELIMIT_SWALLOW_ERRORS=True,
        RATELIMIT_IN_MEMORY_FALLBACK_ENABLED=True,
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
    from .routes.webhooks_xy import bp as xy_inbound_bp
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
    app.register_blueprint(xy_inbound_bp)
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
    from flask_wtf.csrf import CSRFError, CSRFProtect
    csrf = CSRFProtect(app)
    # Exempt blueprints that are public (no session) or use Bearer tokens
    csrf.exempt(api_bp)
    csrf.exempt(inbound_bp)
    csrf.exempt(xy_inbound_bp)   # supplier callback — signed, not session-based
    csrf.exempt(checkout_bp)   # public payment pages — no login session

    @app.errorhandler(CSRFError)
    def _csrf_error(e):
        # A stale/expired session used to surface as a bare "Bad Request — The
        # CSRF session token is missing" page (users read it as "the site is
        # down"). Recover gracefully: bounce back to the form with a message.
        from flask import flash as _flash, jsonify as _js
        from flask import redirect as _redir, request as _rq
        if _rq.path.startswith("/v1"):
            return _js(error="session token invalid — retry the request"), 400
        _flash("Your session expired — please try again.", "warning")
        # Only bounce back to OUR OWN pages — a foreign Referer must not turn
        # this handler into an open redirect.
        target = url_for_login()
        ref = _rq.referrer
        if ref:
            from urllib.parse import urlparse as _up
            if _up(ref).netloc in ("", _rq.host):
                target = ref
        return _redir(target), 303

    def url_for_login():
        from flask import url_for as _uf
        return _uf("auth.login_page")

    @app.get("/favicon.ico")
    def _favicon():
        # Serve the SVG bolt LOGO as the favicon (was the old mismatched PNG).
        # Every modern browser renders an SVG favicon, so the tab icon now
        # matches the brand logo. Browsers request /favicon.ico unconditionally.
        from flask import redirect as _redir, url_for as _uf
        return _redir(_uf("static", filename="img/logo.svg"), 301)

    from . import cli  # noqa: F401
    cli.register(app)

    @app.errorhandler(403)
    def forbidden(e):
        from flask import render_template as _rt, request as _rq, jsonify as _js
        if _rq.path.startswith("/v1"):
            return _js(error=e.description or "forbidden"), 403
        return _rt("403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        # A customer hitting a dead payment link used to see the bare default
        # Werkzeug page; a /v1 client must get JSON (blueprint handlers do not
        # fire for URLs that never matched a route).
        from flask import render_template as _rt, request as _rq, jsonify as _js
        if _rq.path.startswith("/v1"):
            return _js(error="not found"), 404
        return _rt("404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        from flask import render_template as _rt, request as _rq, jsonify as _js
        if _rq.path.startswith("/v1"):
            return _js(error="internal server error"), 500
        try:
            return _rt("500.html"), 500
        except Exception:
            return "Something went wrong on our side.", 500

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
        # Render sets RENDER_GIT_COMMIT on every service automatically — surfacing
        # it here is the difference between "curl one URL" and "open the Render
        # dashboard" when confirming a deploy actually landed. Unset outside Render.
        commit = os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", database="up", commit=commit), 200
        except Exception as exc:  # pragma: no cover
            app.logger.error("healthz DB check failed: %s", exc)
            return jsonify(status="degraded", database="down", commit=commit), 503

    @app.get("/livez")
    def livez():
        """Pure liveness — process is up. No external dependencies checked."""
        from flask import jsonify
        return jsonify(status="ok"), 200

    @app.get("/ops/status")
    def ops_status():
        """Worker/beat liveness, read from the Redis heartbeat.

        Served by the WEB process on purpose: a dead worker can't report its own
        death, so the freshness of the heartbeat it writes has to be checked from
        the outside. Point an external uptime monitor (UptimeRobot, Render) at
        this URL — a 503 means the Celery worker/beat pipeline has stopped even
        though the web app is fine. NOT wired into /healthz, so a dead worker
        never makes Render restart the healthy web service.
        """
        from flask import jsonify
        from .services.alerts import heartbeat_age_seconds
        threshold = app.config.get("HEARTBEAT_STALE_SECONDS", 300)
        age = heartbeat_age_seconds()
        if age is None:
            # Unknown: no Redis, or no heartbeat written yet (e.g. just deployed).
            # Report degraded-but-200 so a monitor doesn't page on a cold start;
            # a genuinely dead pipeline will flip to stale (503) once the key ages
            # out or, if it never wrote, stays here — surfaced in the body.
            return jsonify(status="unknown", worker="unknown",
                           heartbeat_age_seconds=None), 200
        fresh = age <= threshold
        return jsonify(
            status="ok" if fresh else "stale",
            worker="up" if fresh else "down",
            heartbeat_age_seconds=age,
            threshold_seconds=threshold,
        ), (200 if fresh else 503)

    # Cache-bust every static asset on each deploy WITHOUT touching a single
    # template: append ?v=<deploy commit> to every url_for('static', ...).
    # tokens.css is cached a day (SEND_FILE_MAX_AGE_DEFAULT) at the browser AND
    # Cloudflare edge, so a design/responsive change was invisible until the
    # cache expired. The version changes with each deploy -> fresh CSS on ship.
    _static_ver = os.environ.get("RENDER_GIT_COMMIT", "")[:12] or "dev"

    @app.url_defaults
    def _add_static_version(endpoint, values):
        if endpoint == "static" and "v" not in values:
            values["v"] = _static_ver

    return app
