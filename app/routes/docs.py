"""API documentation page — plus machine-readable variants.

Surfaces, one source of truth each:
- GET /docs               — the human HTML page (templates/docs.html).
- GET /docs.md            — the complete docs as raw markdown (templates/docs.md),
                            served text/markdown for agents/LLMs and curl users.
- GET /docs/llms.txt      — an llms.txt index (https://llmstxt.org standard) pointing
                            at the above, so a crawler/agent can discover the
                            agent-friendly variant without scraping HTML.
- GET /docs/changelog     — the public API changelog (CHANGELOG.md at the repo root)
                            rendered as HTML. One canonical place integrators learn
                            about API changes before anything breaks.
- GET /docs/changelog.md  — the same changelog as raw markdown.

docs.md and CHANGELOG.md are read from disk verbatim (NOT rendered through Jinja):
they contain JSON/code samples full of braces, and they must never depend on
request context or autoescape rules. The changelog HTML page uses a deliberately
tiny, dependency-free markdown-to-HTML converter — CHANGELOG.md is a controlled
file we author ourselves, not arbitrary input.
"""
import html as _htmlmod
import os
import re as _re

from flask import Blueprint, Response, render_template
from markupsafe import Markup

bp = Blueprint("docs", __name__)

_LLMS_TXT = """\
# Samsoftpay

> Payment gateway API for Uganda: MTN Mobile Money collections and disbursements, hosted checkout, payment links, vending-machine payments, subaccount splits and signed webhooks. Base URL https://api.samsoftpay.com, Bearer-key auth, deterministic sandbox.

## Docs

- [API documentation (HTML)](https://api.samsoftpay.com/docs): full integration guide for humans
- [API documentation (Markdown)](https://api.samsoftpay.com/docs.md): the same docs as raw markdown — the agent-friendly variant, ideal for LLMs and programmatic consumption
- [API changelog (HTML)](https://api.samsoftpay.com/docs/changelog): every API-visible change, newest first, with the stability promise (response shapes are additive-only; webhook events are only ever added)
- [API changelog (Markdown)](https://api.samsoftpay.com/docs/changelog.md): the same changelog as raw markdown

## Optional

- [Service status](https://api.samsoftpay.com/status): live platform status
- [Terms of service](https://api.samsoftpay.com/terms)
- [Privacy policy](https://api.samsoftpay.com/privacy)
"""


def _read_docs_md() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "templates", "docs.md")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_changelog_md() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "..", "CHANGELOG.md")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------- minimal markdown renderer (changelog only) ----------

def _md_inline(text: str) -> str:
    """Inline markdown on an already-HTML-escaped string: code, bold, links."""
    text = _re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = _re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _changelog_html(md: str) -> str:
    """Convert the changelog markdown to HTML.

    Supports exactly what CHANGELOG.md uses: #/##/### headings, blockquotes,
    unordered lists (with indented continuation lines), horizontal rules and
    paragraphs. Everything is HTML-escaped before inline formatting, so the
    output is safe even if a sample string contains angle brackets.
    """
    out: list[str] = []
    items: list[str] = []          # open <ul> items (last one may grow)
    quote: list[str] = []          # open <blockquote> lines
    para: list[str] = []           # open <p> lines

    def flush_para():
        if para:
            out.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_list():
        if items:
            out.append("<ul>" + "".join(
                "<li>" + _md_inline(i) + "</li>" for i in items) + "</ul>")
            items.clear()

    def flush_quote():
        if quote:
            out.append("<blockquote><p>" + _md_inline(" ".join(quote)) +
                       "</p></blockquote>")
            quote.clear()

    def flush_all():
        flush_para()
        flush_list()
        flush_quote()

    for raw in md.splitlines():
        line = _htmlmod.escape(raw.rstrip(), quote=False)
        stripped = line.strip()
        if not stripped:
            flush_all()
            continue
        if stripped == "---":
            flush_all()
            out.append("<hr>")
            continue
        m = _re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_all()
            level = len(m.group(1))
            out.append(f"<h{level}>" + _md_inline(m.group(2)) + f"</h{level}>")
            continue
        if stripped.startswith("&gt; ") or stripped == "&gt;":
            flush_para()
            flush_list()
            quote.append(stripped[5:] if len(stripped) > 4 else "")
            continue
        if stripped.startswith("- "):
            flush_para()
            flush_quote()
            items.append(stripped[2:])
            continue
        if items and raw.startswith("  "):
            # continuation of the previous list item
            items[-1] += " " + stripped
            continue
        flush_list()
        flush_quote()
        para.append(stripped)
    flush_all()
    return "".join(out)


# ---------- routes ----------

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


@bp.get("/docs/changelog")
def changelog():
    body = Markup(_changelog_html(_read_changelog_md()))
    return render_template("docs_changelog.html", changelog_body=body)


@bp.get("/docs/changelog.md")
def changelog_markdown():
    return Response(_read_changelog_md(), mimetype="text/markdown",
                    headers={"Content-Type": "text/markdown; charset=utf-8"})
