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
