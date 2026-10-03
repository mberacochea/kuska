"""Tasks: create, edit, filter, and claim one atomically."""

from __future__ import annotations

import functools
import operator
import time
from typing import Any

from peewee import JOIN, SqliteDatabase, fn

from ..db import TASK_STATUSES, now
from ..models import Feature, Task, TaskDep, row, rows
from .common import bound
from .features import ensure_feature, norm_feature_name


def _tasks():
    """Every task query starts here: the task's columns plus its feature's
    name as "feature" (None when it has none) next to "feature_id"."""
    return Task.select(Task, Feature.name.alias("feature")).join(
        Feature, JOIN.LEFT_OUTER, on=(Task.feature_id == Feature.id)
    )


def _norm_tags(value: str | None) -> str | None:
    """Normalize tags: split, strip, lowercase, deduplicate, rejoin.

    None or empty string means no tags.
    """
    if not value:
        return None
    tags = set()
    for tag in value.split(','):
        tag = tag.strip().lower()
        if tag:
            tags.add(tag)
    return ','.join(sorted(tags)) if tags else None


@bound
def add_task(
    db: SqliteDatabase,
    title: str,
    description: str = "",
    assigned_to: str | None = None,
    feature: str | None = None,
    tags: str | None = None,
) -> int:
    """Create a new task.

    Creates a new "todo" task (a waiting list; it must be moved to "ready"
    before an agent will claim it). assigned_to names the agent it is for.

    Args:
        db: SqliteDatabase instance for this project.
        title: Short task name.
        description: Optional longer explanation of what to do.
        assigned_to: Optional agent name to assign this task to.
        feature: Optional name of the feature this task belongs to (e.g.
                 "search"); created if it does not exist yet. Empty/None
                 means no feature.
        tags: Optional comma-separated tags for filtering (e.g. "frontend,bug").

    Returns:
        int: New task ID.

    Examples:
        >>> task_id = add_task(db, "Review PR #42", "Check for style issues")
        >>> task_id
        15
    """
    ts = now()
    task = Task.create(
        title=title,
        description=description,
        assigned_to=assigned_to or None,
        status="todo",
        feature_id=ensure_feature(db, feature),
        tags=_norm_tags(tags),
        created_at=ts,
        updated_at=ts,
    )
    return int(task.id)


@bound
def update_task_status(db: SqliteDatabase, task_id: int, status: str) -> None:
    """Set a task's status to a valid state.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task to update.
        status: New status; must be one of TASK_STATUSES.

    Raises:
        ValueError: If status is not in TASK_STATUSES.
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {status}")
    Task.update(status=status, updated_at=now()).where(Task.id == task_id).execute()


@bound
def bulk_update_status(db: SqliteDatabase, task_ids: list[int], status: str) -> dict:
    """Move several tasks to one status, skipping the ones that may not go.

    The same rules as the board: only an agent starts work, so `in_progress`
    is never a target and a task that is in progress is left alone; a task
    needs an agent before it can be `ready`.

    Args:
        db: SqliteDatabase instance for this project.
        task_ids: Tasks to move. Unknown ids are skipped.
        status: Target status; must be one of TASK_STATUSES.

    Returns:
        dict: `moved` (ids changed) and `skipped` (a list of (id, reason)).
        A task already in the target status counts as moved, not skipped.

    Raises:
        ValueError: If status is unknown or is "in_progress".
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {status}")
    if status == "in_progress":
        raise ValueError("only an agent moves a task to in_progress")
    moved: list[int] = []
    skipped: list[tuple[int, str]] = []
    for task_id in dict.fromkeys(task_ids):
        task = get_task(db, task_id)
        if not task:
            skipped.append((task_id, "not found"))
        elif task["status"] == "in_progress":
            skipped.append((task_id, "in progress"))
        elif status == "ready" and not task["assigned_to"]:
            skipped.append((task_id, "no agent"))
        else:
            if task["status"] != status:
                update_task_status(db, task_id, status)
            moved.append(task_id)
    return {"moved": moved, "skipped": skipped}


