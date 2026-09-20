"""Flask + HTMX web UI - two pages, where the human writes.

Flask rather than FastAPI: the job is rendering HTML fragments, not validating
JSON, and Jinja2 comes bundled. HTMX loads from a CDN, so there is no npm, no
build step and no bundler."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from peewee import PeeweeException, SqliteDatabase

from . import tables as tbl
from .db import HUMAN, TASK_STATUSES, connect, init_db, now
from .export import _fmt_ts, export_markdown
from .markdown import PROSE_KINDS
from .markdown import render as md
from .project import (
    AGENT_FIELDS,
    db_path,
    load_config,
    read_prompt,
    registry_load,
    remove_agent_config,
    set_agent_config,
    sync_agents_from_config,
    write_prompt,
)
from .runtime import GLYPHS, one_line
from .store import (
    active_claims,
    add_dependency,
    add_task,
    blocking_map,
    delete_agent,
    delete_task,
    docs_get,
    docs_list,
    docs_set,
    get_task,
    list_agents,
    list_tasks,
    recent_events,
    remove_dependency,
    send_message,
    task_dependencies,
    task_dependents,
    task_events,
    task_messages,
    token_usage_by_agent,
    update_task,
    update_task_status,
)


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = max(0, int(now() - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return _fmt_ts(ts)


# agent names become TOML keys and prompt filenames, so keep them plain
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BACKENDS = ("claude", "codex")


def create_app(project_dir: Path):
    from flask import Flask, render_template, request
    from markupsafe import escape

    app = Flask(__name__)  # templates/ and static/ live beside this module
    state: dict[str, Any] = {}

    def open_project(path: Path) -> None:
        path = Path(path).resolve()
        database = connect(db_path(path))
        init_db(database)
        sync_agents_from_config(database, path)
        previous = state.get("db")
        state.update(project=path, db=database, name=path.name)
        if previous is not None:
            previous.close()

    open_project(project_dir)

    def db() -> SqliteDatabase:
        return state["db"]

    def render_row(task: dict) -> str:
        return render_template(
            "task_row.html",
            t=task,
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            blocking=blocking_map(db()),
            ago=_ago,
        )

    def tasks_table() -> str:
        return render_template("tasks_table.html", tasks=list_tasks(db()), render_row=render_row)

    def dependency_candidates(task: dict) -> list[dict]:
        """Tasks this one could wait for: anything but itself and its own deps."""
        taken = {d["id"] for d in task_dependencies(db(), task["id"])} | {task["id"]}
        return [t for t in list_tasks(db()) if t["id"] not in taken]

    def agent_models() -> dict[str, str]:
        return {
            name: cfg.get("model", "")
            for name, cfg in load_config(state["project"]).get("agents", {}).items()
        }

    def agent_rows() -> str:
        return render_template(
            "agent_rows.html", agents=list_agents(db()), models=agent_models(), ago=_ago
        )

    def activity_tail() -> str:
        return render_template(
            "activity_tail.html",
            events=recent_events(db(), limit=25),
            ago=_ago,
            glyph=lambda kind: GLYPHS.get(kind, " "),
            brief=lambda body: one_line(body or "", 110),
        )

    def claims_panel() -> str:
        return render_template("claims.html", claims=active_claims(db()), ago=_ago)

    def task_activity(task_id: int) -> str:
        return render_template(
            "task_activity.html",
            events=task_events(db(), task_id),
            ago=_ago,
            glyph=lambda kind: GLYPHS.get(kind, " "),
            brief=lambda body: one_line(body or "", 90),
            prose_kinds=PROSE_KINDS,
            md=md,
        )

    def task_detail_panel(task: dict, edit: bool = False) -> str:
        return render_template(
            "task_detail.html",
            t=task,
            edit=edit,
            description_html=md(task["description"]) or "<p class='muted'>No description.</p>",
            dependencies=task_dependencies(db(), task["id"]),
            dependents=task_dependents(db(), task["id"]),
            candidates=dependency_candidates(task),
            messages=task_messages(db(), task["id"]),
            activity=task_activity(task["id"]),
            ago=_ago,
            md=md,
        )

    def _toast(message: str) -> str:
        """An out-of-band swap, so a fragment response can also say something."""
        return f'<div id="toast" hx-swap-oob="true">{escape(message)}</div>'

    def rows_with_toast(message: str) -> str:
        """HTMX swaps the table in place and drops the message in the toast."""
        return agent_rows() + _toast(message)

    def editor(name: str) -> str:
        cfg = load_config(state["project"]).get("agents", {}).get(name, {})

        def value(field: dict) -> str:
            raw = cfg.get(field["key"], "")
            return "" if raw is None else str(raw)

        return render_template(
            "agent_editor.html",
            name=name,
            fields=AGENT_FIELDS,
            value=value,
            content=read_prompt(state["project"], name),
        )

    @app.context_processor
    def layout_context() -> dict:
        registry = registry_load()
        return {
            "project_name": state["name"],
            "projects": sorted(registry) if registry else [state["name"]],
        }

    @app.get("/")
    def index():
        return render_template(
            "project.html",
            page="project",
            description=docs_get(db(), "description") or "",
            description_html=md(docs_get(db(), "description")),
            agents=list_agents(db()),
            tasks_table=tasks_table(),
        )

    @app.post("/description")
    def set_description():
        docs_set(db(), "description", request.form.get("content", ""), HUMAN)
        return "description saved"

    @app.post("/tasks")
    def create_task():
        title = (request.form.get("title") or "").strip()
        if title:
            add_task(
                db(), title,
                request.form.get("description", ""),
                request.form.get("assigned_to") or None,
            )
        return tasks_table()

    @app.post("/tasks/<int:task_id>")
    def patch_task(task_id: int):
        fields = {k: v for k, v in request.form.items() if k in {"title", "description", "assigned_to", "status"}}
        update_task(db(), task_id, **fields)
        task = get_task(db(), task_id)
        if not task:
            return ""
        # an edit from the detail panel comes back as the panel, not the row
        if "description" in fields:
            return task_detail_panel(task) + _toast(f"task {task_id} saved")
        return render_row(task)

    @app.post("/tasks/<int:task_id>/requeue")
    def requeue_task(task_id: int):
        update_task_status(db(), task_id, "todo")
        task = get_task(db(), task_id)
        return render_row(task) if task else ""

    @app.post("/tasks/<int:task_id>/approve")
    def approve_task(task_id: int):
        update_task_status(db(), task_id, "done")
        task = get_task(db(), task_id)
        return (render_row(task) + _toast(f"task {task_id} approved")) if task else ""

    @app.post("/tasks/<int:task_id>/send-back")
    def send_back_task(task_id: int):
        update_task_status(db(), task_id, "todo")
        task = get_task(db(), task_id)
        return (render_row(task) + _toast(f"task {task_id} sent back")) if task else ""

    @app.post("/tasks/<int:task_id>/deps")
    def add_task_dependency(task_id: int):
        task = get_task(db(), task_id)
        if not task:
            return ""
        try:
            add_dependency(db(), task_id, int(request.form.get("depends_on", 0)))
        except (ValueError, PeeweeException) as exc:
            return task_detail_panel(task) + _toast(str(exc))
        return task_detail_panel(get_task(db(), task_id))

    @app.post("/tasks/<int:task_id>/deps/<int:dep_id>/delete")
    def drop_task_dependency(task_id: int, dep_id: int):
        remove_dependency(db(), task_id, dep_id)
        task = get_task(db(), task_id)
        return task_detail_panel(task) if task else ""

    @app.post("/tasks/<int:task_id>/delete")
    def remove_task(task_id: int):
        delete_task(db(), task_id)
        return tasks_table()

    @app.get("/tasks/<int:task_id>/row")
    def task_row(task_id: int):
        task = get_task(db(), task_id)
        return render_row(task) if task else ""

    @app.get("/tasks/<int:task_id>/detail")
    def task_detail(task_id: int):
        task = get_task(db(), task_id)
        if not task:
            return ""
        return task_detail_panel(task, edit=request.args.get("edit") == "1")

    @app.post("/tasks/<int:task_id>/message")
    def task_message(task_id: int):
        task = get_task(db(), task_id)
        if not task:
            return ""
        payload = (request.form.get("payload") or "").strip()
        if payload:
            send_message(
                db(), HUMAN, task["assigned_to"] or HUMAN, task_id, "note", payload
            )
        return task_detail_panel(task)

    @app.get("/agents")
    def agents_page():
        return render_template(
            "agents.html",
            page="agents",
            agent_rows=agent_rows(),
            activity=activity_tail(),
            claims=claims_panel(),
            backends=BACKENDS,
            usage=token_usage_by_agent(db()),
        )

    @app.get("/agents/rows")
    def agents_rows():
        return agent_rows()

    @app.get("/agents/activity")
    def agents_activity():
        return activity_tail()

    @app.get("/agents/claims")
    def agents_claims():
        return claims_panel()

    @app.post("/agents")
    def create_agent():
        name = (request.form.get("name") or "").strip()
        if not AGENT_NAME.fullmatch(name):
            return rows_with_toast(f"'{name}' is not a usable agent name")
        if name in {a["name"] for a in list_agents(db())}:
            return rows_with_toast(f"{name} already exists")
        set_agent_config(state["project"], name, request.form.to_dict())
        sync_agents_from_config(db(), state["project"])
        return rows_with_toast(f"added {name}")

    @app.post("/agents/<name>")
    def save_agent(name: str):
        try:
            set_agent_config(state["project"], name, request.form.to_dict())
        except ValueError:
            return rows_with_toast(f"{name}: prices must be numbers")
        sync_agents_from_config(db(), state["project"])
        return rows_with_toast(f"{name} settings saved - restart its daemon to pick them up")

    @app.post("/agents/<name>/delete")
    def remove_agent(name: str):
        remove_agent_config(state["project"], name)
        freed = delete_agent(db(), name)
        note = f" ({freed} task{'s' if freed != 1 else ''} unassigned)" if freed else ""
        return rows_with_toast(f"removed {name}{note}") + '<div id="agent-editor" hx-swap-oob="true"></div>'

    @app.get("/agents/close")
    def close_editor():
        return '<div id="agent-editor"></div>'

    @app.get("/agents/<name>/context")
    def get_context(name: str):
        return editor(name)

    @app.post("/agents/<name>/context")
    def set_context(name: str):
        write_prompt(state["project"], name, request.form.get("content", ""))
        return f"{name} prompt saved"

    @app.post("/export")
    def do_export():
        out = state["project"] / ".agents-export"
        written = export_markdown(db(), out)
        return f"exported {len(written)} files to {out}"

    # --- docs: the shared project knowledge agents read and write ---------

    def docs_table() -> str:
        return render_template("docs_table.html", docs=docs_list(db()), ago=_ago)

    def doc_editor(key: str) -> str:
        doc = next((d for d in docs_list(db()) if d["key"] == key), None)
        content = (doc or {}).get("content") or ""
        return render_template(
            "doc_editor.html",
            key=key,
            content=content,
            content_html=md(content),
            updated_by=(doc or {}).get("updated_by"),
        )

    @app.get("/docs")
    def docs_page():
        return render_template("docs.html", page="docs", docs_table=docs_table())

    @app.post("/docs")
    def create_doc():
        key = (request.form.get("key") or "").strip()
        if not key:
            return doc_editor("")
        if docs_get(db(), key) is None:
            docs_set(db(), key, "", HUMAN)
        return doc_editor(key)

    @app.get("/docs/close")
    def close_doc():
        return '<div id="doc-editor"></div>'

    @app.get("/docs/<key>")
    def read_doc(key: str):
        return doc_editor(key)

    @app.post("/docs/<key>")
    def save_doc(key: str):
        docs_set(db(), key, request.form.get("content", ""), HUMAN)
        return docs_table()

    @app.post("/docs/<key>/delete")
    def delete_doc(key: str):
        tbl.delete_row(db(), "docs", key)
        return docs_table()

    # --- every table, row by row ------------------------------------------

    def data_rows(table: str, offset: int = 0, limit: int = 50) -> str:
        return render_template(
            "data_rows.html",
            table=table,
            fields=tbl.fields(table),
            pk=tbl.pk_name(table),
            rows=tbl.list_rows(db(), table, limit, offset),
            total=tbl.count_rows(db(), table),
            offset=offset,
            limit=limit,
            ago=_ago,
            brief=lambda value: one_line("" if value is None else str(value), 60),
        )

    @app.get("/data")
    @app.get("/data/<table>")
    def data_page(table: str = "tasks"):
        if table not in tbl.TABLES:
            return render_template("data.html", page="data", tables=tbl.TABLES, table=None,
                                   note=f"no such table: {table}", insertable=[], types={}, rows="")
        spec = tbl.spec(table)
        return render_template(
            "data.html",
            page="data",
            tables=tbl.TABLES,
            table=table,
            note=spec.get("note"),
            insertable=spec["insertable"],
            types=tbl.field_types(table),
            rows=data_rows(table, request.args.get("offset", 0, type=int)),
        )

    @app.get("/data/close")
    def close_row():
        return '<div id="row-editor"></div>'

    @app.get("/data/<table>/rows")
    def data_rows_fragment(table: str):
        return data_rows(table, request.args.get("offset", 0, type=int))

    @app.get("/data/<table>/row")
    def data_row(table: str):
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
        )

    @app.post("/data/<table>/row")
    def save_row(table: str):
        try:
            tbl.update_row(db(), table, request.args.get("pk", ""), request.form.to_dict())
        except (ValueError, PeeweeException) as exc:
            return data_rows(table) + _toast(f"not saved: {exc}")
        return data_rows(table) + _toast("row saved")

    @app.post("/data/<table>")
    def insert_row(table: str):
        try:
            pk_value = tbl.insert_row(db(), table, request.form.to_dict())
        except (ValueError, PeeweeException) as exc:
            return data_rows(table) + _toast(f"not inserted: {exc}")
        return data_rows(table) + _toast(f"inserted {table} {pk_value}")

    @app.post("/data/<table>/delete")
    def delete_row(table: str):
        try:
            deleted = tbl.delete_row(db(), table, request.args.get("pk", ""))
        except PeeweeException as exc:
            return data_rows(table) + _toast(f"not deleted: {exc}")
        return data_rows(table) + _toast("row deleted" if deleted else "nothing to delete")

    @app.post("/switch")
    def switch_project():
        target = registry_load().get(request.form.get("project", ""))
        if target:
            open_project(Path(target))
        return index()

    return app
