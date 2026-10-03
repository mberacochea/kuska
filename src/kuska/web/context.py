"""The web app's per-process state and the helpers its pages share."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import render_template, request
from markupsafe import escape
from peewee import SqliteDatabase

from .. import eventfmt, worktree
from .. import tables as tbl
from ..db import EVENT_KINDS, TASK_STATUSES, connect, init_db
from ..export import _fmt_ts
from ..markdown import render as md
from ..project import (
    AGENT_FIELDS,
    db_path,
    load_config,
    read_prompt,
    registry_load,
    sync_agents_from_config,
)
from ..runtime import one_line
from ..store import (
    blocking_map,
    docs_list,
    list_agents,
    list_features,
    list_tags,
    list_tasks,
    recent_events,
    task_dependencies,
    task_dependents,
    task_events,
    task_messages,
    update_task_status,
)
from .helpers import _activity_qs, _ago, _clock, _group_runs


def make_context(app, project_dir: Path) -> SimpleNamespace:
    """The open project and the rendering helpers every page module shares.

    Closures over `state`, exactly as they were inside create_app; the page
    modules get them back as attributes of the returned namespace."""
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
        feature_list: list[str] | None = None,
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
            features=list_features(db()),
            feature_list=feature_list or [],
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
            features=list_features(db()) if edit else [],
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

    return SimpleNamespace(
        _activity_query=_activity_query,
        _merge_queue_context=_merge_queue_context,
        _toast=_toast,
        activity_log=activity_log,
        activity_tail=activity_tail,
        agent_models=agent_models,
        agent_rows=agent_rows,
        data_rows=data_rows,
        db=db,
        dependency_candidates=dependency_candidates,
        doc_editor=doc_editor,
        docs_table=docs_table,
        editor=editor,
        merge_queue_rows=merge_queue_rows,
        open_project=open_project,
        render_row=render_row,
        rows_with_toast=rows_with_toast,
        state=state,
        task_activity=task_activity,
        task_panel=task_panel,
        tasks_container=tasks_container,
        tasks_table=tasks_table,
    )
