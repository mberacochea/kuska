"""Routes: the read-only table browser."""

from __future__ import annotations

from flask import redirect, render_template, request

from .. import tables as tbl
from .helpers import wants_fragment


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    data_rows = ctx.data_rows
    data_row = ctx.data_row
    db = ctx.db

    # ========== ROUTES: Data (read-only table browser) ==========

    @app.get("/data")
    @app.get("/data/<table>")
    def data_page(table: str = "tasks") -> str | tuple[str, int]:
        """GET /data[/<table>] - list a table's rows, ?page=N (1-based).

        htmx (the pager) swaps #rows and gets just that; a browser navigation
        gets the whole page. ?open=<pk> is the old inline-editor link and now
        redirects to the row's own page.
        """
        if table not in tbl.TABLES:
            return render_template("data.html", page="data", tables=tbl.TABLES, table=None,
                                   note=f"no such table: {table}", rows=""), 404
        open_pk = request.args.get("open")
        if open_pk:
            return redirect(f"/data/{table}/{open_pk}", 301)
        rows_html = data_rows(table, max(request.args.get("page", 1, type=int), 1))
        if wants_fragment():
            return rows_html
        return render_template("data.html", page="data", tables=tbl.TABLES, table=table,
                               note=tbl.spec(table).get("note"), rows=rows_html)

    @app.get("/data/<table>/<pk>")
    def data_row_page(table: str, pk: str) -> str | tuple[str, int]:
        """GET /data/<table>/<pk> - one row, every column, read-only."""
        if table not in tbl.TABLES:
            return "", 404
        row = tbl.get_row(db(), table, pk)
        if row is None:
            return "", 404
        body = data_row(table, row)
        if wants_fragment():
            return body
        return render_template("data_row_page.html", page="data", table=table,
                               label=tbl.spec(table)["label"], pk=pk, row_html=body)
