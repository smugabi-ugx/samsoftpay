from flask import request
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_login import LoginManager
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login_page"
login_manager.login_message = "Please log in to access that page."


def _rate_limit_key():
    """Key by API key so limits are per-merchant, not per-IP.

    Unauthenticated requests key on the REAL client IP (CF-Connecting-IP /
    rightmost X-Forwarded-For) — `remote_addr` behind Render's proxy is the
    proxy hop, which put every visitor in ONE shared bucket: the first 5
    disputes/hour or 10 logins/minute platform-wide locked out everyone else.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    from .services.client_ip import real_client_ip
    return real_client_ip()


limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
