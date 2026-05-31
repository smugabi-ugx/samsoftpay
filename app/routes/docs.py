"""API documentation page."""
from flask import Blueprint, render_template

bp = Blueprint("docs", __name__)


@bp.get("/docs")
def docs():
    return render_template("docs.html")
