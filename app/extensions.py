from flask import request
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_login import LoginManager

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login_page"
login_manager.login_message = "Please log in to access that page."


def _rate_limit_key():
    """Key by API key so limits are per-merchant, not per-IP."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return request.remote_addr


limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
