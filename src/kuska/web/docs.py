"""Routes: shared docs."""

from __future__ import annotations

from flask import render_template, request

from .. import tables as tbl
from ..db import HUMAN
from ..store import docs_get, docs_list, docs_set
from .helpers import (
    _bad_request,
    validate_doc_content,
    validate_doc_key,
    wants_fragment,
)


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    db = ctx.db
    doc_editor = ctx.doc_editor
    docs_table = ctx.docs_table

    # ========== ROUTES: Docs ==========

    @app.get("/docs")
    def docs_page() -> str:
        """GET /docs - Display the shared docs page.

        ?open=<key> pre-opens that doc's editor, so a link from elsewhere
        (e.g. a search result) can land directly on it. An htmx click on a doc
        key hits this same URL but only swaps #doc-editor, so it gets the
        editor on its own.
        """
        open_key = request.args.get("open", "")
        editor = doc_editor(open_key) if open_key and docs_get(db(), open_key) is not None else '<div id="doc-editor"></div>'
        if open_key and wants_fragment():
            return editor
        return render_template("docs.html", page="docs", docs_table=docs_table(), doc_editor=editor)

    @app.post("/docs")
    def create_doc() -> tuple[str, int]:
        """POST /docs - Create a new doc with the given key."""
        key = request.form.get("key", "").strip()
        existing = {d["key"] for d in docs_list(db())}

        # Validate doc key
        key_error = validate_doc_key(key, existing)
        if key_error:
            return _bad_request(docs_table(), "key", key_error)

        try:
            if docs_get(db(), key) is None:
                docs_set(db(), key, "", HUMAN)
        except (ValueError, OSError) as exc:
            return _bad_request(docs_table(), "form", f"Failed to create doc: {exc}")

        return doc_editor(key), 200

    @app.get("/docs/<key>")
    def read_doc(key: str) -> str:
        """GET /docs/<key> - Get the doc editor for a specific doc."""
        return doc_editor(key)

    @app.post("/docs/<key>")
    def save_doc(key: str) -> tuple[str, int]:
        """POST /docs/<key> - Save doc content."""
        content = request.form.get("content", "")

        # Validate content length
        content_error = validate_doc_content(content)
        if content_error:
            return _bad_request(doc_editor(key), "content", content_error)

        try:
            docs_set(db(), key, content, HUMAN)
        except (ValueError, OSError) as exc:
            return _bad_request(doc_editor(key), "form", f"Failed to save doc: {exc}")

        return docs_table(), 200

    @app.post("/docs/<key>/delete")
    def delete_doc(key: str) -> str:
        """POST /docs/<key>/delete - Delete a doc."""
        tbl.delete_row(db(), "docs", key)
        return docs_table()
