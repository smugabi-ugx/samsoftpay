"""API documentation page — plus machine-readable variants.

Three surfaces, one source of truth:
- GET /docs          — the human HTML page (templates/docs.html).
- GET /docs.md       — the complete docs as raw markdown (templates/docs.md),
                       served text/markdown for agents/LLMs and curl users.
- GET /docs/llms.txt — an llms.txt index (https://llmstxt.org standard) pointing
                       at the above, so a crawler/agent can discover the
                       agent-friendly variant without scraping HTML.

docs.md is read from disk verbatim (NOT rendered through Jinja): it contains
JSON/code samples full of braces, and it must never depend on request context
or autoescape rules.
"""
import os

from flask import Blueprint, Response, render_template

bp = Blueprint("docs", __name__)

_LLMS_TXT = """\
# Samsoftpay

> Payment gateway API for Uganda: MTN Mobile Money collections and disbursements, hosted checkout, payment links, vending-machine payments, subaccount splits and signed webhooks. Base URL https://api.samsoftpay.com, Bearer-key auth, deterministic sandbox.

## Docs

- [API documentation (HTML)](https://api.samsoftpay.com/docs): full integration guide for humans
- [API documentation (Markdown)](https://api.samsoftpay.com/docs.md): the same docs as raw markdown — the agent-friendly variant, ideal for LLMs and programmatic consumption

## Optional

- [Service status](https://api.samsoftpay.com/status): live platform status
- [Terms of service](https://api.samsoftpay.com/terms)
- [Privacy policy](https://api.samsoftpay.com/privacy)
"""


def _read_docs_md() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "templates", "docs.md")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@bp.get("/docs")
def docs():
    return render_template("docs.html")


@bp.get("/docs.md")
def docs_markdown():
    return Response(_read_docs_md(), mimetype="text/markdown",
                    headers={"Content-Type": "text/markdown; charset=utf-8"})


@bp.get("/docs/llms.txt")
def llms_txt():
    return Response(_LLMS_TXT, mimetype="text/plain",
                    headers={"Content-Type": "text/plain; charset=utf-8"})
