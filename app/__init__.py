"""Application factory.

Keeping this thin on purpose. All real logic lives in services/.
"""
import os

# Load .env before anything else reads os.environ.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask
from .extensions import db


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-do-not-use-in-prod"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///pesademo.db"
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
    )
    if config:
        app.config.update(config)

    db.init_app(app)

    from .routes.api import bp as api_bp
    from .routes.dashboard import bp as dash_bp
    from .routes.webhooks_inbound import bp as inbound_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(inbound_bp)

    from . import cli  # noqa: F401
    cli.register(app)
    return app
