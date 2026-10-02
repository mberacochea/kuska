"""Flask + HTMX web UI - two pages, where the human writes.

The job is rendering HTML fragments, not validating
JSON, and Jinja2 comes bundled. HTMX loads from a CDN, so there is no npm, no
build step and no bundler."""

from __future__ import annotations

import itertools
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from flask import Flask, make_response, render_template, request
from flask_htmx import HTMX
from markupsafe import escape
from peewee import PeeweeException, SqliteDatabase

from . import eventfmt
from . import tables as tbl
from . import worktree
from .db import EVENT_KINDS, HUMAN, TASK_STATUSES, connect, init_db, now
from .export import _fmt_ts, export_markdown
from .markdown import render as md
from .project import (
    AGENT_FIELDS,
    db_path,
    find_project,
    load_config,
    read_prompt,
    registry_load,
    remove_agent_config,
    set_agent_config,
    sync_agents_from_config,
    write_prompt,
)
from .runtime import one_line
from .store import (
    add_dependency,
    add_task,
    avg_task_duration,
    blocking_map,
    cost_by_task,
    delete_agent,
    delete_task,
    docs_get,
    docs_list,
    docs_set,
    filter_tasks,
    full_text_search,
    get_event,
    get_task,
    list_agents,
    list_tags,
    list_tasks,
    longest_tasks,
    recent_events,
    recent_runs,
    remove_dependency,
    reply_to_task,
    run_events,
    task_counts_by_agent,
    task_dependencies,
    task_dependents,
    task_events,
    task_messages,
    task_status_counts,
    token_usage_by_agent,
    update_task,
    update_task_status,
)

# bound to the app in create_app(); reads the current request's HX-* headers
htmx = HTMX()


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


def _clock(ts: float | None) -> str:
    """HH:MM:SS for one event's timestamp.

    Individual event log lines used a humanized "12h ago" - fine for a task's
    created/updated column, but relative time on a per-event feed just makes
    old events unreadable ("3d ago" tells you nothing about when in the day
    it happened). The full date is still one hover away via the same title
    attribute these spans already carried.
    """
    if not ts:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(ts))


# a run's terminal `result` event has a label like "done - $5.1009, 103
# rounds, ..." (claude.py) or "done - $0.0312, 40/1217 tok" (codex.py,
# openai.py) - every backend agrees on "<status> - <summary with a $cost>",
# so pulling the status and the first dollar amount out of it is enough to
# read across all three without parsing the rest of the summary.
_RUN_COST = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")


def _run_status_cost(label: str | None) -> tuple[str | None, float | None]:
    """(status, cost) parsed from a `result` event's label, degrading to (None, None)."""
    if not label:
        return None, None
    status = label.split(" - ", 1)[0].strip() or None
    match = _RUN_COST.search(label)
    cost = float(match.group(1)) if match else None
    return status, cost


def _group_runs(events: list[dict]) -> list[dict]:
    """Group a task's chronological events into per-run blocks.

    One agent works a task's runs one at a time, so a task's events already
    arrive in contiguous per-run blocks in id order - `itertools.groupby`
    over the flat list is enough, no need to bucket by run_id first.

    Returns one dict per run: `run_id`, `agent`, `first_ts`, `count`,
    `status`/`cost` (parsed from the run's terminal `result` event, both None
    while the run is still in flight), `events` (the run's events, newest
    first), and `open` (True only for the newest run - the one a human is
    most likely watching right now).

    Runs and, within each run, events are returned newest-first, since a
    human scanning a task's activity cares about what just happened.
    """
    groups: list[dict] = []
    for run_id, members in itertools.groupby(events, key=lambda e: e.get("run_id")):
        members = list(members)
        result = next((e for e in members if e.get("kind") == "result"), None)
        status, cost = _run_status_cost(result["label"] if result else None)
        groups.append(
            {
                "run_id": run_id,
                "agent": members[0].get("agent"),
                "first_ts": members[0].get("ts"),
                "count": len(members),
                "status": status,
                "cost": cost,
                "events": members,
                "open": False,
            }
        )
    if groups:
        groups[-1]["open"] = True
    groups.reverse()
    for g in groups:
        g["events"] = list(reversed(g["events"]))
    return groups


