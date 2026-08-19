"""SEO routes: sitemap.xml and robots.txt."""
from flask import Blueprint, Response, request

bp = Blueprint("seo", __name__)


@bp.get("/sitemap.xml")
def sitemap():
    base = request.host_url.rstrip("/")
    urls = [
        (base + "/",       "1.0", "weekly"),
        (base + "/docs",   "0.9", "monthly"),
        (base + "/signup", "0.8", "monthly"),
        (base + "/login",  "0.7", "monthly"),
    ]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for loc, priority, freq in urls:
        xml += f"  <url><loc>{loc}</loc><priority>{priority}</priority><changefreq>{freq}</changefreq></url>\n"
    xml += "</urlset>"
    return Response(xml, mimetype="application/xml")


@bp.get("/status")
def status_page():
    """Human-readable system status — the page a partner's engineer looks for.

    Live checks, not hardcoded claims (the old admin index once showed a
    fabricated '100%'): database via SELECT 1, worker/beat via the Redis
    heartbeat, rails from the actual runtime config.
    """
    from flask import current_app, render_template
    from sqlalchemy import text
    from ..extensions import db as _db
    from ..services.alerts import heartbeat_age_seconds

    try:
        _db.session.execute(text("SELECT 1"))
        api_ok = True
    except Exception:
        api_ok = False

    hb = heartbeat_age_seconds()
    threshold = current_app.config.get("HEARTBEAT_STALE_SECONDS", 300)
    worker = "operational" if (hb is not None and hb <= threshold) else (
        "unknown" if hb is None else "degraded")

    momo_real = bool(current_app.config.get("MOMO_USE_REAL"))
    components = [
        ("API & database", "operational" if api_ok else "degraded",
         "Core API, hosted checkout and the transaction ledger."),
        ("Background processing", worker,
         "Settlement sweeps, webhook delivery, reconciliation and payment polling."),
        ("MTN Mobile Money", "operational" if momo_real else "sandbox",
         "Collections and disbursements." if momo_real
         else "Running against the MTN sandbox while production onboarding completes."),
        ("Crypto (via ChangeNow)", "operational", "BTC / ETH / USDT settlement to UGX."),
        ("Airtel Money", "in development", "Rail under construction — not yet accepting live payments."),
        ("Cards (Visa / Mastercard)", "in development", "Rail under construction — not yet accepting live payments."),
    ]
    all_ok = api_ok and worker == "operational"
    return render_template("status.html", components=components, all_ok=all_ok)


@bp.get("/terms")
def terms():
    """Terms of Service — honest, specific, no boilerplate lorem."""
    from flask import render_template
    return render_template("terms.html")


@bp.get("/privacy")
def privacy():
    """Privacy Policy — states exactly what we collect and why."""
    from flask import render_template
    return render_template("privacy.html")


@bp.get("/robots.txt")
def robots():
    txt = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /dashboard/\n"
        "Disallow: /account\n"
        "Disallow: /kyc/\n"
        "Disallow: /verify-email\n"
        "Disallow: /verify-2fa\n"
        f"\nSitemap: {request.host_url}sitemap.xml\n"
    )
    return Response(txt, mimetype="text/plain")
