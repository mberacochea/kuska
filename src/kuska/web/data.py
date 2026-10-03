"""Routes: the generic table editor."""

from __future__ import annotations

from flask import render_template, request
from peewee import PeeweeException

from .. import tables as tbl
from ..markdown import render as md
from .helpers import wants_fragment


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    _toast = ctx._toast
    data_rows = ctx.data_rows
    db = ctx.db

    # ========== ROUTES: Data (Generic Table Editor) ==========

    @app.get("/data")
    @app.get("/data/<table>")
    def data_page(table: str = "tasks") -> str:
        """GET /data[/<table>] - Display the generic data table browser/editor.

        ?open=<pk> pre-opens that row's editor, so a link from elsewhere
        (e.g. a search result) can land directly on it.
        ?offset=<n> pages through the rows.

        Both are also hit by htmx from this page, each swapping a different
        element: an ?open link swaps #row-editor, a pagination link swaps
        #rows. Those two get their own fragment; a browser navigation gets the
        whole page.
        """
        if table not in tbl.TABLES:
            return render_template("data.html", page="data", tables=tbl.TABLES, table=None,
                                   note=f"no such table: {table}", insertable=[], types={}, rows="")
        spec = tbl.spec(table)
        open_pk = request.args.get("open", "")
        row_editor = render_template(
            "row_editor.html",
            table=table,
            fields=tbl.fields(table),
            editable=spec["editable"],
            pk=tbl.pk_name(table),
            pk_value=open_pk,
            row=tbl.get_row(db(), table, open_pk) if open_pk else None,
            md=md,
            markdown_fields={"description", "payload", "body", "content"},
        ) if open_pk and tbl.get_row(db(), table, open_pk) else '<div id="row-editor"></div>'

        if wants_fragment():
            if open_pk:
                return row_editor
            return data_rows(table, request.args.get("offset", 0, type=int))

        return render_template(
            "data.html",
            page="data",
            tables=tbl.TABLES,
            table=table,
            note=spec.get("note"),
            insertable=spec["insertable"],
            types=tbl.field_types(table),
            rows=data_rows(table, request.args.get("offset", 0, type=int)),
            row_editor=row_editor,
        )

    @app.get("/data/<table>/markdown-preview")
    def markdown_preview(table: str) -> str:
        """GET /data/<table>/markdown-preview - Show fullscreen markdown preview for a field."""
        pk_value = request.args.get("pk", "")
        field = request.args.get("field", "")
        row = tbl.get_row(db(), table, pk_value)
        if not row or field not in row:
            return '<div id="markdown-modal"></div>'
        content = row.get(field, "")
        if not content:
            content = "<p class='muted'>No content to preview.</p>"
        return render_template(
            "markdown_preview.html",
            table=table,
            field=field,
            content=content,
            md=md,
        )

    @app.get("/data/<table>/rows")
    def data_rows_fragment(table: str) -> str:
        """GET /data/<table>/rows - Get paginated rows for a table."""
        return data_rows(table, request.args.get("offset", 0, type=int))

    @app.get("/data/<table>/row")
    def data_row(table: str) -> str:
        """GET /data/<table>/row - Get the editor for a specific table row."""
        spec = tbl.spec(table)
        pk_value = request.args.get("pk", "")
        row = tbl.get_row(db(), table, pk_value)
        if not row:
            return '<div id="row-editor"></div>'
        return render_template(
            "row_editor.html",
            table=table,
            fields=tbl.fields(table),
            editable=spec["editable"],
            pk=tbl.pk_name(table),
            pk_value=pk_value,
            row=row,
            md=md,
            markdown_fields={"description", "payload", "body", "content"},
        )

    @app.post("/data/<table>/row")
    def save_row(table: str) -> str:
        """POST /data/<table>/row - Update a table row."""
        try:
            tbl.update_row(db(), table, request.args.get("pk", ""), request.form.to_dict())
        except (ValueError, PeeweeException) as exc:
            return data_rows(table) + _toast(f"not saved: {exc}")
        return data_rows(table) + _toast("row saved")

    @app.post("/data/<table>")
    def insert_row(table: str) -> str:
        """POST /data/<table> - Insert a new row into a table."""
        try:
            pk_value = tbl.insert_row(db(), table, request.form.to_dict())
        except (ValueError, PeeweeException) as exc:
            return data_rows(table) + _toast(f"not inserted: {exc}")
        return data_rows(table) + _toast(f"inserted {table} {pk_value}")

    @app.post("/data/<table>/delete")
    def delete_row(table: str) -> str:
        """POST /data/<table>/delete - Delete a row from a table."""
        try:
            deleted = tbl.delete_row(db(), table, request.args.get("pk", ""))
        except PeeweeException as exc:
            return data_rows(table) + _toast(f"not deleted: {exc}")
        return data_rows(table) + _toast("row deleted" if deleted else "nothing to delete")
