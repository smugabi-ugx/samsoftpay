from flask import request
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter

db = SQLAlchemy()


def _rate_limit_key():
    """Key by API key so limits are per-merchant, not per-IP."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return request.remote_addr


limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