@bound
def update_task(db: SqliteDatabase, task_id: int, **fields: Any) -> None:
    """Update one or more fields on a task.

    A flexible PATCH operation for common task modifications. Silently ignores
    any fields not in the allowed set. Updates the modified timestamp.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task to update.
        **fields: Keyword arguments for fields to update. Only these are allowed:
                  - title (str): Task name.
                  - description (str): Task description.
                  - assigned_to (str | None): Assign to an agent or None.
                  - status (str): Must be in TASK_STATUSES.
                  - feature (str | None): Feature name (created if new), or
                    None/"" to take the task out of its feature.
                  - tags (str | None): Comma-separated tags for filtering.
                  - worktree_path (str | None): Path to the task's git
                    worktree, or None once it's been removed.

    Raises:
        ValueError: If status is provided and not in TASK_STATUSES.

    Examples:
        >>> update_task(db, 42, title="New title", status="in_progress")
        >>> update_task(db, 42, assigned_to="alice")  # assign to alice
        >>> update_task(db, 42, assigned_to=None)  # unassign
    """
    allowed = {"title", "description", "assigned_to", "status", "feature", "tags", "worktree_path"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "status" in sets and sets["status"] not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {sets['status']}")
    if "assigned_to" in sets:
        sets["assigned_to"] = sets["assigned_to"] or None
    if "feature" in sets:
        sets["feature_id"] = ensure_feature(db, sets.pop("feature"))
    if "tags" in sets:
        sets["tags"] = _norm_tags(sets["tags"])
    sets["updated_at"] = now()
    Task.update(**sets).where(Task.id == task_id).execute()


@bound
def delete_task(db: SqliteDatabase, task_id: int) -> None:
    """Remove a task and all its associated data (dependencies, messages, events).

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task to delete.
    """
    Task.delete().where(Task.id == task_id).execute()


@bound
def list_tasks(
    db: SqliteDatabase, status: str | None = None, feature: str | None = None
) -> list[dict]:
    """Fetch all tasks, optionally filtered by status and/or feature.

    Args:
        db: SqliteDatabase instance for this project.
        status: Optional status filter (e.g., "todo", "in_progress", "done").
        feature: Optional feature name filter. Normalised before matching, so
                 callers can pass raw free text.

    Returns:
        list[dict]: Task records in insertion order, with keys: id, title,
                    description, assigned_to, status, feature_id, feature
                    (its name), tags, worktree_path, created_at, updated_at.
    """
    query = _tasks().order_by(Task.id)
    if status:
        query = query.where(Task.status == status)
    if feature:
        query = query.where(Feature.name == norm_feature_name(feature))
    return rows(query)


@bound
def filter_tasks(
    db: SqliteDatabase,
    search: str = "",
    status: list[str] | None = None,
    agent: list[str] | None = None,
    feature: list[str] | None = None,
    tags: list[str] | None = None,
    sort_by: str | None = None,
    sort_dir: str = "asc",
) -> list[dict]:
    """Filter and sort tasks by search query, status, assigned agent, feature, tags,
    and sort field.

    Args:
        db: SqliteDatabase instance for this project.
        search: Case-insensitive substring match against title or description.
        status: Optional list of statuses to include (default: all).
        agent: Optional list of assigned agent names to include. An empty
               string in the list also matches unassigned tasks.
        feature: Optional list of feature names to include. An empty string
                 in the list also matches tasks with no feature.
        tags: Optional list of tags to include. Tasks matching any of the tags
              are included. An empty string in the list also matches untagged
              tasks (Task.tags IS NULL).
        sort_by: One of "title", "assigned_to", "status", "created_at",
                 "updated_at". Defaults to updated_at desc, created_at desc.
                 Rows are always grouped by feature first (ungrouped last),
                 so this sorts within each feature group.
        sort_dir: "asc" or "desc" (only used with sort_by).

    Returns:
        list[dict]: Matching task records, pre-grouped by feature.
    """
    query = _tasks()

    search = search.strip()
    if search:
        query = query.where(Task.title.contains(search) | Task.description.contains(search))

    if status:
        query = query.where(Task.status.in_(status))

    if agent:
        if "" in agent:
            query = query.where(Task.assigned_to.in_(agent) | Task.assigned_to.is_null())
        else:
            query = query.where(Task.assigned_to.in_(agent))

    if feature:
        names = [norm_feature_name(f) for f in feature if f]
        if "" in feature:
            query = query.where(Feature.name.in_(names) | Task.feature_id.is_null())
        else:
            query = query.where(Feature.name.in_(names))

    if tags:
        named = [t for t in tags if t]
        conditions = [Task.tags.contains(tag) for tag in named]
        if "" in tags:
            # also match untagged tasks
            conditions.append(Task.tags.is_null())
        if conditions:
            query = query.where(functools.reduce(operator.or_, conditions))

    # ungrouped sorts last: "~~~" sorts after any lowercase feature name
    group = fn.COALESCE(Feature.name, "~~~")

    sort_fields = {
        "title": fn.LOWER(fn.COALESCE(Task.title, "")),
        "assigned_to": fn.COALESCE(Task.assigned_to, ""),
        "status": fn.COALESCE(Task.status, ""),
        "created_at": fn.COALESCE(Task.created_at, 0),
        "updated_at": fn.COALESCE(Task.updated_at, 0),
    }
    field = sort_fields.get(sort_by)
    if field is not None:
        query = query.order_by(group, field.desc() if sort_dir == "desc" else field.asc())
    else:
        query = query.order_by(
            group, fn.COALESCE(Task.updated_at, 0).desc(), fn.COALESCE(Task.created_at, 0).desc()
        )

    return rows(query)


@bound
def list_tags(db: SqliteDatabase) -> list[str]:
    """List all distinct tags used across tasks, sorted alphabetically.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[str]: Sorted list of unique tags (deduplicated).

    Examples:
        >>> list_tags(db)
        ["bug", "feature", "urgent"]
    """
    query = Task.select(Task.tags).where(Task.tags.is_null(False)).distinct()
    all_tags = set()
    for task in rows(query):
        if task.get("tags"):
            for tag in task["tags"].split(","):
                tag = tag.strip()
                if tag:
                    all_tags.add(tag)
    return sorted(list(all_tags))


@bound
def get_task(db: SqliteDatabase, task_id: int) -> dict | None:
    """Fetch a single task by ID.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task to retrieve.

    Returns:
        dict: Task record, or None if not found.
    """
    return row(_tasks().where(Task.id == task_id))


def _unmet_dependency_tasks():
    """Sub-query: ids of tasks with at least one dependency that is not done."""
    return (
        TaskDep.select(TaskDep.task)
        .join(Task, on=(TaskDep.depends_on == Task.id))
        .where(Task.status != "done")
    )


def _claimable(agent_name: str) -> Task | None:
    """Oldest 'ready' task for this agent whose dependencies are all done."""
    return (
        Task.select()
        .where(
            (Task.assigned_to == agent_name)
            & (Task.status == "ready")
            & (Task.id.not_in(_unmet_dependency_tasks()))
        )
        .order_by(Task.id)
        .first()
    )


@bound
def claim_task(db: SqliteDatabase, agent_name: str) -> dict | None:
    """Atomically take the oldest runnable 'ready' task for this agent, or None.

    Runnable means every task it depends on is done - so a dependency waiting
    for approval (or blocked, or simply not finished) holds this one back,
    while tasks that depend on none of that keep flowing.

    This operation is atomic: if the task is claimed by another daemon between
    the select and the update, None is returned instead of race-claiming it.
    Claimed tasks move to "in_progress" status.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Agent to claim a task for.

    Returns:
        dict: Task record moved to "in_progress", or None if no runnable
              task is available or another daemon won the race.

    Examples:
        >>> task = claim_task(db, "claude-opus-worker-1")
        >>> if task:
        ...     print(f"Claimed task {task['id']}: {task['title']}")
        ... else:
        ...     print("No runnable tasks")
    """
    # Idle polls find nothing, and finding nothing needs no write lock: look
    # first without a transaction. This read only skips work, it is not
    # trusted - the select is repeated under the lock below.
    if _claimable(agent_name) is None:
        return None
    # IMMEDIATE takes the write lock up front: a deferred transaction that
    # reads, then upgrades to a write, fails at once with "database is locked"
    # when another process wrote in between - busy_timeout does not cover it.
    with db.atomic("IMMEDIATE"):
        candidate = _claimable(agent_name)
        if candidate is None:
            return None
        # the `claim` transition of store/lifecycle.TRANSITIONS, done inline to keep the claim atomic
        taken = (
            Task.update(status="in_progress", updated_at=now())
            .where((Task.id == candidate.id) & (Task.status == "ready"))
            .execute()
        )
        if not taken:  # another daemon claimed it between the select and here
            return None
        return row(_tasks().where(Task.id == candidate.id))


@bound
def wait_for_task(db: SqliteDatabase, agent_name: str, poll_interval: float = 2) -> dict:
    """Block until a runnable task is assigned to this agent, then claim it.

    Polls at regular intervals, sleeping between attempts. This is the blocking
    variant of claim_task(): it always returns a task, never None, unless
    interrupted.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Agent to wait for tasks for.
        poll_interval: Seconds between claim attempts (default 2).

    Returns:
        dict: A claimed task record, guaranteed in "in_progress" status.

    Examples:
        >>> task = wait_for_task(db, "claude-opus-worker-1", poll_interval=1)
        >>> process_task(task)
    """
    while True:
        task = claim_task(db, agent_name)
        if task:
            return task
        time.sleep(poll_interval)