def _activity_qs(agent: str, kind: str, show_system: bool) -> str:
    """Query string for the current fleet-tail filter state, or "" when unfiltered.

    Rendered onto the tail's own `hx-get` so its 3s self-poll keeps re-asking
    with the same filters instead of silently resetting them - and, when no
    filter is set, this returns "" so the attribute stays the bare
    `/agents/activity` the tests (and a plain first load) expect.
    """
    params = [(k, v) for k, v in (("agent", agent), ("kind", kind)) if v]
    if show_system:
        params.append(("system", "1"))
    return f"?{urlencode(params)}" if params else ""


# agent names become TOML keys and prompt filenames, so keep them plain
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BACKENDS = ("claude", "codex", "openai")
DOC_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# run_id is a 12-hex id set per Monologue (runtime.py:429)
RUN_ID = re.compile(r"[0-9a-f]{12}")

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


def wants_fragment() -> bool:
    """True when htmx is asking a page route for a fragment to swap into an element.

    A page route serves two audiences: a browser navigating to the URL, which
    needs the whole document, and htmx swapping part of the page in place,
    which needs only the fragment its hx-target expects. Handing the full page
    to an htmx swap is what nests a second copy of a table inside one of its
    own rows.

    htmx marks its requests with HX-Request, with one exception that matters
    here: on back/forward it may re-request the URL with
    HX-History-Restore-Request set, and it then replaces the entire body with
    whatever comes back. That case wants the full page, so it is excluded.
    """
    return bool(htmx) and not htmx.history_restore_request


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
    htmx.init_app(app)
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

    def render_row(task: dict) -> str:
        """Render a single task row - title links out to its own /tasks/<id> page."""
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
    ) -> str:
        """Render the full task table. Defaults to all tasks, unfiltered, unsorted."""
        return render_template(
            "tasks_table.html",
            tasks=tasks if tasks is not None else list_tasks(db()),
            render_row=render_row,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )

    def tasks_container(
        tasks: list[dict] | None = None,
        search: str = "",
        status_list: list[str] | None = None,
        agent_list: list[str] | None = None,
        tag_list: list[str] | None = None,
        sort_by: str | None = None,
        sort_dir: str = "asc",
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
            tasks_table=tasks_table(tasks, sort_by, sort_dir),
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            tags=list_tags(db()),
            search=search,
            status_list=status_list or [],
            agent_list=agent_list or [],
            tag_list=tag_list or [],
            sort_by=sort_by or "",
            sort_dir=sort_dir,
        )

    def dependency_candidates(task: dict) -> list[dict]:
        """Return tasks that this task could depend on (excluding itself and existing deps)."""
        taken = {d["id"] for d in task_dependencies(db(), task["id"])} | {task["id"]}
        return [t for t in list_tasks(db()) if t["id"] not in taken]

    def task_activity(task_id: int) -> str:
        """Render the activity feed for a task."""
        events = task_events(db(), task_id)
        run_groups = _group_runs(events)
        return render_template(
            "task_activity.html",
            run_groups=run_groups,
            ago=_ago,
            clock=_clock,
            summarize=eventfmt.summarize,
            detail_html=eventfmt.detail_html,
            glyph=eventfmt.glyph,
            _fmt_ts=_fmt_ts,
        )

    def task_panel(task: dict, edit: bool = False) -> str:
        """Render the standalone task page's content (display or edit mode).

        Used both as the body of the full /tasks/<id> page and as the htmx
        fragment every mutation on that page swaps back in.
        """
        # Get worktree info if the task has one
        worktree_info = None
        worktree_commands = []
        if task.get("worktree_path"):
            project = state["project"]
            path = Path(task["worktree_path"])
            wt_list = worktree.list_worktrees(project)
            wt = next((w for w in wt_list if Path(w["path"]).resolve() == path.resolve()), None)
            if wt:
                branch = wt.get("branch", "")
                base = worktree.base_branch(project)
                worktree_info = {
                    "path": task["worktree_path"],
                    "branch": branch,
                    "base": base,
                }
                # Generate the four merge commands
                if branch:
                    worktree_commands = [
                        f"git diff {base}...{branch}",
                        f"git merge --no-ff {branch}",
                        f"git checkout -b pr/{task['id']}-{branch[len('kuska/'):]} {base} && git merge --squash {branch} && git commit",
                        f"git worktree remove {task['worktree_path']} && git branch -d {branch}",
                    ]

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
            worktree_info=worktree_info,
            worktree_commands=worktree_commands,
            ago=_ago,
            md=md,
        )

    def _merge_queue_context(tasks: list[dict]) -> dict:
        """Gather worktree info, diff stats, and blocking dependencies for each
        task in ready_to_merge status, flipping merged tasks to done.

        Shared by the merge-queue page and its polled rows fragment so the
        merge-detection and sorting logic lives in exactly one place.
        """
        project = state["project"]
        base = worktree.base_branch(project)

        # Get all worktrees
        wt_list = worktree.list_worktrees(project)
        worktrees_map = {
            Path(wt["path"]).name.replace("task-", ""): wt
            for wt in wt_list if wt.get("branch")
        }

        # Get merged branches
        merged = worktree.merged_branches(project, base)

        # Get diff stats for each task
        diffs = {}
        ahead = {}
        for task in tasks:
            if task.get("worktree_path"):
                path = Path(task["worktree_path"])
                wt = next((w for w in wt_list if Path(w["path"]).resolve() == path.resolve()), None)
                if wt and wt.get("branch"):
                    diffs[task["id"]] = worktree.diff_stat(project, wt["branch"], base)
                    ahead[task["id"]] = worktree.ahead_count(path, wt["branch"], base)
                    worktrees_map[str(task["id"])] = wt

        # Get blocked-by counts for each task
        blocks_map = {}  # task_id -> count of tasks that depend on it
        for task in tasks:
            dependents = task_dependents(db(), task["id"])
            blocks_map[task["id"]] = len([d for d in dependents if d["status"] != "done"])

        # Sort by blocks descending, then task id ascending
        tasks_sorted = sorted(
            tasks,
            key=lambda t: (-blocks_map.get(t["id"], 0), t["id"])
        )

        # Perform merge detection: flip tasks to done if branch is merged.
        # A branch that never diverged from base is trivially "merged" by
        # git's own definition, so also require it to actually be ahead -
        # otherwise every freshly-created worktree would auto-flip to done.
        for task in tasks_sorted:
            if task.get("worktree_path"):
                path = Path(task["worktree_path"])
                wt = next((w for w in wt_list if Path(w["path"]).resolve() == path.resolve()), None)
                if wt and wt.get("branch") and wt["branch"] in merged and ahead.get(task["id"], 0) > 0:
                    # Branch is merged - update task to done
                    update_task_status(db(), task["id"], "done")
                    task["status"] = "done"

        return {
            "tasks": tasks_sorted,
            "worktrees": worktrees_map,
            "diffs": diffs,
            "ahead": ahead,
            "blocks": blocks_map,
            "merged": {task["id"]: task["status"] == "done" for task in tasks_sorted},
        }

    def merge_queue_rows(tasks: list[dict]) -> str:
        """Render the merge queue rows fragment."""
        return render_template("merge_queue_table.html", **_merge_queue_context(tasks))

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

    def _activity_query() -> tuple[list[dict], str, str, bool, str]:
        """Read the activity filters from the request and fetch matching events.

        Returns (events, agent_filter, kind_filter, show_system, qs).
        """
        agent = request.args.get("agent", "").strip() or None
        kind = request.args.get("kind", "").strip() or None
        show_system = request.args.get("system") == "1"

        # Exclude system events unless explicitly requested
        exclude_kinds = None if show_system else eventfmt.QUIET_KINDS

        events = recent_events(db(), agent=agent, kinds=kind, exclude_kinds=exclude_kinds, limit=25)
        qs = _activity_qs(agent or "", kind or "", show_system)
        return events, agent or "", kind or "", show_system, qs

    def activity_log() -> str:
        """Render just the #activity log fragment - the self-poll target.

        Deliberately excludes the filter form: that form lives once in
        activity_tail.html, outside #activity, so the 3s poll (outerHTML on
        #activity alone) never re-renders it. Returning the filters here too
        would duplicate them into the page on every poll tick.
        """
        events, _agent, _kind, _show_system, qs = _activity_query()
        return render_template(
            "activity_log.html",
            events=events,
            qs=qs,
            ago=_ago,
            clock=_clock,
            summarize=eventfmt.summarize,
            detail_html=eventfmt.detail_html,
            glyph=eventfmt.glyph,
            _fmt_ts=_fmt_ts,
        )

    def activity_tail() -> str:
        """Render the full live activity block (filters + log) for the agents page."""
        events, agent_filter, kind_filter, show_system, qs = _activity_query()
        return render_template(
            "activity_tail.html",
            events=events,
            agent_filter=agent_filter,
            kind_filter=kind_filter,
            show_system=show_system,
            agents=list_agents(db()),
            kinds=EVENT_KINDS,
            qs=qs,
            ago=_ago,
            clock=_clock,
            summarize=eventfmt.summarize,
            detail_html=eventfmt.detail_html,
            glyph=eventfmt.glyph,
            _fmt_ts=_fmt_ts,
        )

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
        """GET / - Display the project overview (description + export)."""
        return render_template(
            "project.html",
            page="project",
            description=docs_get(db(), "description") or "",
            description_html=md(docs_get(db(), "description")),
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
        tag_list = request.args.getlist("tag")
        sort_by = request.args.get("sort") or None
        sort_dir = request.args.get("direction", "asc")
        filtered = filter_tasks(
            db(),
            search,
            status=status_list or None,
            agent=agent_list or None,
            tags=tag_list or None,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        container = tasks_container(filtered, search, status_list, agent_list, tag_list, sort_by, sort_dir)

        if wants_fragment():
            return container

        return render_template(
            "tasks.html",
            page="tasks",
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            tags=list_tags(db()),
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
        except (ValueError, PeeweeException) as exc:
            return _bad_request(tasks_container(), "form", f"Failed to create task: {exc}")

        return tasks_container(), 200

    @app.post("/tasks/<int:task_id>")
    def patch_task(task_id: int) -> tuple[str, int]:
        """POST /tasks/<id> - Update task fields (title, description, assigned_to, status, tags)."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        fields = {k: v for k, v in request.form.items() if k in {"title", "description", "assigned_to", "status", "tags"}}

        # Validate title if provided
        if "title" in fields:
            title_error = validate_task_title(fields["title"], db(), task_id)
            if title_error:
                return _bad_request(task_panel(task, edit=True), "title", title_error)

        # Validate description if provided
        if "description" in fields:
            desc_error = validate_task_description(fields["description"])
            if desc_error:
                return _bad_request(task_panel(task, edit=True), "description", desc_error)

        # Validate assigned_to if provided
        if "assigned_to" in fields:
            assigned_to = fields["assigned_to"] or None
            agent_error = validate_task_assigned_to(assigned_to, db())
            if agent_error:
                return _bad_request(task_panel(task, edit=True), "assigned_to", agent_error)

        try:
            update_task(db(), task_id, **fields)
        except (ValueError, PeeweeException) as exc:
            return _bad_request(task_panel(task, edit=True), "form", f"Failed to update task: {exc}")

        task = get_task(db(), task_id)
        if not task:
            return "", 404

        # an edit from the task page comes back as the page's panel, not the row
        if "description" in fields:
            return task_panel(task) + _toast(f"task {task_id} saved"), 200
        return render_row(task), 200

    def _queue_status(task_id: int) -> str:
        task = get_task(db(), task_id)
        return "ready" if task and task["assigned_to"] else "todo"

    @app.post("/tasks/<int:task_id>/requeue")
    def requeue_task(task_id: int) -> str:
        """POST /tasks/<id>/requeue - Re-queue a task (set status to ready, or todo if unassigned)."""
        update_task_status(db(), task_id, _queue_status(task_id))
        task = get_task(db(), task_id)
        return task_panel(task) if task else ""

    @app.post("/tasks/<int:task_id>/approve")
    def approve_task(task_id: int) -> str:
        """POST /tasks/<id>/approve - Approve a task (set status to done)."""
        update_task_status(db(), task_id, "done")
        task = get_task(db(), task_id)
        return (task_panel(task) + _toast(f"task {task_id} approved")) if task else ""

    @app.post("/tasks/<int:task_id>/send-back")
    def send_back_task(task_id: int) -> str:
        """POST /tasks/<id>/send-back - Send a task back (set status to ready, or todo if unassigned)."""
        update_task_status(db(), task_id, _queue_status(task_id))
        task = get_task(db(), task_id)
        return (task_panel(task) + _toast(f"task {task_id} sent back")) if task else ""

    @app.post("/tasks/<int:task_id>/deps")
    def add_task_dependency(task_id: int) -> tuple[str, int]:
        """POST /tasks/<id>/deps - Add a task dependency."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        try:
            dep_id = int(request.form.get("depends_on", 0))
        except (ValueError, TypeError):
            return _bad_request(task_panel(task), "depends_on", "Invalid dependency ID")

        # Validate self-dependency
        if dep_id == task_id:
            return _bad_request(task_panel(task), "depends_on", "Task cannot depend on itself")

        # Validate task exists
        dep_task = get_task(db(), dep_id)
        if not dep_task:
            return _bad_request(task_panel(task), "depends_on", f"Task {dep_id} not found")

        # Check for circular dependency
        existing_deps = {d["id"] for d in task_dependencies(db(), task_id)}
        if dep_id in existing_deps:
            return _bad_request(task_panel(task), "depends_on", "Dependency already exists")

        # Check if adding this dependency would create a cycle
        # (if dep_task already depends on task_id, adding task_id->dep_id would create a cycle)
        dep_task_deps = {d["id"] for d in task_dependencies(db(), dep_id)}
        if task_id in dep_task_deps:
            return _bad_request(task_panel(task), "depends_on",
                f"Would create a circular dependency: task {dep_id} already depends on this task")

        try:
            add_dependency(db(), task_id, dep_id)
        except (ValueError, PeeweeException) as exc:
            return _bad_request(task_panel(task), "depends_on", str(exc))

        return task_panel(get_task(db(), task_id)), 200

    @app.post("/tasks/<int:task_id>/deps/<int:dep_id>/delete")
    def drop_task_dependency(task_id: int, dep_id: int) -> str:
        """POST /tasks/<id>/deps/<dep_id>/delete - Remove a task dependency."""
        remove_dependency(db(), task_id, dep_id)
        task = get_task(db(), task_id)
        return task_panel(task) if task else ""

    @app.post("/tasks/<int:task_id>/delete")
    def remove_task(task_id: int) -> str:
        """POST /tasks/<id>/delete - Delete a task."""
        delete_task(db(), task_id)
        return tasks_container()

    @app.post("/tasks/<int:task_id>/message")
    def task_message(task_id: int) -> str:
        """POST /tasks/<id>/message - Reply on a task thread.

        Reopens the task for its assigned agent if it was done/holding, so
        the reply doesn't just sit unread - see reply_to_task().
        """
        task = get_task(db(), task_id)
        if not task:
            return ""
        payload = (request.form.get("payload") or "").strip()
        if payload:
            reply_to_task(db(), task_id, payload, HUMAN)
            task = get_task(db(), task_id)
        return task_panel(task) if task else ""

    @app.get("/tasks/<int:task_id>/row")
    def task_row(task_id: int) -> str:
        """GET /tasks/<id>/row - Get a single task row for the table."""
        task = get_task(db(), task_id)
        return render_row(task) if task else ""

    @app.get("/tasks/<int:task_id>")
    def task_page(task_id: int) -> tuple[str, int] | str:
        """GET /tasks/<id> - the standalone task page (status, description,
        dependencies, message thread, and run activity).

        Same htmx-vs-browser split as /tasks: an htmx request (e.g. toggling
        edit mode, or a mutation's response target) gets just the panel
        fragment, a plain navigation gets the whole page.

        ?edit=1 - opens the description editor
        """
        task = get_task(db(), task_id)
        if not task:
            return "", 404
        panel = task_panel(task, edit=request.args.get("edit") == "1")
        if wants_fragment():
            return panel
        return render_template("task.html", page="tasks", t=task, task_panel=panel)

    @app.post("/tasks/<int:task_id>/merged")
    def mark_task_merged(task_id: int) -> str:
        """POST /tasks/<id>/merged - Mark a task as done (merged)."""
        update_task_status(db(), task_id, "done")
        # Return the merge queue fragment
        tasks_ready = list_tasks(db(), status="ready_to_merge")
        return merge_queue_rows(tasks_ready)

    @app.get("/merge-queue")
    def merge_queue_page() -> str:
        """GET /merge-queue - Display tasks ready to merge."""
        tasks_ready = list_tasks(db(), status="ready_to_merge")
        return render_template("merge_queue.html", page="merge-queue", **_merge_queue_context(tasks_ready))

    @app.get("/merge-queue/rows")
    def merge_queue_rows_fragment() -> str:
        """GET /merge-queue/rows - Return merge queue rows fragment for polling."""
        tasks_ready = list_tasks(db(), status="ready_to_merge")
        return merge_queue_rows(tasks_ready)

    @app.post("/tasks/<int:task_id>/prune")
    def prune_task_worktree(task_id: int) -> str:
        """POST /tasks/<id>/prune - Remove a task's worktree and branch."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        if not task.get("worktree_path"):
            return _toast("No worktree for this task"), 200

        project = find_project(request.args.get("project"))
        base = worktree.base_branch(project)
        path = Path(task["worktree_path"])
        branch = worktree.list_worktrees(project)

        # Find the branch for this worktree
        task_branch = None
        for wt in branch:
            if Path(wt["path"]).resolve() == path.resolve():
                task_branch = wt.get("branch")
                break

        # Check if branch is merged
        merged = worktree.merged_branches(project, base)
        if task_branch and task_branch not in merged:
            return _toast("Branch is not merged - cannot prune"), 400

        # Remove the worktree
        success, msg = worktree.remove_worktree(project, path, task_branch)
        if success:
            # Clear the worktree_path from the task
            update_task(db(), task_id, worktree_path=None)
            return merge_queue_rows(list_tasks(db(), status="ready_to_merge"))
        else:
            return _toast(f"Failed to prune: {msg}"), 400

    # ========== ROUTES: Agents ==========

    @app.get("/agents")
    def agents_page() -> str:
        """GET /agents - Display the agents page with status and activity."""
        return render_template(
            "agents.html",
            page="agents",
            agent_rows=agent_rows(),
            activity=activity_tail(),
            backends=BACKENDS,
            usage=token_usage_by_agent(db()),
        )

    @app.get("/agents/<name>")
    def agent_page(name: str) -> tuple[str, int] | str:
        """GET /agents/<name> - the standalone agent settings + prompt page.

        Same htmx-vs-browser split as /tasks/<id>: an htmx request (e.g. the
        response target of a save or delete) gets just the editor panel, a
        plain navigation gets the whole page.
        """
        if not any(a["name"] == name for a in list_agents(db())):
            return "", 404
        panel = editor(name)
        if wants_fragment():
            return panel
        return render_template("agent.html", page="agents", name=name, agent_panel=panel)

    @app.get("/agents/rows")
    def agents_rows() -> str:
        """GET /agents/rows - Get the agent list rows fragment."""
        return agent_rows()

    @app.get("/agents/activity")
    def agents_activity() -> str:
        """GET /agents/activity - Get the activity log fragment (poll target and filter-change target)."""
        return activity_log()

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
                return _bad_request(editor(name), "backend", backend_error)

        if model:
            model_error = validate_agent_model(model)
            if model_error:
                return _bad_request(editor(name), "model", model_error)

        if role:
            role_error = validate_agent_role(role)
            if role_error:
                return _bad_request(editor(name), "role", role_error)

        # Validate prices if provided
        price_error = validate_agent_prices(request.form.to_dict())
        if price_error:
            return _bad_request(editor(name), "prices", price_error)

        try:
            set_agent_config(state["project"], name, request.form.to_dict())
        except (ValueError, OSError) as exc:
            return _bad_request(editor(name), "form", f"Failed to save agent settings: {exc}")

        sync_agents_from_config(db(), state["project"])
        return editor(name) + _toast(f"{name} settings saved - restart its daemon to pick them up"), 200

    @app.post("/agents/<name>/delete")
    def remove_agent(name: str):
        """POST /agents/<name>/delete - Delete an agent, unassign its tasks, and
        redirect back to the agents list (its own page no longer exists)."""
        remove_agent_config(state["project"], name)
        delete_agent(db(), name)
        resp = make_response("")
        resp.headers["HX-Redirect"] = "/agents"
        return resp

    @app.post("/agents/<name>/context")
    def set_context(name: str) -> str:
        """POST /agents/<name>/context - Save an agent's prompt content."""
        write_prompt(state["project"], name, request.form.get("content", ""))
        return f"{name} prompt saved"

    # ========== ROUTES: Events ==========

    @app.get("/events/<int:event_id>/detail")
    def get_event_detail(event_id: int) -> str:
        """GET /events/<id>/detail - Fetch the expanded detail for an event."""
        event = get_event(db(), event_id)
        if not event:
            return ""
        return eventfmt.detail_html(event)

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

    # ========== ROUTES: Search ==========

    @app.get("/search")
    def search_page() -> str:
        """GET /search - Display search page with results."""
        query = request.args.get("q", "").strip()
        page = request.args.get("page", 1, type=int)
        tables_filter = request.args.getlist("tables[]")

        # Validate page number
        page = max(page, 1)

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

    # ========== ROUTES: Run Transcripts ==========

    @app.get("/runs")
    def runs_index() -> str:
        """GET /runs - Display the index of recent agent invocations."""
        runs = recent_runs(db(), limit=50)
        return render_template(
            "runs.html",
            page="runs",
            runs=runs,
            ago=_ago,
            _fmt_ts=_fmt_ts,
        )

    @app.get("/runs/<run_id>")
    def run_transcript(run_id: str) -> str:
        """GET /runs/<run_id> - Display a complete invocation transcript.

        A run_id is a 12-hex id set per Monologue (runtime.py:429).
        """
        # Validate run_id format before querying
        if not RUN_ID.fullmatch(run_id):
            return render_template(
                "run.html",
                page="runs",
                run_id=None,
                run=None,
                events=[],
                error="Invalid run ID format",
                ago=_ago,
                summarize=eventfmt.summarize,
                detail_html=eventfmt.detail_html,
                glyph=eventfmt.glyph,
                _fmt_ts=_fmt_ts,
            )

        # Fetch all events for this run
        events = run_events(db(), run_id)

        if not events:
            return render_template(
                "run.html",
                page="runs",
                run_id=run_id,
                run=None,
                events=[],
                error="No run found with this ID",
                ago=_ago,
                summarize=eventfmt.summarize,
                detail_html=eventfmt.detail_html,
                glyph=eventfmt.glyph,
                _fmt_ts=_fmt_ts,
            )

        # Extract run metadata from events
        first_event = events[0]
        last_event = events[-1]
        result_event = next((e for e in events if e.get("kind") == "result"), None)
        status, cost = _run_status_cost(result_event["label"] if result_event else None)

        run_data = {
            "run_id": run_id,
            "agent": first_event.get("agent"),
            "task_id": first_event.get("task_id"),
            "first_ts": first_event.get("ts"),
            "last_ts": last_event.get("ts"),
            "event_count": len(events),
            "status": status,
            "cost": cost,
        }

        return render_template(
            "run.html",
            page="runs",
            run_id=run_id,
            run=run_data,
            events=events,
            error=None,
            ago=_ago,
            clock=_clock,
            summarize=eventfmt.summarize,
            detail_html=eventfmt.detail_html,
            glyph=eventfmt.glyph,
            _fmt_ts=_fmt_ts,
        )

    # ========== ROUTES: Stats Dashboard ==========

    def compute_stats() -> dict[str, Any]:
        """Compute all metrics for the stats dashboard."""
        agents = list_agents(db())
        usage = token_usage_by_agent(db())
        agent_task_counts = {r["assigned_to"]: r for r in task_counts_by_agent(db())}

        # Project overview stats
        status_counts = {r["status"]: r["count"] for r in task_status_counts(db())}
        total_tasks = sum(status_counts.values())
        completed = status_counts.get("done", 0)
        in_progress = status_counts.get("in_progress", 0)
        blocked = status_counts.get("blocked", 0)
        needs_approval = status_counts.get("needs_approval", 0)
        ready_to_merge = status_counts.get("ready_to_merge", 0)
        todo = status_counts.get("todo", 0)
        ready = status_counts.get("ready", 0)

        completed_pct = int((completed / total_tasks * 100) if total_tasks > 0 else 0)

        # Agent productivity stats
        agent_stats = []
        for agent in agents:
            agent_usage = next((u for u in usage if u["agent"] == agent["name"]), None)
            counts = agent_task_counts.get(agent["name"])

            stats = {
                "name": agent["name"],
                "backend": agent["backend"],
                "status": agent["status"],
                "current_task_id": agent["current_task_id"],
                "completed_tasks": counts["completed_tasks"] if counts else 0,
                "total_tasks": counts["total_tasks"] if counts else 0,
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
        cost_per_task = cost_by_task(db())
        for item in cost_per_task:
            if not item["title"]:
                item["title"] = f"Task {item['task_id']}"

        # Task duration analysis
        longest = longest_tasks(db())
        for t in longest:
            t["duration_hours"] = t["duration"] / 3600
        avg_duration_hours = avg_task_duration(db()) / 3600

        blocked_tasks = list_tasks(db(), status="blocked")
        approval_tasks = list_tasks(db(), status="needs_approval")
        merge_tasks = list_tasks(db(), status="ready_to_merge")

        return {
            "total_tasks": total_tasks,
            "completed": completed,
            "completed_pct": completed_pct,
            "in_progress": in_progress,
            "blocked": blocked,
            "needs_approval": needs_approval,
            "ready_to_merge": ready_to_merge,
            "todo": todo,
            "ready": ready,
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
            "avg_duration_hours": avg_duration_hours,
            "longest_tasks": longest,
            "blocked_tasks": blocked_tasks,
            "approval_tasks": approval_tasks,
            "merge_tasks": merge_tasks,
        }

    @app.get("/stats")
    def stats_page() -> str:
        """GET /stats - Display the project statistics dashboard."""
        metrics = compute_stats()
        return render_template("stats.html", page="stats", metrics=metrics)

    return app
