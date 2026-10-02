"""Agents, tasks, dependencies, messages, docs and events - every read and
write of project state.

Plain functions over a peewee database handle, returning plain dicts: the ORM
stays inside this module, so the daemons, the web app and the MCP server keep
working against the same small vocabulary they always did.
"""

from __future__ import annotations

import functools
import operator
import os
import time
from pathlib import Path
from typing import Any

from peewee import JOIN, SQL, Case, SqliteDatabase, fn

from .db import EVENT_KINDS, HUMAN, TASK_STATUSES, now
from .models import (
    MODELS,
    Agent,
    Doc,
    Event,
    Message,
    Task,
    TaskDep,
    row,
    rows,
)


def bound(fn_):
    """Bind the models to the database this call is for.

    Several projects can be open in one process (the web app switches between
    them), so binding happens per call rather than once at import.
    """

    @functools.wraps(fn_)
    def wrapper(db: SqliteDatabase, *args: Any, **kwargs: Any):
        with db.bind_ctx(MODELS):
            return fn_(db, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------


@bound
def register_agent(db: SqliteDatabase, name: str, backend: str, role: str = "") -> None:
    """Register or update an agent in the registry.

    Creates a new agent record or updates an existing one. All agents start in
    "offline" status and receive heartbeat updates from their backend daemon.

    Args:
        db: SqliteDatabase instance for this project.
        name: Unique agent identifier (e.g., "claude-opus-worker-1").
        backend: The runtime backend (e.g., "anthropic", "openai").
        role: Optional role description (e.g., "code-review", "architect").
    """
    Agent.insert(name=name, backend=backend, role=role, status="offline").on_conflict(
        conflict_target=[Agent.name],
        update={Agent.backend: backend, Agent.role: role},
    ).execute()


@bound
def heartbeat(db: SqliteDatabase, name: str, status: str, task_id: int | None = None) -> None:
    """Update an agent's status and last-seen timestamp.

    Called periodically by the agent's daemon to indicate it is alive. The
    timestamp is used to detect stale agents and release their file claims.

    Args:
        db: SqliteDatabase instance for this project.
        name: Agent identifier.
        status: Current agent state (e.g., "idle", "working").
        task_id: Optional ID of the task currently being worked on.
    """
    Agent.update(status=status, current_task_id=task_id, last_heartbeat=now()).where(
        Agent.name == name
    ).execute()


@bound
def list_agents(db: SqliteDatabase) -> list[dict]:
    """List all registered agents, ordered by name.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: Agent records with keys: id, name, backend, role, status,
                    current_task_id, last_heartbeat, created_at.
    """
    return rows(Agent.select().order_by(Agent.name))


@bound
def get_agent(db: SqliteDatabase, name: str) -> dict | None:
    """Fetch a single agent record by name.

    Args:
        db: SqliteDatabase instance for this project.
        name: Agent identifier.

    Returns:
        dict: Agent record, or None if not found.
    """
    return row(Agent.select().where(Agent.name == name))


@bound
def delete_agent(db: SqliteDatabase, name: str) -> int:
    """Forget an agent. Its tasks go back to unassigned rather than vanishing;
    its messages stay, so the thread and the cost ledger keep their history."""
    freed = (
        Task.update(assigned_to=None, updated_at=now()).where(Task.assigned_to == name).execute()
    )
    Agent.delete().where(Agent.name == name).execute()
    return freed


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


def _norm_feature(value: str | None) -> str | None:
    """Free text in, a stable group key out. None means ungrouped."""
    return ((value or "").strip().lower())[:40] or None


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

    Creates a new "todo" task. If assigned_to is given, the task is placed in
    that agent's queue; otherwise it waits unassigned.

    Args:
        db: SqliteDatabase instance for this project.
        title: Short task name.
        description: Optional longer explanation of what to do.
        assigned_to: Optional agent name to assign this task to.
        feature: Optional free-text feature group this task belongs to (e.g.
                 "search"). Normalised via _norm_feature; empty/None means
                 ungrouped.
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
        feature=_norm_feature(feature),
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
                  - feature (str | None): Free-text feature group, or None
                    to ungroup. Normalised via _norm_feature.
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
        sets["feature"] = _norm_feature(sets["feature"])
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
        feature: Optional feature filter. Normalised via _norm_feature before
                 matching, so callers can pass raw free text.

    Returns:
        list[dict]: Task records in insertion order, with keys: id, title,
                    description, assigned_to, status, feature, created_at,
                    updated_at.
    """
    query = Task.select().order_by(Task.id)
    if status:
        query = query.where(Task.status == status)
    if feature:
        query = query.where(Task.feature == _norm_feature(feature))
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
        feature: Optional list of feature groups to include. An empty string
                 in the list also matches ungrouped tasks (Task.feature IS NULL).
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
    query = Task.select()

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
        if "" in feature:
            query = query.where(Task.feature.in_(feature) | Task.feature.is_null())
        else:
            query = query.where(Task.feature.in_(feature))

    if tags:
        named = [t for t in tags if t]
        conditions = [Task.tags.contains(tag) for tag in named]
        if "" in tags:
            # also match untagged tasks
            conditions.append(Task.tags.is_null())
        if conditions:
            query = query.where(functools.reduce(operator.or_, conditions))

    # ungrouped sorts last: "~~~" sorts after any lowercase feature name
    group = fn.COALESCE(Task.feature, "~~~")

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
def list_features(db: SqliteDatabase) -> list[dict]:
    """List distinct feature groups with per-feature task totals, for the
    filter dropdown and the group headings on the task list.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: One row per distinct feature, each with keys: feature,
                    total, done. Ordered by feature name, with the ungrouped
                    bucket (feature=None) last.

    Examples:
        >>> list_features(db)
        [{"feature": "search", "total": 4, "done": 2}, {"feature": None, "total": 1, "done": 0}]
    """
    query = (
        Task.select(
            Task.feature,
            fn.COUNT(Task.id).alias("total"),
            fn.SUM(Case(None, [(Task.status == "done", 1)], 0)).alias("done"),
        )
        .group_by(Task.feature)
        .order_by(fn.COALESCE(Task.feature, "~~~"))
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
    return row(Task.select().where(Task.id == task_id))


def _unmet_dependency_tasks():
    """Sub-query: ids of tasks with at least one dependency that is not done."""
    return (
        TaskDep.select(TaskDep.task)
        .join(Task, on=(TaskDep.depends_on == Task.id))
        .where(Task.status != "done")
    )


@bound
def claim_task(db: SqliteDatabase, agent_name: str) -> dict | None:
    """Atomically take the oldest runnable 'todo' task for this agent, or None.

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
    with db.atomic():
        candidate = (
            Task.select()
            .where(
                (Task.assigned_to == agent_name)
                & (Task.status == "todo")
                & (Task.id.not_in(_unmet_dependency_tasks()))
            )
            .order_by(Task.id)
            .first()
        )
        if candidate is None:
            return None
        taken = (
            Task.update(status="in_progress", updated_at=now())
            .where((Task.id == candidate.id) & (Task.status == "todo"))
            .execute()
        )
        if not taken:  # another daemon claimed it between the select and here
            return None
        return row(Task.select().where(Task.id == candidate.id))


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


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------


def _reaches(db: SqliteDatabase, start: int, target: int) -> bool:
    """Does `start` reach `target` by following dependency edges?

    A recursive CTE walks the graph in SQL rather than pulling every edge in
    the project into Python to BFS over.
    """
    sql = """
        WITH RECURSIVE reachable(node) AS (
            SELECT depends_on FROM task_deps WHERE task_id = ?
            UNION ALL
            SELECT d.depends_on FROM task_deps d
            JOIN reachable r ON d.task_id = r.node
        )
        SELECT 1 FROM reachable WHERE node = ? LIMIT 1
    """
    return db.execute_sql(sql, (start, target)).fetchone() is not None


@bound
def add_dependency(db: SqliteDatabase, task_id: int, depends_on: int) -> None:
    """Make `task_id` wait for `depends_on`. Refuses self-loops and cycles.

    Enforces a strict acyclic dependency graph: a task cannot wait on itself,
    and adding a dependency that would create a cycle is rejected. Duplicate
    dependencies are silently ignored (idempotent operation).

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task that will wait.
        depends_on: Task that must finish first.

    Raises:
        ValueError: If task_id == depends_on (self-loop).
        ValueError: If depends_on already transitively depends on task_id (cycle).

    Examples:
        >>> add_dependency(db, 10, 5)  # task 10 waits for task 5
        >>> add_dependency(db, 10, 5)  # idempotent; no error
        >>> add_dependency(db, 5, 10)  # raises ValueError: cycle
    """
    if task_id == depends_on:
        raise ValueError("a task cannot depend on itself")
    if _reaches(db, depends_on, task_id):
        raise ValueError(f"task {depends_on} already depends on task {task_id}")
    TaskDep.insert(task=task_id, depends_on=depends_on).on_conflict_ignore().execute()


@bound
def remove_dependency(db: SqliteDatabase, task_id: int, depends_on: int) -> None:
    """Remove a dependency edge.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task that was waiting.
        depends_on: Task that is no longer a prerequisite.
    """
    TaskDep.delete().where(
        (TaskDep.task == task_id) & (TaskDep.depends_on == depends_on)
    ).execute()


@bound
def task_dependencies(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch the tasks that this one depends on, as full task records.

    These are the prerequisites: all must reach "done" status before this
    task becomes runnable.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query.

    Returns:
        list[dict]: Task records this task depends on, in insertion order.
    """
    return rows(
        Task.select()
        .join(TaskDep, on=(TaskDep.depends_on == Task.id))
        .where(TaskDep.task == task_id)
        .order_by(Task.id)
    )


@bound
def task_dependents(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch the tasks that depend on this one (the reverse direction).

    These tasks are blocked until this one reaches "done" status.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query.

    Returns:
        list[dict]: Task records that depend on this task, in insertion order.
    """
    return rows(
        Task.select()
        .join(TaskDep, on=(TaskDep.task == Task.id))
        .where(TaskDep.depends_on == task_id)
        .order_by(Task.id)
    )


@bound
def blocking_dependencies(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch the dependencies that are blocking this task from running.

    Returns only the dependencies that have not yet reached "done" status.
    An empty list means the task is runnable (all prerequisites met).

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query.

    Returns:
        list[dict]: Task records that are dependencies and not yet done.

    Examples:
        >>> blocking = blocking_dependencies(db, 42)
        >>> if blocking:
        ...     print(f"Task 42 is blocked by {len(blocking)} task(s)")
        ...     for dep in blocking:
        ...         print(f"  - {dep['id']}: {dep['title']} ({dep['status']})")
    """
    return [d for d in task_dependencies(db, task_id) if d["status"] != "done"]


@bound
def blocking_map(db: SqliteDatabase) -> dict[int, list[dict]]:
    """Compute all tasks' blocking dependencies in one efficient query.

    For UI rendering and status summaries, this is more efficient than
    calling blocking_dependencies() for each task separately. Returns a
    mapping from task ID to its list of unfinished dependencies.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        dict[int, list[dict]]: Maps task_id -> list of unfinished dependency
                               records (id, title, status). Tasks with no
                               blocking dependencies are omitted.

    Examples:
        >>> blocking_map = blocking_map(db)
        >>> if 42 in blocking_map:
        ...     print(f"Task 42 blocked by: {blocking_map[42]}")
    """
    query = (
        TaskDep.select(
            TaskDep.task.alias("task_id"), Task.id, Task.title, Task.status
        )
        .join(Task, on=(TaskDep.depends_on == Task.id))
        .where(Task.status != "done")
        .order_by(Task.id)
    )
    blocking: dict[int, list[dict]] = {}
    for record in rows(query):
        blocking.setdefault(record["task_id"], []).append(record)
    return blocking


# --------------------------------------------------------------------------
# messages (audit log + cost ledger)
# --------------------------------------------------------------------------


@bound
def send_message(
    db: SqliteDatabase,
    sender: str,
    recipient: str,
    task_id: int | None,
    msg_type: str,
    payload: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    tool_rounds: int = 0,
    cost_usd: float = 0.0,
) -> int:
    """Log a message and record its cost.

    Messages are the audit trail and cost ledger: every agent communication is
    tracked here. Messages may be unread until the recipient's next invocation,
    when get_inbox() marks them as read.

    Args:
        db: SqliteDatabase instance for this project.
        sender: Agent or "human" sending the message.
        recipient: Agent or "human" receiving the message.
        task_id: Associated task (optional).
        msg_type: Message classification (e.g., "request", "result", "question").
        payload: Message content (may be JSON, markdown, or plain text).
        input_tokens: Fresh input tokens, charged at full price.
        output_tokens: Tokens generated by the LLM output.
        cache_read_tokens: Input served from cache, at roughly a tenth the price.
        cache_write_tokens: Input written to cache, at roughly 1.25x the price.
        tool_rounds: API round-trips in this turn.
        cost_usd: Direct cost in USD (if applicable).

    Returns:
        int: New message ID.

    Examples:
        >>> msg_id = send_message(db, "claude-worker", "human", 42, "result",
        ...                        "Task completed", input_tokens=100, output_tokens=50)
    """
    message = Message.create(
        ts=now(),
        sender=sender,
        recipient=recipient,
        task_id=task_id,
        msg_type=msg_type,
        payload=payload,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        tool_rounds=tool_rounds,
        cost_usd=cost_usd,
    )
    return int(message.id)


def reply(
    db: SqliteDatabase,
    agent_name: str,
    task_id: int | None,
    payload: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    tool_rounds: int = 0,
    cost_usd: float = 0.0,
    status: str = "done",
) -> int:
    """Log a task result back to the human and update the task status.

    Sends a message to the human and atomically updates the associated task's
    status. This is the primary way an agent signals completion or a state change
    back to the coordinator.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent sending the result.
        task_id: ID of the task being reported on (optional).
        payload: Result summary or detailed report.
        input_tokens: Fresh input tokens consumed by this run.
        output_tokens: Tokens generated by this run.
        cache_read_tokens: Input served from cache this run.
        cache_write_tokens: Input written to cache this run.
        tool_rounds: API round-trips in this run.
        cost_usd: Total cost in USD.
        status: Status to move the task to (default "done"). Must be in TASK_STATUSES.

    Returns:
        int: Message ID of the logged result.

    Examples:
        >>> reply(db, "claude-worker", 42, "Task completed successfully",
        ...       input_tokens=150, output_tokens=75, cost_usd=0.005)
    """
    msg_id = send_message(
        db, agent_name, HUMAN, task_id, "result", payload,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
        tool_rounds=tool_rounds, cost_usd=cost_usd,
    )
    if task_id is not None:
        update_task_status(db, task_id, status)
    return msg_id


def reply_to_task(db: SqliteDatabase, task_id: int, payload: str, sender: str = HUMAN) -> int:
    """Send a human's reply on a task's thread, and reopen it if it was resting.

    This is the human side of the async back-and-forth `reply()` and
    `send_message()` describe for agents: a message alone is invisible until
    the next time the assigned agent's daemon claims a task, and a closed or
    holding task (done, needs_approval, blocked, ready_to_merge) never gets
    reclaimed on its own (see claim_task - it only ever picks up "todo"). So
    a plain note left on a finished task would just sit there unread forever.

    Requeuing to "todo" costs nothing extra: compose_task_prompt() rebuilds
    the next run's prompt from the task's own message thread, so the agent
    sees its prior work plus this reply without re-deriving anything from the
    project itself. A task already "todo" or "in_progress" is left alone -
    the agent is already working it or about to.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to reply on.
        payload: The reply text.
        sender: Who is replying (default "human").

    Returns:
        int: New message ID.

    Raises:
        ValueError: If the task does not exist.
    """
    task = get_task(db, task_id)
    if not task:
        raise ValueError(f"task {task_id} not found")
    recipient = task["assigned_to"] or HUMAN
    msg_id = send_message(db, sender, recipient, task_id, "note", payload)
    if task["assigned_to"] and task["status"] not in ("todo", "in_progress"):
        update_task_status(db, task_id, "todo")
    return msg_id


@bound
def get_inbox(db: SqliteDatabase, agent_name: str, mark_read: bool = True) -> list[dict]:
    """Fetch unread messages addressed to this agent, oldest first.

    On each invocation, agents call get_inbox() to learn what the human or other
    agents are asking for. By default, fetched messages are marked as read;
    set mark_read=False to leave them unread for later processing.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Agent to fetch messages for.
        mark_read: If True (default), mark fetched messages as read.

    Returns:
        list[dict]: Unread message records, chronologically ordered. Each has:
                    id, ts, sender, recipient, task_id, msg_type, payload,
                    input_tokens, output_tokens, cost_usd, read_at.

    Examples:
        >>> inbox = get_inbox(db, "claude-worker-1")
        >>> for msg in inbox:
        ...     print(f"{msg['sender']}: {msg['payload']}")
    """
    unread = rows(
        Message.select()
        .where((Message.recipient == agent_name) & Message.read_at.is_null())
        .order_by(Message.ts)
    )
    if unread and mark_read:
        Message.update(read_at=now()).where(
            Message.id.in_([m["id"] for m in unread])
        ).execute()
    return unread


@bound
def mark_messages_read(db: SqliteDatabase, message_ids: list[int]) -> None:
    """Mark specific messages as read.

    Used to mark inbox messages as read after a successful run, ensuring
    messages are not lost if a run fails (see task R4).

    Args:
        db: SqliteDatabase instance for this project.
        message_ids: List of message IDs to mark as read.
    """
    if message_ids:
        Message.update(read_at=now()).where(
            Message.id.in_(message_ids)
        ).execute()


@bound
def task_messages(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch all messages related to a task, chronologically.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query messages for.

    Returns:
        list[dict]: Message records in timestamp order.
    """
    return rows(Message.select().where(Message.task_id == task_id).order_by(Message.ts))


@bound
def token_usage_by_agent(db: SqliteDatabase) -> list[dict]:
    """Aggregate token usage and cost by agent, highest cost first.

    Summarizes all messages sent by agents (excluding human messages) to
    compute per-agent token consumption and total cost. Useful for billing
    and performance analysis.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: Aggregated stats per agent, ordered by cost descending.
                    Each record has: agent, turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd.
                    Rows predating migration 003 report 0 cache tokens and carry
                    cache reads inside input_tokens.

    Examples:
        >>> usage = token_usage_by_agent(db)
        >>> for stat in usage:
        ...     print(f"{stat['agent']}: {stat['cost_usd']:.4f} USD "
        ...           f"({stat['input_tokens']} in, {stat['output_tokens']} out)")
    """
    query = (
        Message.select(
            Message.sender.alias("agent"),
            fn.COUNT(Message.id).alias("turns"),
            fn.COALESCE(fn.SUM(Message.input_tokens), 0).alias("input_tokens"),
            fn.COALESCE(fn.SUM(Message.output_tokens), 0).alias("output_tokens"),
            fn.COALESCE(fn.SUM(Message.cache_read_tokens), 0).alias("cache_read_tokens"),
            fn.COALESCE(fn.SUM(Message.cache_write_tokens), 0).alias("cache_write_tokens"),
            fn.COALESCE(fn.SUM(Message.tool_rounds), 0).alias("tool_rounds"),
            fn.COALESCE(fn.SUM(Message.cost_usd), 0.0).alias("cost_usd"),
        )
        .where(Message.sender != HUMAN)
        .group_by(Message.sender)
        .order_by(SQL("cost_usd DESC"))
    )
    return rows(query)


@bound
def task_status_counts(db: SqliteDatabase) -> list[dict]:
    """Task counts grouped by status, for the stats dashboard."""
    query = Task.select(Task.status, fn.COUNT(Task.id).alias("count")).group_by(Task.status)
    return rows(query)


@bound
def task_counts_by_agent(db: SqliteDatabase) -> list[dict]:
    """Per-agent total and completed task counts, for the stats dashboard."""
    query = (
        Task.select(
            Task.assigned_to,
            fn.COUNT(Task.id).alias("total_tasks"),
            fn.SUM(Task.status == "done").alias("completed_tasks"),
        )
        .where(Task.assigned_to.is_null(False))
        .group_by(Task.assigned_to)
    )
    return rows(query)


@bound
def longest_tasks(db: SqliteDatabase, limit: int = 10) -> list[dict]:
    """The `limit` longest-running done tasks, by wall-clock duration in seconds."""
    duration = Task.updated_at - Task.created_at
    query = (
        Task.select(Task.id, Task.title, duration.alias("duration"))
        .where(Task.status == "done")
        .order_by(SQL("duration DESC"))
        .limit(limit)
    )
    return rows(query)


@bound
def avg_task_duration(db: SqliteDatabase) -> float:
    """Average wall-clock duration of done tasks, in seconds."""
    duration = Task.updated_at - Task.created_at
    query = Task.select(fn.COALESCE(fn.AVG(duration), 0.0).alias("avg")).where(Task.status == "done")
    return query.dicts().get()["avg"]


@bound
def cost_by_task(db: SqliteDatabase, limit: int = 10) -> list[dict]:
    """Total cost per task, highest first, with the task title joined in.

    A left join, because a message can outlive the task it belonged to -
    `title` comes back None for those and the caller decides how to label them.
    """
    query = (
        Message.select(
            Message.task_id,
            Task.title,
            fn.COALESCE(fn.SUM(Message.cost_usd), 0.0).alias("cost"),
        )
        .join(Task, JOIN.LEFT_OUTER, on=(Message.task_id == Task.id))
        .where(Message.task_id.is_null(False))
        .group_by(Message.task_id)
        .order_by(SQL("cost DESC"))
        .limit(limit)
    )
    return rows(query)


# --------------------------------------------------------------------------
# events: the agent monologue, kept for audit
# --------------------------------------------------------------------------


@bound
def log_event(
    db: SqliteDatabase,
    agent: str,
    task_id: int | None,
    run_id: str | None,
    kind: str,
    body: str,
    label: str | None = None,
) -> int:
    """Log an agent's activity event to the audit trail.

    Events record the agent's internal monologue - thinking, tool calls, results,
    errors - for audit, debugging, and cost tracking purposes. This is separate
    from messages (which are agent-to-agent communication).

    Args:
        db: SqliteDatabase instance for this project.
        agent: Agent name that produced the event.
        task_id: Associated task (optional).
        run_id: Invocation ID (optional, groups events from one call).
        kind: Event classification; must be in EVENT_KINDS.
        body: Event details (may be JSON, text, or structured).
        label: Human-readable annotation (e.g., tool name).

    Returns:
        int: New event ID.

    Raises:
        ValueError: If kind is not in EVENT_KINDS.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    event = Event.create(
        ts=now(), agent=agent, task_id=task_id, run_id=run_id, kind=kind, label=label, body=body
    )
    return int(event.id)


def _as_tuple(value: Any) -> tuple:
    """Accept a single kind or an iterable of them, and normalise to a tuple.

    `kinds="system"` is the obvious thing to write and would otherwise iterate
    into six one-letter kinds that match nothing.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _filter_events(query, kinds: Any = None, exclude_kinds: Any = None, agent: str | None = None):
    """Narrow an event query in SQL.

    The filtering belongs in the query and not in the caller: a feed that wants
    the newest 25 events of substance would otherwise have to over-fetch an
    unknown multiple of 25 and slice, because telemetry kinds outnumber the rest.
    """
    if kinds is not None:
        query = query.where(Event.kind.in_(_as_tuple(kinds)))
    excluded = _as_tuple(exclude_kinds)
    if excluded:
        query = query.where(Event.kind.not_in(excluded))
    if agent:
        query = query.where(Event.agent == agent)
    return query


@bound
def task_events(
    db: SqliteDatabase,
    task_id: int,
    kinds: Any = None,
    exclude_kinds: Any = None,
    agent: str | None = None,
) -> list[dict]:
    """Fetch all events associated with a task, in order.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query events for.
        kinds: Optional kind, or iterable of kinds, to restrict to.
        exclude_kinds: Optional kind, or iterable of kinds, to leave out.
        agent: Optional agent name to filter by.

    Returns:
        list[dict]: Event records chronologically ordered.

    Note:
        With no filters this returns the whole trail, system telemetry included.
        Hiding noisy kinds is a display decision, so it is the feed that passes
        `exclude_kinds`, not this function that assumes it.
    """
    query = Event.select().where(Event.task_id == task_id).order_by(Event.id)
    return rows(_filter_events(query, kinds, exclude_kinds, agent))


@bound
def run_events(db: SqliteDatabase, run_id: str) -> list[dict]:
    """Fetch all events from a single agent invocation, in order.

    Args:
        db: SqliteDatabase instance for this project.
        run_id: Invocation ID to query events for.

    Returns:
        list[dict]: Event records from that invocation, chronologically ordered.
    """
    return rows(Event.select().where(Event.run_id == run_id).order_by(Event.id))


@bound
def get_event(db: SqliteDatabase, event_id: int) -> dict | None:
    """Fetch a single event by ID.

    Used to lazily load one row's expanded detail (the fleet tail renders
    only the one-line summary up front and fetches the full payload on
    expand, so a 25-row poll never ships bodies nobody opened).

    Args:
        db: SqliteDatabase instance for this project.
        event_id: ID of the event to retrieve.

    Returns:
        dict: Event record, or None if not found.
    """
    return row(Event.select().where(Event.id == event_id))


@bound
def recent_events(
    db: SqliteDatabase,
    agent: str | None = None,
    limit: int = 50,
    kinds: Any = None,
    exclude_kinds: Any = None,
) -> list[dict]:
    """Fetch the most recent events, newest first, optionally filtered.

    Useful for monitoring: see what just happened across the fleet or from a
    specific agent.

    The filters are applied in SQL, so `limit` counts events the caller wanted.
    A tail of the newest 25 rows is otherwise mostly `kind="system"` telemetry -
    the heartbeat kinds outnumber the substantive ones - and excluding them
    after the fetch would just return a short list.

    Args:
        db: SqliteDatabase instance for this project.
        agent: Optional agent name to filter by.
        limit: Maximum number of events to return (default 50).
        kinds: Optional kind, or iterable of kinds, to restrict to.
        exclude_kinds: Optional kind, or iterable of kinds, to leave out.

    Returns:
        list[dict]: Recent event records, newest first.

    Examples:
        >>> recent = recent_events(db, agent="claude-worker-1", limit=10)
        >>> for event in recent:
        ...     print(f"[{event['ts']}] {event['kind']}: {event['label']}")
        >>> feed = recent_events(db, limit=25, exclude_kinds=("system",))
    """
    query = Event.select().order_by(Event.id.desc()).limit(limit)
    return rows(_filter_events(query, kinds, exclude_kinds, agent))


@bound
def recent_runs(db: SqliteDatabase, limit: int = 20) -> list[dict]:
    """Fetch the most recent agent invocations, newest first, one row per run.

    `events.run_id` groups the monologue of a single invocation; this is the
    index over those groups, so a UI can offer "show me that run" without
    reading every event to discover which runs exist.

    Args:
        db: SqliteDatabase instance for this project.
        limit: Maximum number of runs to return (default 20).

    Returns:
        list[dict]: One row per run, newest first, with keys `run_id`, `agent`,
        `task_id`, `first_ts`, `last_ts`, `event_count` and `result` - the
        label of the run's terminal `result` event, carrying its final status
        and cost. `result` is None for a run still in flight, which still
        appears: an unfinished run is exactly the one a human wants to watch.
    """
    # the terminal label comes from a join against the (tiny) set of result
    # events - one row per run - rather than a query per run. It is constant
    # within the group, so MAX() over it is an identity, not a choice.
    terminal = Event.alias()
    last_result = (
        terminal.select(terminal.run_id.alias("run_id"), fn.MAX(terminal.id).alias("event_id"))
        .where((terminal.kind == "result") & terminal.run_id.is_null(False))
        .group_by(terminal.run_id)
        .alias("last_result")
    )
    label_of = Event.alias()
    query = (
        Event.select(
            Event.run_id,
            fn.MAX(Event.agent).alias("agent"),
            fn.MAX(Event.task_id).alias("task_id"),
            fn.MIN(Event.ts).alias("first_ts"),
            fn.MAX(Event.ts).alias("last_ts"),
            fn.COUNT(Event.id).alias("event_count"),
            fn.MAX(label_of.label).alias("result"),
        )
        .join(last_result, JOIN.LEFT_OUTER, on=(last_result.c.run_id == Event.run_id))
        .join(label_of, JOIN.LEFT_OUTER, on=(label_of.id == last_result.c.event_id))
        .where(Event.run_id.is_null(False))
        .group_by(Event.run_id)
        .order_by(fn.MAX(Event.id).desc())
        .limit(limit)
    )
    return rows(query)


# --------------------------------------------------------------------------
# docs (shared project knowledge)
# --------------------------------------------------------------------------


_UNSET = object()  # docs_set's task_id sentinel: "leave the link as it is"


@bound
def docs_get(db: SqliteDatabase, key: str, task_id: int | None = None) -> str | None:
    """Fetch a shared project knowledge document.

    Agents and humans write documentation here that persists across invocations.
    Used for storing project context, architecture, decisions, etc.

    Args:
        db: SqliteDatabase instance for this project.
        key: Document identifier (e.g., "architecture", "conventions").
        task_id: If given, the doc must be linked to this task - a doc with
            no link, or linked to a different task, returns None instead of
            its content. Omit for project-wide docs (e.g. "architecture"),
            or when the key alone is enough to identify the doc.

    Returns:
        str: Document content, or None if not found or not linked to task_id.

    Examples:
        >>> arch = docs_get(db, "architecture")
        >>> if arch:
        ...     print("Architecture notes:", arch)
        >>> plan = docs_get(db, "task_42_planning-agent_context", task_id=42)
    """
    doc = row(Doc.select(Doc.content, Doc.task_id).where(Doc.key == key))
    if not doc:
        return None
    if task_id is not None and doc["task_id"] != task_id:
        return None
    return doc["content"]


@bound
def docs_set(
    db: SqliteDatabase,
    key: str,
    content: str,
    updated_by: str = HUMAN,
    task_id: int | None = _UNSET,
) -> None:
    """Write or update a shared project knowledge document.

    Creates a new document or replaces an existing one. Tracks who updated
    the document and when.

    Args:
        db: SqliteDatabase instance for this project.
        key: Document identifier.
        content: Document text (markdown, JSON, or any format).
        updated_by: Who is updating this (default "human").
        task_id: Task this doc belongs to (e.g. a plan or handover report);
            the doc is deleted when that task is. Omit to leave an existing
            doc's link untouched, or pass None to explicitly clear it - a
            bare positional call never touches the link.

    Examples:
        >>> docs_set(db, "conventions", "# Code Conventions\\n\\n- Use snake_case...",
        ...          updated_by="claude-reviewer")
        >>> docs_set(db, "task_42_context", "# Plan for task 42...", "planning-agent", task_id=42)
    """
    # Use insert().on_conflict() instead of .replace() to ensure the UPDATE trigger
    # fires on the FTS5 index. INSERT OR REPLACE only fires the DELETE trigger if
    # PRAGMA recursive_triggers is ON (it defaults OFF), leaving orphaned index entries.
    # See migration 005's docs_fts_update for the trigger that this must invoke.
    now_val = now()
    fields = {"key": key, "content": content, "updated_by": updated_by, "updated_at": now_val}
    update = {Doc.content: content, Doc.updated_by: updated_by, Doc.updated_at: now_val}
    if task_id is not _UNSET:
        fields["task_id"] = task_id
        update[Doc.task_id] = task_id
    Doc.insert(**fields).on_conflict(conflict_target=[Doc.key], update=update).execute()


@bound
def docs_list(db: SqliteDatabase, task_id: int | None = None) -> list[dict]:
    """List shared project documents, ordered by key.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: If given, only docs linked to this task.

    Returns:
        list[dict]: Document records with keys: key, content, updated_by,
                    updated_at, task_id.
    """
    query = Doc.select().order_by(Doc.key)
    if task_id is not None:
        query = query.where(Doc.task_id == task_id)
    return rows(query)


def normalize_path(path: str, project_dir: str | os.PathLike | None = None) -> str:
    """Normalize a file path to a consistent project-relative form.

    Two agents that name the same file differently should still collide in
    claims. This function collapses ".." and "." and resolves to a canonical
    relative path (relative to project_dir if given).

    Args:
        path: File or directory path (absolute or relative).
        project_dir: Project root for computing relative paths.

    Returns:
        str: Normalized path, typically relative to project_dir.

    Examples:
        >>> normalize_path("./src/foo.py", "/home/user/proj")
        "src/foo.py"
        >>> normalize_path("src/../src/foo.py", "/home/user/proj")
        "src/foo.py"
    """
    # collapse "." and ".." textually first, so two agents naming the same
    # file differently still land on the same key
    candidate = Path(os.path.normpath(str(path)))
    if project_dir:
        base = Path(project_dir).resolve()
        absolute = candidate if candidate.is_absolute() else base / candidate
        try:
            candidate = absolute.resolve().relative_to(base)
        except ValueError:
            candidate = absolute.resolve()
    result = str(candidate)
    # An absolute result only happens when project_dir was given and the path
    # escaped it (the relative_to() above failed) - callers such as
    # guardrails.check_outside_project rely on os.path.isabs() of this return
    # value to detect that. Stripping "/" indiscriminately would erase the
    # one signal that carries, so only trim the leading slash of paths that
    # were never anchored to a project in the first place.
    if candidate.is_absolute() and project_dir:
        return result.rstrip("/") or "/"
    return result.strip("/") or "."


# --------------------------------------------------------------------------
# cost anomaly detection
#
# This used to compare token totals, which was misleading: the daemon folded
# cache reads into input_tokens, and cache reads are priced around a tenth of
# fresh input. A turn could triple its token count while costing less. Cost is
# the only figure that compares like with like, and the backend reports it
# directly, so that is what we watch.
# --------------------------------------------------------------------------


@bound
def calculate_rolling_cost_average(
    db: SqliteDatabase, window_size: int = 20, exclude_task_id: int | None = None
) -> float:
    """Calculate the rolling average cost per task, in USD.

    Looks at recent result messages - the task completion records - and averages
    what they actually cost.

    Args:
        db: SqliteDatabase instance for this project.
        window_size: Number of recent tasks to average over (default 20).
        exclude_task_id: Optional task ID to exclude from calculation (typically
                        the current task being evaluated).

    Returns:
        float: Average USD per task. Returns 0 if no data available.

    Examples:
        >>> avg = calculate_rolling_cost_average(db, window_size=20)
        >>> print(f"Average cost per task: ${avg:.4f}")
    """
    query = Message.select(
        Message.task_id,
        fn.SUM(Message.cost_usd).alias("total_cost"),
    ).where(
        Message.msg_type == "result"
    ).group_by(Message.task_id).order_by(Message.ts.desc()).limit(window_size)

    if exclude_task_id is not None:
        query = query.where(Message.task_id != exclude_task_id)

    results = rows(query)
    if not results:
        return 0.0

    return sum(r.get("total_cost") or 0.0 for r in results) / len(results)


@bound
def check_cost_anomaly(
    db: SqliteDatabase,
    task_id: int,
    cost_usd: float,
    anomaly_threshold: float = 2.0,
    window_size: int = 20,
) -> dict:
    """Detect and log cost anomalies for a task.

    Compares a task's cost against the rolling average. If it exceeds the
    threshold (default 2x), logs a warning event visible in the activity feed.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task to check.
        cost_usd: What this task's run cost, as reported by the backend.
        anomaly_threshold: Multiple of rolling average to trigger alert (default 2.0).
        window_size: Number of recent tasks for rolling average (default 20).

    Returns:
        dict: Detection result with keys:
              - is_anomaly (bool): Whether an anomaly was detected.
              - cost_usd (float): Cost for this task.
              - rolling_average (float): Average cost per recent task.
              - multiplier (float): How many times the average this task cost.
              - event_id (int | None): ID of logged warning event, or None if no anomaly.

    Examples:
        >>> result = check_cost_anomaly(db, task_id=42, cost_usd=1.20)
        >>> if result["is_anomaly"]:
        ...     print(f"Anomaly: ${result['cost_usd']:.4f} ({result['multiplier']:.1f}x average)")
    """
    rolling_average = calculate_rolling_cost_average(
        db, window_size=window_size, exclude_task_id=task_id
    )

    result = {
        "is_anomaly": False,
        "cost_usd": cost_usd,
        "rolling_average": rolling_average,
        "multiplier": 0.0,
        "event_id": None,
    }

    # No anomaly if rolling average is 0 (not enough history)
    if rolling_average == 0:
        return result

    multiplier = cost_usd / rolling_average
    result["multiplier"] = multiplier

    if multiplier >= anomaly_threshold:
        result["is_anomaly"] = True
        task = get_task(db, task_id)
        task_title = task["title"] if task else f"Task #{task_id}"

        warning_message = (
            f"Task #{task_id} ({task_title}) cost ${cost_usd:.4f} "
            f"({multiplier:.1f}x average of ${rolling_average:.4f}) - "
            f"check its tool round-trip count and how much it read"
        )

        event_id = log_event(
            db,
            agent="system",
            task_id=task_id,
            run_id=None,
            kind="warning",
            body=warning_message,
            label="cost_anomaly",
        )
        result["event_id"] = event_id

    return result


# --------------------------------------------------------------------------
# full-text search (FTS5)
# --------------------------------------------------------------------------




def _parse_fts_snippet(snippet_text: str) -> dict:
    """Parse FTS5 snippet with markers into structured parts.

    FTS5's snippet() function returns text with start/end markers around
    matched content. This function splits the snippet into before/match/after
    parts so the template can render each escaped, preventing both injection
    and broken highlighting.

    Args:
        snippet_text: Snippet from FTS5 with <MARK> delimiters.

    Returns:
        dict with keys: before, match, after (all escaped strings).
    """
    if not snippet_text:
        return {"before": "", "match": "", "after": ""}

    # Split on the markers FTS5 used
    parts = snippet_text.split("<MARK>")
    if len(parts) < 2:
        # No match found (shouldn't happen, but handle it)
        return {"before": snippet_text, "match": "", "after": ""}

    before = parts[0]
    rest = "<MARK>".join(parts[1:])

    match_parts = rest.split("</MARK>")
    if len(match_parts) < 2:
        # Malformed, treat all as before
        return {"before": snippet_text, "match": "", "after": ""}

    match = match_parts[0]
    after = "</MARK>".join(match_parts[1:])

    return {
        "before": before,
        "match": match,
        "after": after,
    }


def _fts_search_table(
    db: SqliteDatabase,
    table: str,
    query: str,
    limit: int | None = None,
) -> list[dict]:
    """Search a single FTS5 table and return results with full context.

    Uses FTS5's snippet() and bm25() functions for highlighting and scoring.
    Returns all matching results (no limit), allowing the caller to handle
    pagination across multiple tables.

    Args:
        db: SqliteDatabase instance for this project.
        table: Table name ('docs', 'messages', 'tasks', 'events').
        query: FTS5 query string (supports AND, OR, NOT, "phrase").
        limit: Optional maximum results per table (for performance tuning).

    Returns:
        list[dict]: Result dicts with keys: table, id, title, snippet_before,
                    snippet_match, snippet_after, rank, metadata.
    """
    fts_table = f"{table}_fts"
    results = []

    # Map of table to (join_table, search_columns, text_column)
    table_specs = {
        "docs": {
            "join_table": "docs",
            "join_on": "d.rowid = f.rowid",
            "search_col": "content",
            "join_select": "d.key, d.updated_by, d.updated_at",
        },
        "messages": {
            "join_table": "messages",
            "join_on": "m.id = f.rowid",
            "search_col": "payload",
            "join_select": "m.sender, m.recipient, m.task_id, m.msg_type, m.ts, m.input_tokens, m.output_tokens, m.cache_read_tokens, m.cache_write_tokens, m.tool_rounds, m.cost_usd, m.read_at",
        },
        "tasks": {
            "join_table": "tasks",
            "join_on": "t.id = f.rowid",
            "search_col": "title",
            "join_select": "t.title, t.assigned_to, t.status, t.created_at, t.updated_at",
        },
        "events": {
            "join_table": "events",
            "join_on": "e.id = f.rowid",
            "search_col": "body",
            "join_select": "e.ts, e.agent, e.task_id, e.run_id, e.kind, e.label",
        },
    }

    if table not in table_specs:
        return results

    spec = table_specs[table]

    # Use FTS5's snippet() and bm25() functions
    # Note: FTS5 functions require the actual table name, not an alias, so we use fts_table directly
    fts_query = f"""
        SELECT f.rowid, bm25({fts_table}) as rank,
               snippet({fts_table}, -1, '<MARK>', '</MARK>', '…', 32) as snippet,
               {spec['join_select']}
        FROM {fts_table} f
        JOIN {spec['join_table']} {spec['join_table'][0]} ON {spec['join_on']}
        WHERE f.{fts_table} MATCH ?
        ORDER BY rank
    """
    if limit:
        fts_query += f" LIMIT {limit}"

    fts_results = db.execute_sql(fts_query, (query,)).fetchall()

    for row in fts_results:
        row_id = row[0]
        bm25_score = row[1]
        snippet_text = row[2]

        # Parse remaining columns based on table
        if table == "docs":
            key, updated_by, updated_at = row[3], row[4], row[5]
            title = key if key else f"Doc #{row_id}"
            metadata = {
                "key": key,
                "updated_by": updated_by,
                "updated_at": updated_at,
            }
        elif table == "messages":
            sender, recipient, task_id, msg_type, ts, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd, read_at = row[3:15]
            sender_name = sender if sender else "unknown"
            first_50 = (snippet_text or "")[:50]
            title = f"From {sender_name}: {first_50}"
            metadata = {
                "sender": sender,
                "recipient": recipient,
                "task_id": task_id,
                "msg_type": msg_type,
                "ts": ts,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "tool_rounds": tool_rounds,
                "cost_usd": cost_usd,
                "read_at": read_at,
            }
        elif table == "tasks":
            title_text, assigned_to, status, created_at, updated_at = row[3], row[4], row[5], row[6], row[7]
            title = title_text if title_text else f"Task #{row_id}"
            metadata = {
                "assigned_to": assigned_to,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        elif table == "events":
            ts, agent, task_id, run_id, kind, label = row[3:9]
            first_50 = (snippet_text or "")[:50]
            if task_id:
                title = f"Task #{task_id}: {first_50}"
            else:
                title = f"Event #{row_id}: {first_50}"
            metadata = {
                "ts": ts,
                "agent": agent,
                "task_id": task_id,
                "run_id": run_id,
                "kind": kind,
                "label": label,
            }

        snippet_parts = _parse_fts_snippet(snippet_text or "")

        results.append({
            "table": table,
            "id": row_id,
            "title": title,
            "snippet_before": snippet_parts["before"],
            "snippet_match": snippet_parts["match"],
            "snippet_after": snippet_parts["after"],
            "rank": bm25_score,
            "metadata": metadata,
        })

    return results


@bound
def full_text_search(
    db: SqliteDatabase,
    query: str,
    tables: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Search across FTS5 indexes for query terms.

    Main search function supporting FTS5 query syntax (AND, OR, NOT, "phrase").
    Searches multiple tables simultaneously, combines and ranks results by
    relevance (BM25 score).

    Args:
        db: SqliteDatabase instance for this project.
        query: FTS5 query string (e.g., "agent AND task", '"exact phrase"', "NOT archived").
        tables: Optional filter by table names (['docs', 'messages', 'tasks', 'events']).
                If None, searches all tables.
        limit: Maximum results to return (default 50).
        offset: Number of results to skip for pagination (default 0).

    Returns:
        list[dict]: Combined results from all searched tables, ranked by relevance.
                    Each dict has keys: table, id, title, snippet, rank, metadata.

    Raises:
        ValueError: If query is too short (less than 2 characters).

    Examples:
        >>> results = full_text_search(db, "agent AND task", tables=["tasks", "messages"])
        >>> for result in results:
        ...     print(f"{result['table']}: {result['title']} (rank: {result['rank']})")

        >>> # Search all tables
        >>> results = full_text_search(db, '"exact phrase"')

        >>> # Pagination
        >>> page1 = full_text_search(db, "query", limit=10, offset=0)
        >>> page2 = full_text_search(db, "query", limit=10, offset=10)
    """
    # Validate query length
    if len(query.strip()) < 2:
        raise ValueError("Query must be at least 2 characters")

    # Default to all tables if not specified
    search_tables = tables if tables else ["docs", "messages", "tasks", "events"]

    # Validate table names
    valid_tables = {"docs", "messages", "tasks", "events"}
    search_tables = [t for t in search_tables if t in valid_tables]
    if not search_tables:
        return []

    # Search each table and collect results
    # Use a larger per-table limit to ensure we get enough results after combining and ranking
    # This helps avoid edge cases where offset skips too many results
    per_table_limit = max(500, limit * len(search_tables))

    all_results = []
    for table in search_tables:
        table_results = _fts_search_table(db, table, query, limit=per_table_limit)
        all_results.extend(table_results)

    # Sort by rank ascending across all tables (negative scores, more negative = better match)
    all_results.sort(key=lambda x: x["rank"])

    # Normalize BM25 scores (negative, more negative = better match) to a 0-1
    # range for display: best match -> 1.0, worst -> 0.0.
    if all_results:
        best_rank = all_results[0]["rank"]
        worst_rank = all_results[-1]["rank"]
        rank_range = worst_rank - best_rank
        for result in all_results:
            if rank_range == 0:
                result["rank_normalized"] = 1.0
            else:
                result["rank_normalized"] = max(
                    0.0, min(1.0, (worst_rank - result["rank"]) / rank_range)
                )

    # Apply offset and limit across combined results
    end_idx = offset + limit
    return all_results[offset:end_idx]
