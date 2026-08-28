"""Tiny server-side pagination helper.

`page_arg(name)` reads a page number from the query string, clamped to >= 1 and
failing safe to 1 on garbage. Pages with several independent lists pass a
distinct `name` per list (e.g. page_arg("page_wd")) so paging one list does not
move the others. Pair with the `pager()` macro in templates/_pagination.html and
SQLAlchemy's `query.paginate(page=..., per_page=..., error_out=False)`.
"""
from flask import request


def page_arg(name: str = "page") -> int:
    try:
        return max(1, int(request.args.get(name, 1)))
    except (TypeError, ValueError):
        return 1
