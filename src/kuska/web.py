"""Flask + HTMX web UI - two pages, where the human writes.

The job is rendering HTML fragments, not validating
JSON, and Jinja2 comes bundled. HTMX loads from a CDN, so there is no npm, no
build step and no bundler."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from flask import Flask, make_response, render_template, request
from markupsafe import escape
from peewee import IntegrityError, PeeweeException, SqliteDatabase

from . import tables as tbl
from .db import HUMAN, TASK_STATUSES, connect, init_db, now
from .export import _fmt_ts, export_markdown
from .markdown import PROSE_KINDS
from .markdown import render as md
from .models import MODELS, Message
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
    filter_tasks,
    full_text_search,
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
BACKENDS = ("claude", "codex", "openai")
DOC_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# Validation limits
MAX_TASK_TITLE_LEN = 200
MAX_TASK_DESC_LEN = 10000
MAX_DOC_CONTENT_LEN = 100000
MAX_AGENT_ROLE_LEN = 500


def _error_response(field: str, message: str) -> dict[str, Any]:
    """Return a structured error response for form validation."""
    return {"error": True, "field": field, "message": message}


def _field_error_html(field: str, message: str) -> str:
    """Return HTML for a form field error."""
    return f'<div class="field-error" role="alert" aria-live="polite" data-field="{escape(field)}">{escape(message)}</div>'


def _bad_request(fragment: str, field: str, message: str):
    """422 response: the fragment re-rendered as-is, plus a field error for htmx to display."""
    return make_response(fragment + _field_error_html(field, message), 422)


def validate_task_title(title: str, db_handle: SqliteDatabase, task_id: int | None = None) -> str | None:
    """Validate task title. Returns error message if invalid, None if valid."""
    title = (title or "").strip()
    if not title:
        return "Title is required"
    if len(title) > MAX_TASK_TITLE_LEN:
        return f"Title is too long (max {MAX_TASK_TITLE_LEN} characters)"
    # Check for duplicates
    existing = [t for t in list_tasks(db_handle) if t["title"] == title]
    if existing and (not task_id or existing[0]["id"] != task_id):
        return "Title already exists. Try adding a suffix like '_v2'"
    return None


def validate_task_description(description: str) -> str | None:
    """Validate task description. Returns error message if invalid, None if valid."""
    if description and len(description) > MAX_TASK_DESC_LEN:
        return f"Description is too long (max {MAX_TASK_DESC_LEN} characters)"
    return None


def validate_task_assigned_to(assigned_to: str | None, db_handle: SqliteDatabase) -> str | None:
    """Validate assigned_to field. Returns error message if invalid, None if valid."""
    if not assigned_to:
        return None
    agent_names = {a["name"] for a in list_agents(db_handle)}
    if assigned_to not in agent_names:
        return f"Agent '{assigned_to}' does not exist"
    return None


def validate_agent_name(name: str, existing_agents: set[str]) -> str | None:
    """Validate agent name format. Returns error message if invalid, None if valid."""
    name = (name or "").strip()
    if not name:
        return "Agent name is required"
    if not AGENT_NAME.fullmatch(name):
        return "Agent name must start with alphanumeric and contain only alphanumeric, dash, underscore, or dot"
    if name in existing_agents:
        return f"Agent '{name}' already exists"
    return None


def validate_agent_backend(backend: str) -> str | None:
    """Validate agent backend. Returns error message if invalid, None if valid."""
    backend = (backend or "").strip()
    if not backend:
        return "Backend is required"
    if backend not in BACKENDS:
        return f"Backend must be one of: {', '.join(BACKENDS)}"
    return None


def validate_agent_model(model: str) -> str | None:
    """Validate agent model. Returns error message if invalid, None if valid."""
    model = (model or "").strip()
    if not model:
        return "Model is required"
    return None


def validate_agent_role(role: str) -> str | None:
    """Validate agent role. Returns error message if invalid, None if valid."""
    role = (role or "").strip()
    if not role:
        return "Role is required"
    if len(role) > MAX_AGENT_ROLE_LEN:
        return f"Role is too long (max {MAX_AGENT_ROLE_LEN} characters)"
    return None


def validate_agent_prices(form_data: dict) -> str | None:
    """Validate agent price values. Returns error message if invalid, None if valid."""
    for key in ("price_in_per_mtok", "price_out_per_mtok"):
        if form_data.get(key):
            try:
                value = float(form_data[key])
                if value < 0:
                    return f"{key} must be non-negative"
            except (ValueError, TypeError):
                return f"{key} must be a valid number"
    return None


def validate_doc_key(key: str, existing_docs: set[str]) -> str | None:
    """Validate doc key. Returns error message if invalid, None if valid."""
    key = (key or "").strip()
    if not key:
        return "Doc key is required"
    if not DOC_KEY.fullmatch(key):
        return "Doc key must start with alphanumeric and contain only alphanumeric, dash, or underscore"
    if key in existing_docs:
        return f"Doc '{key}' already exists"
    return None


def validate_doc_content(content: str) -> str | None:
    """Validate doc content. Returns error message if invalid, None if valid."""
    if len(content) > MAX_DOC_CONTENT_LEN:
        return f"Content is too long (max {MAX_DOC_CONTENT_LEN} characters)"
    return None


def create_app(project_dir: Path):
    app = Flask(__name__)  # templates/ and static/ live beside this module
    state: dict[str, Any] = {}

    # ========== State Management ==========

    def open_project(path: Path) -> None:
        """Open a project, closing any previously open project."""
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
        """Get the current database handle."""
        return state["db"]

    # ========== Helper: Toast Messages ==========

    def _toast(message: str) -> str:
        """Return an out-of-band swap div for toast messages to display."""
        return f'<div id="toast" hx-swap-oob="true">{escape(message)}</div>'

    def rows_with_toast(message: str) -> str:
        """Swap agent rows table and display a toast message."""
        return agent_rows() + _toast(message)

    # ========== Helper: Task Rendering ==========

    def render_row(task: dict, expanded: bool = False) -> str:
        """Render a single task row - collapsed by default, or its full detail
        panel when expanded (used to land a search result open in place)."""
        if expanded:
            return task_detail_panel(task)
        return render_template(
            "task_row.html",
            t=task,
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            blocking=blocking_map(db()),
            ago=_ago,
        )

    def tasks_table(
        tasks: list[dict] | None = None,
        sort_by: str | None = None,
        sort_dir: str = "asc",
        open_task_id: int | None = None,
    ) -> str:
        """Render the full task table. Defaults to all tasks, unfiltered, unsorted.

        open_task_id, if given, renders that one row already expanded - a link
        from elsewhere (e.g. a search result) can land directly on it.
        """
        return render_template(
            "tasks_table.html",
            tasks=tasks if tasks is not None else list_tasks(db()),
            render_row=lambda t: render_row(t, expanded=(t["id"] == open_task_id)),
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    def tasks_container(
        tasks: list[dict] | None = None,
        search: str = "",
        status_list: list[str] | None = None,
        agent_list: list[str] | None = None,
        sort_by: str | None = None,
        sort_dir: str = "asc",
        open_task_id: int | None = None,
    ) -> str:
        """Render the task table together with its filter/sort controls.

        The controls are plain htmx-driven form fields that GET /tasks
        themselves (see tasks_container.html) - the server is the only place
        that knows the current filter/sort state, and re-renders it into the
        form on every response so there is no client-side state to keep in
        sync.
        """
        return render_template(
            "tasks_container.html",
            tasks_table=tasks_table(tasks, sort_by, sort_dir, open_task_id),
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            search=search,
            status_list=status_list or [],
            agent_list=agent_list or [],
            sort_by=sort_by or "",
            sort_dir=sort_dir,
        )

    def dependency_candidates(task: dict) -> list[dict]:
        """Return tasks that this task could depend on (excluding itself and existing deps)."""
        taken = {d["id"] for d in task_dependencies(db(), task["id"])} | {task["id"]}
        return [t for t in list_tasks(db()) if t["id"] not in taken]

    def task_activity(task_id: int) -> str:
        """Render the activity feed for a task."""
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
        """Render the detail panel for a task (display or edit mode)."""
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

    # ========== Helper: Agent Rendering ==========

    def agent_models() -> dict[str, str]:
        """Build a map of agent names to their configured models."""
        return {
            name: cfg.get("model", "")
            for name, cfg in load_config(state["project"]).get("agents", {}).items()
        }

    def agent_rows() -> str:
        """Render the agent list rows."""
        return render_template(
            "agent_rows.html", agents=list_agents(db()), models=agent_models(), ago=_ago
        )

    def activity_tail() -> str:
        """Render the live activity feed showing recent events."""
        return render_template(
            "activity_tail.html",
            events=recent_events(db(), limit=25),
            ago=_ago,
            glyph=lambda kind: GLYPHS.get(kind, " "),
            brief=lambda body: one_line(body or "", 110),
        )

    def claims_panel() -> str:
        """Render the file claims panel showing what agents are working on."""
        return render_template("claims.html", claims=active_claims(db()), ago=_ago)

    def editor(name: str) -> str:
        """Render the agent configuration and prompt editor."""
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

    # ========== Helper: Docs Rendering ==========

    def docs_table() -> str:
        """Render the docs list table."""
        return render_template("docs_table.html", docs=docs_list(db()), ago=_ago)

    def doc_editor(key: str) -> str:
        """Render the doc editor for a specific doc key."""
        doc = next((d for d in docs_list(db()) if d["key"] == key), None)
        content = (doc or {}).get("content") or ""
        return render_template(
            "doc_editor.html",
            key=key,
            content=content,
            content_html=md(content),
            updated_by=(doc or {}).get("updated_by"),
        )

    # ========== Helper: Data Table Rendering ==========

    def data_rows(table: str, offset: int = 0, limit: int = 50) -> str:
        """Render paginated rows for a generic data table."""
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
            md=md,
            markdown_fields={"description", "payload", "body", "content"},
        )

    # ========== Context Processor ==========

    @app.context_processor
    def layout_context() -> dict:
        """Provide project context for all templates."""
        registry = registry_load()
        return {
            "project_name": state["name"],
            "projects": sorted(registry) if registry else [state["name"]],
        }

    # ========== ROUTES: Project/Main ==========

    @app.get("/")
    def index() -> str:
        """GET / - Display the project overview with task list."""
        return render_template(
            "project.html",
            page="project",
            description=docs_get(db(), "description") or "",
            description_html=md(docs_get(db(), "description")),
            agents=list_agents(db()),
            tasks_container=tasks_container(),
        )

    @app.get("/tasks")
    def tasks_page() -> str:
        """GET /tasks - the tasks page, and also the target of every filter,
        sort, and reload control on it.

        Those controls hx-get this same URL. htmx marks its own requests with
        the HX-Request header, so a plain browser navigation (first load,
        reload, back/forward) gets the full page, while an htmx-issued request
        gets just the tasks-container fragment to swap in. Since hx-push-url
        points the address bar at this same URL with the same query params,
        those two cases always render identically - there is no separate
        fragment-only endpoint that a reload could land on and get bare HTML.
        """
        search = request.args.get("search", "").strip()
        status_list = request.args.getlist("status")
        agent_list = request.args.getlist("agent")
        sort_by = request.args.get("sort") or None
        sort_dir = request.args.get("direction", "asc")
        open_task_id = request.args.get("open", type=int)
        filtered = filter_tasks(db(), search, status_list or None, agent_list or None, sort_by, sort_dir)
        container = tasks_container(filtered, search, status_list, agent_list, sort_by, sort_dir, open_task_id)

        if request.headers.get("HX-Request") == "true":
            return container

        return render_template(
            "tasks.html",
            page="tasks",
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            tasks_container=container,
        )

    @app.post("/description")
    def set_description() -> str:
        """POST /description - Update the project description."""
        docs_set(db(), "description", request.form.get("content", ""), HUMAN)
        return "description saved"

    @app.post("/switch")
    def switch_project() -> str:
        """POST /switch - Switch to a different project."""
        target = registry_load().get(request.form.get("project", ""))
        if target:
            open_project(Path(target))
        return index()

    @app.post("/export")
    def do_export() -> str:
        """POST /export - Export the project to markdown files."""
        out = state["project"] / ".agents-export"
        written = export_markdown(db(), out)
        return f"exported {len(written)} files to {out}"

    # ========== ROUTES: Tasks ==========

    @app.post("/tasks")
    def create_task() -> tuple[str, int]:
        """POST /tasks - Create a new task."""
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        assigned_to = request.form.get("assigned_to") or None

        # Validate title
        title_error = validate_task_title(title, db())
        if title_error:
            return _bad_request(tasks_container(), "title", title_error)

        # Validate description
        desc_error = validate_task_description(description)
        if desc_error:
            return _bad_request(tasks_container(), "description", desc_error)

        # Validate assigned_to
        agent_error = validate_task_assigned_to(assigned_to, db())
        if agent_error:
            return _bad_request(tasks_container(), "assigned_to", agent_error)

        try:
            add_task(db(), title, description, assigned_to)
        except (ValueError, IntegrityError) as exc:
            return _bad_request(tasks_container(), "form", f"Failed to create task: {exc}")

        return tasks_container(), 200

    @app.post("/tasks/<int:task_id>")
    def patch_task(task_id: int) -> tuple[str, int]:
        """POST /tasks/<id> - Update task fields (title, description, assigned_to, status)."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        fields = {k: v for k, v in request.form.items() if k in {"title", "description", "assigned_to", "status"}}

        # Validate title if provided
        if "title" in fields:
            title_error = validate_task_title(fields["title"], db(), task_id)
            if title_error:
                return _bad_request(task_detail_panel(task, edit=True), "title", title_error)

        # Validate description if provided
        if "description" in fields:
            desc_error = validate_task_description(fields["description"])
            if desc_error:
                return _bad_request(task_detail_panel(task, edit=True), "description", desc_error)

        # Validate assigned_to if provided
        if "assigned_to" in fields:
            assigned_to = fields["assigned_to"] or None
            agent_error = validate_task_assigned_to(assigned_to, db())
            if agent_error:
                return _bad_request(task_detail_panel(task, edit=True), "assigned_to", agent_error)

        try:
            update_task(db(), task_id, **fields)
        except (ValueError, IntegrityError) as exc:
            return _bad_request(task_detail_panel(task, edit=True), "form", f"Failed to update task: {exc}")

        task = get_task(db(), task_id)
        if not task:
            return "", 404

        # an edit from the detail panel comes back as the panel, not the row
        if "description" in fields:
            return task_detail_panel(task) + _toast(f"task {task_id} saved"), 200
        return render_row(task), 200

    @app.post("/tasks/<int:task_id>/requeue")
    def requeue_task(task_id: int) -> str:
        """POST /tasks/<id>/requeue - Re-queue a task (set status to todo)."""
        update_task_status(db(), task_id, "todo")
        task = get_task(db(), task_id)
        return render_row(task) if task else ""

    @app.post("/tasks/<int:task_id>/approve")
    def approve_task(task_id: int) -> str:
        """POST /tasks/<id>/approve - Approve a task (set status to done)."""
        update_task_status(db(), task_id, "done")
        task = get_task(db(), task_id)
        return (render_row(task) + _toast(f"task {task_id} approved")) if task else ""

    @app.post("/tasks/<int:task_id>/send-back")
    def send_back_task(task_id: int) -> str:
        """POST /tasks/<id>/send-back - Send a task back (set status to todo)."""
        update_task_status(db(), task_id, "todo")
        task = get_task(db(), task_id)
        return (render_row(task) + _toast(f"task {task_id} sent back")) if task else ""

    @app.post("/tasks/<int:task_id>/deps")
    def add_task_dependency(task_id: int) -> tuple[str, int]:
        """POST /tasks/<id>/deps - Add a task dependency."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        try:
            dep_id = int(request.form.get("depends_on", 0))
        except (ValueError, TypeError):
            return _bad_request(task_detail_panel(task), "depends_on", "Invalid dependency ID")

        # Validate self-dependency
        if dep_id == task_id:
            return _bad_request(task_detail_panel(task), "depends_on", "Task cannot depend on itself")

        # Validate task exists
        dep_task = get_task(db(), dep_id)
        if not dep_task:
            return _bad_request(task_detail_panel(task), "depends_on", f"Task {dep_id} not found")

        # Check for circular dependency
        existing_deps = {d["id"] for d in task_dependencies(db(), task_id)}
        if dep_id in existing_deps:
            return _bad_request(task_detail_panel(task), "depends_on", "Dependency already exists")

        # Check if adding this dependency would create a cycle
        # (if dep_task already depends on task_id, adding task_id->dep_id would create a cycle)
        dep_task_deps = {d["id"] for d in task_dependencies(db(), dep_id)}
        if task_id in dep_task_deps:
            return _bad_request(task_detail_panel(task), "depends_on",
                f"Would create a circular dependency: task {dep_id} already depends on this task")

        try:
            add_dependency(db(), task_id, dep_id)
        except (ValueError, PeeweeException) as exc:
            return _bad_request(task_detail_panel(task), "depends_on", str(exc))

        return task_detail_panel(get_task(db(), task_id)), 200

    @app.post("/tasks/<int:task_id>/deps/<int:dep_id>/delete")
    def drop_task_dependency(task_id: int, dep_id: int) -> str:
        """POST /tasks/<id>/deps/<dep_id>/delete - Remove a task dependency."""
        remove_dependency(db(), task_id, dep_id)
        task = get_task(db(), task_id)
        return task_detail_panel(task) if task else ""

    @app.post("/tasks/<int:task_id>/delete")
    def remove_task(task_id: int) -> str:
        """POST /tasks/<id>/delete - Delete a task."""
        delete_task(db(), task_id)
        return tasks_container()

    @app.post("/tasks/<int:task_id>/message")
    def task_message(task_id: int) -> str:
        """POST /tasks/<id>/message - Send a message on a task thread."""
        task = get_task(db(), task_id)
        if not task:
            return ""
        payload = (request.form.get("payload") or "").strip()
        if payload:
            send_message(
                db(), HUMAN, task["assigned_to"] or HUMAN, task_id, "note", payload
            )
        return task_detail_panel(task)

    @app.get("/tasks/<int:task_id>/row")
    def task_row(task_id: int) -> str:
        """GET /tasks/<id>/row - Get a single task row for the table."""
        task = get_task(db(), task_id)
        return render_row(task) if task else ""

    @app.get("/tasks/<int:task_id>/detail")
    def task_detail(task_id: int) -> str:
        """GET /tasks/<id>/detail - Get the task detail panel (optionally in edit mode)."""
        task = get_task(db(), task_id)
        if not task:
            return ""
        return task_detail_panel(task, edit=request.args.get("edit") == "1")

    # ========== ROUTES: Agents ==========

    @app.get("/agents")
    def agents_page() -> str:
        """GET /agents - Display the agents page with status, activity, and file claims."""
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
    def agents_rows() -> str:
        """GET /agents/rows - Get the agent list rows fragment."""
        return agent_rows()

    @app.get("/agents/activity")
    def agents_activity() -> str:
        """GET /agents/activity - Get the activity tail fragment."""
        return activity_tail()

    @app.get("/agents/claims")
    def agents_claims() -> str:
        """GET /agents/claims - Get the file claims panel fragment."""
        return claims_panel()

    @app.post("/agents")
    def create_agent() -> tuple[str, int]:
        """POST /agents - Create a new agent with the given configuration."""
        name = request.form.get("name", "").strip()
        existing = {a["name"] for a in list_agents(db())}

        # Validate agent name
        name_error = validate_agent_name(name, existing)
        if name_error:
            return _bad_request(agent_rows(), "name", name_error)

        # Validate required fields
        backend = request.form.get("backend", "").strip()
        model = request.form.get("model", "").strip()
        role = request.form.get("role", "").strip()

        backend_error = validate_agent_backend(backend)
        if backend_error:
            return _bad_request(agent_rows(), "backend", backend_error)

        model_error = validate_agent_model(model)
        if model_error:
            return _bad_request(agent_rows(), "model", model_error)

        role_error = validate_agent_role(role)
        if role_error:
            return _bad_request(agent_rows(), "role", role_error)

        # Validate prices if provided
        price_error = validate_agent_prices(request.form.to_dict())
        if price_error:
            return _bad_request(agent_rows(), "prices", price_error)

        try:
            set_agent_config(state["project"], name, request.form.to_dict())
            sync_agents_from_config(db(), state["project"])
        except (ValueError, OSError) as exc:
            return _bad_request(agent_rows(), "form", f"Failed to create agent: {exc}")

        return rows_with_toast(f"added {name}"), 200

    @app.post("/agents/<name>")
    def save_agent(name: str) -> tuple[str, int]:
        """POST /agents/<name> - Save agent configuration (backend, model, role, etc)."""
        # Validate required fields if provided
        backend = request.form.get("backend", "").strip()
        model = request.form.get("model", "").strip()
        role = request.form.get("role", "").strip()

        if backend:
            backend_error = validate_agent_backend(backend)
            if backend_error:
                return _bad_request(agent_rows(), "backend", backend_error)

        if model:
            model_error = validate_agent_model(model)
            if model_error:
                return _bad_request(agent_rows(), "model", model_error)

        if role:
            role_error = validate_agent_role(role)
            if role_error:
                return _bad_request(agent_rows(), "role", role_error)

        # Validate prices if provided
        price_error = validate_agent_prices(request.form.to_dict())
        if price_error:
            return _bad_request(agent_rows(), "prices", price_error)

        try:
            set_agent_config(state["project"], name, request.form.to_dict())
        except (ValueError, OSError) as exc:
            return _bad_request(agent_rows(), "form", f"Failed to save agent settings: {exc}")

        sync_agents_from_config(db(), state["project"])
        return rows_with_toast(f"{name} settings saved - restart its daemon to pick them up"), 200

    @app.post("/agents/<name>/delete")
    def remove_agent(name: str) -> str:
        """POST /agents/<name>/delete - Delete an agent and unassign its tasks."""
        remove_agent_config(state["project"], name)
        freed = delete_agent(db(), name)
        note = f" ({freed} task{'s' if freed != 1 else ''} unassigned)" if freed else ""
        return rows_with_toast(f"removed {name}{note}") + '<div id="agent-editor" hx-swap-oob="true"></div>'

    @app.get("/agents/<name>/context")
    def get_context(name: str) -> str:
        """GET /agents/<name>/context - Get the agent prompt editor."""
        return editor(name)

    @app.post("/agents/<name>/context")
    def set_context(name: str) -> str:
        """POST /agents/<name>/context - Save an agent's prompt content."""
        write_prompt(state["project"], name, request.form.get("content", ""))
        return f"{name} prompt saved"

    # ========== ROUTES: Docs ==========

    @app.get("/docs")
    def docs_page() -> str:
        """GET /docs - Display the shared docs page.

        ?open=<key> pre-opens that doc's editor, so a link from elsewhere
        (e.g. a search result) can land directly on it.
        """
        open_key = request.args.get("open", "")
        editor = doc_editor(open_key) if open_key and docs_get(db(), open_key) is not None else '<div id="doc-editor"></div>'
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

    # ========== ROUTES: Data (Generic Table Editor) ==========

    @app.get("/data")
    @app.get("/data/<table>")
    def data_page(table: str = "tasks") -> str:
        """GET /data[/<table>] - Display the generic data table browser/editor."""
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

    # ========== ROUTES: Search ==========

    @app.get("/search")
    def search_page() -> str:
        """GET /search - Display search page with results."""
        query = request.args.get("q", "").strip()
        page = request.args.get("page", 1, type=int)
        tables_filter = request.args.getlist("tables[]")

        # Validate page number
        if page < 1:
            page = 1

        # All available tables for filtering
        all_tables = ["docs", "messages", "tasks", "events"]
        tables_selected = [t for t in tables_filter if t in all_tables] or all_tables

        # Initialize results
        results = []
        error_msg = None

        # If query provided, validate and search
        if query:
            query_len = len(query)
            if query_len < 2:
                error_msg = "Search query must be at least 2 characters"
            elif query_len > 1000:
                error_msg = "Search query must be no more than 1000 characters"
            else:
                try:
                    limit = 20
                    offset = (page - 1) * limit
                    results = full_text_search(
                        db(),
                        query,
                        tables=tables_selected if tables_selected != all_tables else None,
                        limit=limit,
                        offset=offset
                    )
                except ValueError as e:
                    error_msg = str(e)
                except Exception as e:
                    error_msg = f"Search failed: {e}"

        return render_template(
            "search.html",
            page="search",
            query=query,
            results=results,
            tables_selected=tables_selected,
            all_tables=all_tables,
            current_page=page,
            limit=20,
            error_msg=error_msg,
        )

    # ========== ROUTES: Stats Dashboard ==========

    def compute_stats() -> dict[str, Any]:
        """Compute all metrics for the stats dashboard."""
        tasks = list_tasks(db())
        agents = list_agents(db())
        usage = token_usage_by_agent(db())

        # Fetch all messages directly from db
        with db().bind_ctx(MODELS):
            all_messages = list(Message.select())
            messages_list = [{"ts": m.ts, "sender": m.sender, "task_id": m.task_id,
                             "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
                             "cost_usd": m.cost_usd} for m in all_messages]

        # Project overview stats
        total_tasks = len(tasks)
        completed = len([t for t in tasks if t["status"] == "done"])
        in_progress = len([t for t in tasks if t["status"] == "in_progress"])
        blocked = len([t for t in tasks if t["status"] == "blocked"])
        needs_approval = len([t for t in tasks if t["status"] == "needs_approval"])
        todo = len([t for t in tasks if t["status"] == "todo"])

        completed_pct = int((completed / total_tasks * 100) if total_tasks > 0 else 0)

        # Task timeline for burndown chart - group by day
        task_timeline = defaultdict(int)
        current_time = now()
        for task in tasks:
            if task["status"] == "done" and task["updated_at"]:
                day_ts = int(task["updated_at"] / 86400) * 86400
                task_timeline[day_ts] += 1

        # Sort by timestamp
        timeline_sorted = sorted(task_timeline.items())
        burndown_data = [{"day": int(ts), "completed": count} for ts, count in timeline_sorted[-30:]]

        # Agent productivity stats
        agent_stats = []
        for agent in agents:
            agent_usage = next((u for u in usage if u["agent"] == agent["name"]), None)
            agent_tasks = [t for t in tasks if t["assigned_to"] == agent["name"]]
            completed_by_agent = len([t for t in agent_tasks if t["status"] == "done"])

            stats = {
                "name": agent["name"],
                "backend": agent["backend"],
                "status": agent["status"],
                "current_task_id": agent["current_task_id"],
                "completed_tasks": completed_by_agent,
                "total_tasks": len(agent_tasks),
                "turns": agent_usage["turns"] if agent_usage else 0,
                "input_tokens": agent_usage["input_tokens"] if agent_usage else 0,
                "output_tokens": agent_usage["output_tokens"] if agent_usage else 0,
                "cache_read_tokens": agent_usage["cache_read_tokens"] if agent_usage else 0,
                "cache_write_tokens": agent_usage["cache_write_tokens"] if agent_usage else 0,
                "tool_rounds": agent_usage["tool_rounds"] if agent_usage else 0,
                "cost_usd": agent_usage["cost_usd"] if agent_usage else 0.0,
                "avg_cost": (agent_usage["cost_usd"] / agent_usage["turns"]) if (agent_usage and agent_usage["turns"] > 0) else 0.0,
            }
            agent_stats.append(stats)

        # Sort by cost descending
        agent_stats.sort(key=lambda x: x["cost_usd"], reverse=True)

        # Cost and token analysis. Fresh input and cache reads are kept apart
        # because they are priced about 10x differently - summing them produces
        # a number that looks alarming and means nothing.
        total_cost = sum(u["cost_usd"] for u in usage)
        total_input_tokens = sum(u["input_tokens"] for u in usage)
        total_output_tokens = sum(u["output_tokens"] for u in usage)
        total_cache_read_tokens = sum(u["cache_read_tokens"] for u in usage)
        total_cache_write_tokens = sum(u["cache_write_tokens"] for u in usage)
        total_turns = sum(u["turns"] for u in usage)
        total_tool_rounds = sum(u["tool_rounds"] for u in usage)
        cached_in = total_cache_read_tokens + total_input_tokens
        cache_hit_rate = (total_cache_read_tokens / cached_in * 100) if cached_in else 0.0
        cost_per_run = (total_cost / total_turns) if total_turns else 0.0
        rounds_per_run = (total_tool_rounds / total_turns) if total_turns else 0.0

        # Cost per task (for histogram)
        task_costs = defaultdict(float)
        for msg in messages_list:
            if msg["task_id"]:
                task_costs[msg["task_id"]] += msg["cost_usd"]

        cost_per_task = [{"task_id": tid, "cost": cost} for tid, cost in sorted(task_costs.items(), key=lambda x: x[1], reverse=True)[:10]]

        # Add task titles
        task_lookup = {t["id"]: t for t in tasks}
        for item in cost_per_task:
            task = task_lookup.get(item["task_id"])
            item["title"] = task["title"] if task else f"Task {item['task_id']}"

        # Task analysis
        task_durations = []
        for task in tasks:
            if task["status"] == "done" and task["created_at"] and task["updated_at"]:
                duration = task["updated_at"] - task["created_at"]
                task_durations.append({
                    "id": task["id"],
                    "title": task["title"],
                    "duration": duration,
                    "duration_hours": duration / 3600,
                })

        task_durations.sort(key=lambda x: x["duration"], reverse=True)
        longest_tasks = task_durations[:10]

        avg_duration = sum(t["duration"] for t in task_durations) / len(task_durations) if task_durations else 0
        avg_duration_hours = avg_duration / 3600

        # Blocked tasks
        blocked_tasks = [t for t in tasks if t["status"] == "blocked"]

        # Tasks waiting for approval
        approval_tasks = [t for t in tasks if t["status"] == "needs_approval"]

        # Time series - tasks completed per day
        completed_per_day = defaultdict(int)
        for task in tasks:
            if task["status"] == "done" and task["updated_at"]:
                day_ts = int(task["updated_at"] / 86400) * 86400
                completed_per_day[day_ts] += 1

        daily_stats = sorted([{"day": int(ts), "completed": count} for ts, count in completed_per_day.items()])

        # Cost trend over time
        cost_per_day = defaultdict(float)
        for msg in messages_list:
            if msg["ts"]:
                day_ts = int(msg["ts"] / 86400) * 86400
                cost_per_day[day_ts] += msg["cost_usd"]

        cost_trend = sorted([{"day": int(ts), "cost": cost} for ts, cost in cost_per_day.items()])

        return {
            "total_tasks": total_tasks,
            "completed": completed,
            "completed_pct": completed_pct,
            "in_progress": in_progress,
            "blocked": blocked,
            "needs_approval": needs_approval,
            "todo": todo,
            "agent_stats": agent_stats,
            "total_cost": total_cost,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "total_tool_rounds": total_tool_rounds,
            "cache_hit_rate": cache_hit_rate,
            "cost_per_run": cost_per_run,
            "rounds_per_run": rounds_per_run,
            "cost_per_task": cost_per_task,
            "task_durations": task_durations,
            "avg_duration_hours": avg_duration_hours,
            "longest_tasks": longest_tasks,
            "blocked_tasks": blocked_tasks,
            "approval_tasks": approval_tasks,
        }

    @app.get("/stats")
    def stats_page() -> str:
        """GET /stats - Display the project statistics dashboard."""
        metrics = compute_stats()
        return render_template("stats.html", page="stats", metrics=metrics)

    return app
