"""Routes: project, tasks, board and merge-queue."""

from __future__ import annotations

from pathlib import Path

from flask import render_template, request, session
from peewee import PeeweeException

from .. import worktree
from ..db import HUMAN, TASK_STATUSES
from ..export import export_markdown
from ..markdown import render as md
from ..project import registry_load
from ..store import (
    InvalidTransition,
    add_dependency,
    add_task,
    bulk_update_status,
    delete_task,
    docs_get,
    docs_set,
    filter_tasks,
    get_task,
    list_agents,
    list_features,
    list_tags,
    list_tasks,
    remove_dependency,
    reply_to_task,
    task_dependencies,
    transition,
    update_task,
)
from .helpers import (
    _bad_request,
    validate_task_assigned_to,
    validate_task_description,
    validate_task_title,
    wants_fragment,
)


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    _merge_queue_context = ctx._merge_queue_context
    _toast = ctx._toast
    db = ctx.db
    merge_queue_rows = ctx.merge_queue_rows
    render_row = ctx.render_row
    project = ctx.project
    task_panel = ctx.task_panel
    tasks_container = ctx.tasks_container

    # ========== ROUTES: Project/Main ==========

    @app.get("/")
    def index() -> str:
        """GET / - Display the project overview (description + export)."""
        return render_template(
            "project.html",
            page="project",
            description=docs_get(db(), "description") or "",
        )

    @app.post("/markdown")
    def markdown_view() -> str:
        """POST /markdown - render a markdown field's current text for its
        View tab. `field` names the form value holding the text."""
        text = request.form.get(request.form.get("field", "text"), "")
        return md(text) or "<p class='muted'>Nothing to preview.</p>"

    def filtered_container(args) -> str:
        """The tasks container for the filter/sort fields in `args`.

        `args` is the query string of a GET /tasks, or the form of a bulk
        move, which re-sends the filter form so the table comes back showing
        the same view.
        """
        search = args.get("search", "").strip()
        status_list = args.getlist("status")
        agent_list = args.getlist("agent")
        tag_list = args.getlist("tag")
        feature_list = args.getlist("feature")
        sort_by = args.get("sort") or None
        sort_dir = args.get("direction", "asc")
        filtered = filter_tasks(
            db(),
            search,
            status=status_list or None,
            agent=agent_list or None,
            feature=feature_list or None,
            tags=tag_list or None,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        return tasks_container(
            filtered, search, status_list, agent_list, tag_list, sort_by, sort_dir, feature_list
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
        container = filtered_container(request.args)

        if wants_fragment():
            return container

        return render_template(
            "tasks.html",
            page="tasks",
            agents=list_agents(db()),
            statuses=TASK_STATUSES,
            tags=list_tags(db()),
            features=list_features(db()),
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
            session["project"] = str(Path(target).resolve())
        return index()

    @app.post("/export")
    def do_export() -> str:
        """POST /export - Export the project to markdown files."""
        out = project() / ".agents-export"
        written = export_markdown(db(), out)
        return f"exported {len(written)} files to {out}"

    # ========== ROUTES: Tasks ==========

    @app.post("/tasks")
    def create_task() -> tuple[str, int]:
        """POST /tasks - Create a new task."""
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        assigned_to = request.form.get("assigned_to") or None
        feature = request.form.get("feature", "").strip() or None

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
            add_task(db(), title, description, assigned_to, feature=feature)
        except (ValueError, PeeweeException) as exc:
            return _bad_request(tasks_container(), "form", f"Failed to create task: {exc}")

        return tasks_container(), 200

    @app.post("/tasks/bulk")
    def bulk_move_tasks() -> str:
        """POST /tasks/bulk - move the ticked tasks (`ids`) to one status
        (`to_status`).

        Follows the board's rules (see bulk_update_status): in progress is
        agent-only both ways and ready needs an agent. Tasks that cannot go
        are left as they are and named in the toast, so one bad pick never
        fails the whole batch. The container comes back filtered the way the
        page was, since the filter form is posted along.
        """
        status = request.form.get("to_status", "")
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        if not ids:
            return filtered_container(request.form) + _toast("tick some tasks first")
        try:
            result = bulk_update_status(db(), ids, status)
        except ValueError as exc:
            return filtered_container(request.form) + _toast(str(exc))
        message = f"{len(result['moved'])} moved to {status}"
        if result["skipped"]:
            why: dict[str, list[int]] = {}
            for task_id, reason in result["skipped"]:
                why.setdefault(reason, []).append(task_id)
            message += "; skipped " + ", ".join(
                f"{reason}: " + " ".join(f"#{i}" for i in task_ids) for reason, task_ids in why.items()
            )
        return filtered_container(request.form) + _toast(message)

    @app.post("/tasks/<int:task_id>")
    def patch_task(task_id: int) -> tuple[str, int]:
        """POST /tasks/<id> - Update task fields (title, description, assigned_to, status, tags, feature)."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        fields = {
            k: v for k, v in request.form.items()
            if k in {"title", "description", "assigned_to", "status", "tags", "feature"}
        }

        # Validate title if provided
        if "title" in fields:
            title_error = validate_task_title(fields["title"], db(), task_id)
            if title_error:
                return _bad_request(task_panel(task), "title", title_error)

        # Validate description if provided
        if "description" in fields:
            desc_error = validate_task_description(fields["description"])
            if desc_error:
                return _bad_request(task_panel(task), "description", desc_error)

        # Validate assigned_to if provided
        if "assigned_to" in fields:
            assigned_to = fields["assigned_to"] or None
            agent_error = validate_task_assigned_to(assigned_to, db())
            if agent_error:
                return _bad_request(task_panel(task), "assigned_to", agent_error)

        # the status dropdown is an explicit human override, and leaves a note
        if "status" in fields:
            status = fields.pop("status")
            try:
                transition(db(), task_id, "force", actor=HUMAN, to=status)
            except (ValueError, InvalidTransition) as exc:
                return _bad_request(task_panel(task), "status", str(exc))

        try:
            update_task(db(), task_id, **fields)
        except (ValueError, PeeweeException) as exc:
            return _bad_request(task_panel(task), "form", f"Failed to update task: {exc}")

        task = get_task(db(), task_id)
        if not task:
            return "", 404

        # an edit from the task page comes back as the page's panel, not the row
        if "description" in fields:
            return task_panel(task) + _toast(f"task {task_id} saved"), 200
        return render_row(task), 200

    def _event_panel(task_id: int, event: str, done_message: str) -> str:
        """Apply a lifecycle event and return the task panel; a refusal comes back as a toast."""
        try:
            transition(db(), task_id, event)
        except InvalidTransition as exc:
            task = get_task(db(), task_id)
            return (task_panel(task) + _toast(str(exc))) if task else ""
        task = get_task(db(), task_id)
        return (task_panel(task) + _toast(done_message)) if task else ""

    @app.post("/tasks/<int:task_id>/requeue")
    def requeue_task(task_id: int) -> str:
        """POST /tasks/<id>/requeue - Re-queue a task (ready, or todo if unassigned)."""
        return _event_panel(task_id, "requeue", f"task {task_id} re-queued")

    @app.post("/tasks/<int:task_id>/approve")
    def approve_task(task_id: int) -> str:
        """POST /tasks/<id>/approve - Approve a task (set status to done)."""
        return _event_panel(task_id, "approve", f"task {task_id} approved")

    @app.post("/tasks/<int:task_id>/send-back")
    def send_back_task(task_id: int) -> str:
        """POST /tasks/<id>/send-back - Send a task back (ready, or todo if unassigned)."""
        return _event_panel(task_id, "requeue", f"task {task_id} sent back")

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

    @app.get("/tasks/<int:task_id>/deps/candidates")
    def dependency_options(task_id: int) -> tuple[str, int] | str:
        """GET /tasks/<id>/deps/candidates?q= - the <select> of matching dependency candidates,
        loaded on demand so the page never renders every task."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404
        return render_template(
            "dep_options.html", candidates=ctx.dependency_candidates(task, request.args.get("q", ""))
        )

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
        a mutation's response target) gets just the panel fragment, a plain
        navigation gets the whole page.
        """
        task = get_task(db(), task_id)
        if not task:
            return "", 404
        panel = task_panel(task)
        if wants_fragment():
            return panel
        return render_template("task.html", page="tasks", t=task, task_panel=panel)

    # ========== ROUTES: Board ==========

    # statuses shown in the Finished column; waiting ones sort ahead of done
    waiting_statuses = ("needs_approval", "ready_to_merge", "blocked")
    done_shown = 20

    def board_html(picker_id: int | None = None, toast: str | None = None, feature: str = "") -> str:
        """Render the whole #board (plus an optional toast).

        Every board response is the full board: there are only four short
        columns, and swapping all of it means a refused or completed move can
        never leave the source and target columns out of step. `feature` limits
        the cards to one feature ("" shows all).
        """
        tasks = list_tasks(db(), feature=feature or None)
        by_status: dict[str, list[dict]] = {}
        for t in tasks:
            by_status.setdefault(t["status"], []).append(t)
        waiting = [t for s in waiting_statuses for t in by_status.get(s, [])]
        done = sorted(by_status.get("done", []), key=lambda t: (t["updated_at"] or 0, t["id"]), reverse=True)
        columns = [
            {"key": "todo", "title": "Todo", "drop": True, "cards": by_status.get("todo", [])},
            {"key": "ready", "title": "Ready", "drop": True, "cards": by_status.get("ready", [])},
            {"key": "in_progress", "title": "In progress", "drop": False, "cards": by_status.get("in_progress", [])},
            {"key": "finished", "title": "Finished", "drop": True, "cards": waiting + done[:done_shown]},
        ]
        html = render_template(
            "board_columns.html",
            columns=columns,
            picker_id=picker_id,
            agents=list_agents(db()),
            feature=feature,
            features=list_features(db()),
        )
        return html + _toast(toast) if toast else html

    @app.get("/board")
    def board_page() -> str:
        """GET /board - the Kanban board. An htmx request gets just #board."""
        feature = request.args.get("feature", "")
        if wants_fragment():
            return board_html(feature=feature)
        return render_template(
            "board.html",
            page="board",
            board=board_html(feature=feature),
            feature=feature,
            features=list_features(db()),
        )

    @app.post("/tasks/<int:task_id>/move")
    def move_task(task_id: int) -> tuple[str, int] | str:
        """POST /tasks/<id>/move - move a card to a column (form field `column`).

        Rules (plan_kanban_board): todo<->ready; finished->todo/ready;
        todo/ready/waiting->finished sets done. In progress is agent-only, in
        both directions. Moving to ready needs an agent: with none on the
        task and no `assigned_to` posted, the card comes back with an agent
        picker that posts here again. A refused move re-renders the board
        unchanged with a toast, so the dragged card snaps back.
        """
        task = get_task(db(), task_id)
        if not task:
            return "", 404
        column = request.form.get("column", "")
        feature = request.form.get("feature", "")
        status = task["status"]

        if status == "in_progress":
            return board_html(toast=f"task {task_id} is in progress - only an agent moves it", feature=feature)
        if column not in ("todo", "ready", "finished"):
            return board_html(toast="that column does not accept cards", feature=feature)

        def refused(exc: InvalidTransition) -> str:
            return board_html(toast=str(exc), feature=feature)

        # a waiting card is already in Finished; dropping it there approves it
        if column == "finished":
            if status == "done":
                return board_html(feature=feature)
            try:
                transition(db(), task_id, "close")
            except InvalidTransition as exc:
                if status == "ready_to_merge":
                    return board_html(
                        toast=f"task {task_id} is waiting to merge - merge its branch, then use the Merge queue",
                        feature=feature,
                    )
                return refused(exc)
            return board_html(toast=f"task {task_id} marked done", feature=feature)
        if column == status:
            return board_html(feature=feature)

        if column == "todo":
            try:
                transition(db(), task_id, "park")
            except InvalidTransition as exc:
                return refused(exc)
            return board_html(toast=f"task {task_id} moved to todo", feature=feature)

        # column == "ready": needs an agent
        assigned_to = request.form.get("assigned_to", "").strip() or None
        if "assigned_to" in request.form:
            if not assigned_to:
                return board_html(picker_id=task_id, toast="choose an agent first", feature=feature)
            agent_error = validate_task_assigned_to(assigned_to, db())
            if agent_error:
                return board_html(picker_id=task_id, toast=agent_error, feature=feature)
            update_task(db(), task_id, assigned_to=assigned_to)
        elif not task["assigned_to"]:
            return board_html(picker_id=task_id, toast="choose an agent to make it ready", feature=feature)
        try:
            # a card from Finished is re-queued; one from todo is made ready
            transition(db(), task_id, "make_ready" if status == "todo" else "requeue")
        except InvalidTransition as exc:
            return refused(exc)
        return board_html(toast=f"task {task_id} is ready", feature=feature)

    @app.post("/tasks/<int:task_id>/merged")
    def mark_task_merged(task_id: int) -> str:
        """POST /tasks/<id>/merged - Mark a task as done (merged).

        Done only when git sees the branch merged. Otherwise the row comes back
        with a "Mark merged anyway" button that posts confirm=1, for squash
        merges git cannot detect.
        """
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        def rows(message: str = "", unconfirmed: int | None = None) -> str:
            html = merge_queue_rows(list_tasks(db(), status="ready_to_merge"), unconfirmed=unconfirmed)
            return html + _toast(message) if message else html

        if request.form.get("confirm") != "1":
            root = project()
            base = worktree.base_branch(root)
            branch = None
            if task.get("worktree_path"):
                path = Path(task["worktree_path"])
                branch = next(
                    (wt.get("branch") for wt in worktree.list_worktrees(root)
                     if Path(wt["path"]).resolve() == path.resolve()),
                    None,
                )
            if not branch or not worktree.is_branch_merged(
                root, branch, base, task.get("worktree_base_sha"), task_id
            ):
                return rows(f"{branch or 'branch'} is not merged into {base} yet", unconfirmed=task_id)
        try:
            transition(db(), task_id, "merged")
        except InvalidTransition as exc:
            return rows(str(exc))
        return rows()

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
        """POST /tasks/<id>/prune - Remove a merged task's worktree and branch,
        then mark the task done so it leaves the queue."""
        task = get_task(db(), task_id)
        if not task:
            return "", 404

        def rows(message: str) -> str:
            # Always 200: htmx doesn't swap 4xx bodies, so the toast would be lost
            return merge_queue_rows(list_tasks(db(), status="ready_to_merge")) + _toast(message)

        if not task.get("worktree_path"):
            return rows("No worktree for this task")

        root = project()
        base = worktree.base_branch(root)
        path = Path(task["worktree_path"])

        # Find the branch for this worktree
        task_branch = next(
            (wt.get("branch") for wt in worktree.list_worktrees(root)
             if Path(wt["path"]).resolve() == path.resolve()),
            None,
        )

        if task_branch and task_branch not in worktree.merged_branches(root, base) and not (
            worktree.is_branch_merged(root, task_branch, base, task.get("worktree_base_sha"), task_id)
        ):
            return rows("Branch is not merged - cannot prune")

        success, msg = worktree.remove_worktree(root, path, task_branch)
        if not success:
            return rows(f"Failed to prune: {msg}")
        update_task(db(), task_id, worktree_path=None)
        try:
            transition(db(), task_id, "merged")
        except InvalidTransition as exc:
            return rows(str(exc))
        return rows(f"task {task_id} pruned")
