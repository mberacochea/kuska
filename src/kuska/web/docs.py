"""Routes: shared docs."""

from __future__ import annotations

from flask import Response, make_response, redirect, render_template, request

from ..db import HUMAN
from ..store import docs_delete, docs_get, docs_list, docs_set
from .helpers import (
    _bad_request,
    htmx,
    validate_doc_content,
    validate_doc_key,
    wants_fragment,
)


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    db = ctx.db
    doc_editor = ctx.doc_editor
    docs_table = ctx.docs_table
    _toast = ctx._toast

    # ========== ROUTES: Docs ==========

    @app.get("/docs")
    def docs_page() -> str | Response:
        """GET /docs - the docs list and the create form.

        ?open=<key> is the old inline-editor link and now redirects to the
        doc's own page.
        """
        open_key = request.args.get("open")
        if open_key:
            return redirect(f"/docs/{open_key}", 301)
        return render_template("docs.html", page="docs", docs_table=docs_table())

    @app.post("/docs")
    def create_doc() -> str | Response | tuple[str, int]:
        """POST /docs - Create a new doc, then go to its page."""
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

        return _goto(f"/docs/{key}")

    @app.get("/docs/<key>")
    def read_doc(key: str) -> str | tuple[str, int]:
        """GET /docs/<key> - the doc's own page; htmx gets just the editor."""
        if docs_get(db(), key) is None:
            return "", 404
        editor = doc_editor(key)
        if wants_fragment():
            return editor
        return render_template("doc.html", page="docs", key=key, doc_editor=editor)

    @app.post("/docs/<key>")
    def save_doc(key: str) -> tuple[str, int]:
        """POST /docs/<key> - Save doc content; answers with the editor and a toast."""
        content = request.form.get("content", "")

        # Validate content length
        content_error = validate_doc_content(content)
        if content_error:
            return _bad_request(doc_editor(key), "content", content_error)

        try:
            docs_set(db(), key, content, HUMAN)
        except (ValueError, OSError) as exc:
            return _bad_request(doc_editor(key), "form", f"Failed to save doc: {exc}")

        return doc_editor(key) + _toast("saved"), 200

    @app.delete("/docs/<key>")
    def delete_doc(key: str) -> str | Response:
        """DELETE /docs/<key> - Delete a doc.

        From the list (hx-target #docs) the table is swapped; from the doc's
        own page there is nothing left to show, so go back to the list.
        """
        docs_delete(db(), key)
        if htmx.target == "docs":
            return docs_table()
        return _goto("/docs")

    def _goto(url: str) -> Response:
        """Send the browser to `url`: HX-Redirect for htmx, a 303 otherwise."""
        if htmx:
            response = make_response("")
            response.headers["HX-Redirect"] = url
            return response
        return redirect(url, 303)
