"""Agents, tasks, dependencies, messages, docs and events - every read and
write of project state.

Plain functions over a peewee database handle, returning plain dicts: the ORM
stays inside this module, so the daemons, the web app and the MCP server keep
working against the same small vocabulary they always did.
"""

from __future__ import annotations

import functools
import os
import time
from pathlib import Path
from typing import Any

from peewee import JOIN, SQL, SqliteDatabase, fn

from .db import EVENT_KINDS, HUMAN, TASK_STATUSES, now
from .models import (
    MODELS,
    Agent,
    Doc,
    Event,
    FileClaim,
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


@bound
def add_task(
    db: SqliteDatabase, title: str, description: str = "", assigned_to: str | None = None
) -> int:
    """Create a new task.

    Creates a new "todo" task. If assigned_to is given, the task is placed in
    that agent's queue; otherwise it waits unassigned.

    Args:
        db: SqliteDatabase instance for this project.
        title: Short task name.
        description: Optional longer explanation of what to do.
        assigned_to: Optional agent name to assign this task to.

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

    Raises:
        ValueError: If status is provided and not in TASK_STATUSES.

    Examples:
        >>> update_task(db, 42, title="New title", status="in_progress")
        >>> update_task(db, 42, assigned_to="alice")  # assign to alice
        >>> update_task(db, 42, assigned_to=None)  # unassign
    """
    allowed = {"title", "description", "assigned_to", "status"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    if "status" in sets and sets["status"] not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {sets['status']}")
    if "assigned_to" in sets:
        sets["assigned_to"] = sets["assigned_to"] or None
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
def list_tasks(db: SqliteDatabase, status: str | None = None) -> list[dict]:
    """Fetch all tasks, optionally filtered by status.

    Args:
        db: SqliteDatabase instance for this project.
        status: Optional status filter (e.g., "todo", "in_progress", "done").

    Returns:
        list[dict]: Task records in insertion order, with keys: id, title,
                    description, assigned_to, status, created_at, updated_at.
    """
    query = Task.select().order_by(Task.id)
    if status:
        query = query.where(Task.status == status)
    return rows(query)


@bound
def filter_tasks(
    db: SqliteDatabase,
    search: str = "",
    status: list[str] | None = None,
    agent: list[str] | None = None,
    sort_by: str | None = None,
    sort_dir: str = "asc",
) -> list[dict]:
    """Filter and sort tasks by search query, status, assigned agent, and sort field.

    Args:
        db: SqliteDatabase instance for this project.
        search: Case-insensitive substring match against title or description.
        status: Optional list of statuses to include (default: all).
        agent: Optional list of assigned agent names to include. An empty
               string in the list also matches unassigned tasks.
        sort_by: One of "title", "assigned_to", "status", "created_at",
                 "updated_at". Defaults to updated_at desc, created_at desc.
        sort_dir: "asc" or "desc" (only used with sort_by).

    Returns:
        list[dict]: Matching task records.
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

    sort_fields = {
        "title": fn.LOWER(fn.COALESCE(Task.title, "")),
        "assigned_to": fn.COALESCE(Task.assigned_to, ""),
        "status": fn.COALESCE(Task.status, ""),
        "created_at": fn.COALESCE(Task.created_at, 0),
        "updated_at": fn.COALESCE(Task.updated_at, 0),
    }
    field = sort_fields.get(sort_by)
    if field is not None:
        query = query.order_by(field.desc() if sort_dir == "desc" else field.asc())
    else:
        query = query.order_by(
            fn.COALESCE(Task.updated_at, 0).desc(), fn.COALESCE(Task.created_at, 0).desc()
        )

    return rows(query)


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
    """Does `start` reach `target` by following dependency edges?"""
    edges: dict[int, list[int]] = {}
    # .dicts() keys are field names, so the foreign key reads as "task"
    for edge in rows(TaskDep.select(TaskDep.task.alias("task"), TaskDep.depends_on)):
        edges.setdefault(edge["task"], []).append(edge["depends_on"])
    seen, queue = set(), list(edges.get(start, []))
    while queue:
        current = queue.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        queue += edges.get(current, [])
    return False


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


@bound
def task_events(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch all events associated with a task, in order.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query events for.

    Returns:
        list[dict]: Event records chronologically ordered.
    """
    return rows(Event.select().where(Event.task_id == task_id).order_by(Event.id))


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
def recent_events(db: SqliteDatabase, agent: str | None = None, limit: int = 50) -> list[dict]:
    """Fetch the most recent events, newest first, optionally filtered by agent.

    Useful for monitoring: see what just happened across the fleet or from a
    specific agent.

    Args:
        db: SqliteDatabase instance for this project.
        agent: Optional agent name to filter by.
        limit: Maximum number of events to return (default 50).

    Returns:
        list[dict]: Recent event records, newest first.

    Examples:
        >>> recent = recent_events(db, agent="claude-worker-1", limit=10)
        >>> for event in recent:
        ...     print(f"[{event['ts']}] {event['kind']}: {event['label']}")
    """
    query = Event.select().order_by(Event.id.desc()).limit(limit)
    if agent:
        query = query.where(Event.agent == agent)
    return rows(query)


# --------------------------------------------------------------------------
# docs (shared project knowledge)
# --------------------------------------------------------------------------


@bound
def docs_get(db: SqliteDatabase, key: str) -> str | None:
    """Fetch a shared project knowledge document.

    Agents and humans write documentation here that persists across invocations.
    Used for storing project context, architecture, decisions, etc.

    Args:
        db: SqliteDatabase instance for this project.
        key: Document identifier (e.g., "architecture", "conventions").

    Returns:
        str: Document content, or None if not found.

    Examples:
        >>> arch = docs_get(db, "architecture")
        >>> if arch:
        ...     print("Architecture notes:", arch)
    """
    doc = row(Doc.select(Doc.content).where(Doc.key == key))
    return doc["content"] if doc else None


@bound
def docs_set(db: SqliteDatabase, key: str, content: str, updated_by: str = HUMAN) -> None:
    """Write or update a shared project knowledge document.

    Creates a new document or replaces an existing one. Tracks who updated
    the document and when.

    Args:
        db: SqliteDatabase instance for this project.
        key: Document identifier.
        content: Document text (markdown, JSON, or any format).
        updated_by: Who is updating this (default "human").

    Examples:
        >>> docs_set(db, "conventions", "# Code Conventions\\n\\n- Use snake_case...",
        ...          updated_by="claude-reviewer")
    """
    Doc.replace(key=key, content=content, updated_by=updated_by, updated_at=now()).execute()


@bound
def docs_list(db: SqliteDatabase) -> list[dict]:
    """List all shared project documents, ordered by key.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: Document records with keys: key, content, updated_by,
                    updated_at.
    """
    return rows(Doc.select().order_by(Doc.key))


# --------------------------------------------------------------------------
# file claims - "I am touching this file"
#
# Advisory and cooperative: a claim is a message to the other agents, not a
# lock on the filesystem. The daemon takes them on the agent's behalf and
# tells it who to talk to when something is already held.
# --------------------------------------------------------------------------

# an agent that stopped heartbeating this long ago is not holding anything
CLAIM_STALE_AFTER = 180.0


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
    return str(candidate).strip("/") or "."


def _overlaps(a: str, b: str) -> bool:
    """Check if two normalized paths refer to the same file or overlap (directory containment).

    Args:
        a: Normalized path.
        b: Normalized path.

    Returns:
        bool: True if a == b or one is a directory ancestor of the other.
    """
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


@bound
def active_claims(db: SqliteDatabase) -> list[dict]:
    """Fetch all claims held by agents that are still alive (heartbeating).

    Agents are considered alive if their last heartbeat was within CLAIM_STALE_AFTER
    seconds. Stale claims (from dead agents) are excluded.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: Claim records from live agents, ordered by claimed_at.
                    Each has: id, path, agent, task_id, run_id, mode, note,
                    claimed_at.
    """
    cutoff = now() - CLAIM_STALE_AFTER
    query = (
        FileClaim.select(
            FileClaim.id, FileClaim.path, FileClaim.agent, FileClaim.task_id,
            FileClaim.run_id, FileClaim.mode, FileClaim.note, FileClaim.claimed_at,
        )
        .join(Agent, JOIN.LEFT_OUTER, on=(Agent.name == FileClaim.agent))
        .where(Agent.last_heartbeat.is_null(False) & (Agent.last_heartbeat >= cutoff))
        .order_by(FileClaim.claimed_at)
    )
    return rows(query)


def claim_holders(
    db: SqliteDatabase,
    path: str,
    agent: str | None = None,
    cached_claims: list[dict] | None = None,
) -> list[dict]:
    """Find live claims that overlap with a path, excluding one agent's own.

    Useful for detecting conflicts: "who else is touching this file?" Returns
    claims from other agents that overlap with the given path (exact match or
    directory containment).

    Args:
        db: SqliteDatabase instance for this project.
        path: File or directory to check.
        agent: Optional agent name to exclude from the result.
        cached_claims: Optional pre-fetched claims list to avoid redundant queries.

    Returns:
        list[dict]: Conflicting claim records from other agents.

    Examples:
        >>> conflicts = claim_holders(db, "src/foo.py", agent="me")
        >>> if conflicts:
        ...     print(f"{len(conflicts)} agent(s) touching src/foo.py")
    """
    target = normalize_path(path)
    claims = cached_claims if cached_claims is not None else active_claims(db)
    return [
        claim
        for claim in claims
        if _overlaps(target, claim["path"]) and claim["agent"] != agent
    ]


@bound
def claim_files(
    db: SqliteDatabase,
    agent: str,
    paths: list[str] | str,
    task_id: int | None = None,
    run_id: str | None = None,
    mode: str = "write",
    note: str | None = None,
) -> dict:
    """Claim what this agent is about to touch.

    An advisory lock: the agent is notifying the system "I am modifying these
    paths." If other agents are already touching them, both sides get the
    conflict info so they can coordinate. The claim always succeeds; the
    returned conflicts are informational.

    Args:
        db: SqliteDatabase instance for this project.
        agent: Agent claiming the paths.
        paths: Path or list of paths to claim.
        task_id: Associated task ID (optional).
        run_id: Associated invocation ID (optional).
        mode: Claim mode ("read" or "write", default "write").
        note: Optional annotation (e.g., reason for the claim).

    Returns:
        dict: Result with keys:
              - claimed (list): Normalized paths successfully claimed.
              - held_by_others (list): Conflicting claims from other agents.

    Examples:
        >>> result = claim_files(db, "claude-worker", ["src/foo.py", "tests/"],
        ...                       task_id=42, note="Refactoring")
        >>> if result["held_by_others"]:
        ...     print(f"Warning: {result['held_by_others']}")
    """
    wanted = [paths] if isinstance(paths, str) else list(paths)
    claimed, conflicts = [], []
    # Pre-fetch all active claims once, then filter per-file in Python
    all_claims = active_claims(db)
    for raw in wanted:
        path = normalize_path(raw)
        held = [c for c in claim_holders(db, path, agent=agent, cached_claims=all_claims) if c["mode"] == "write" or mode == "write"]
        conflicts += held
        mine = FileClaim.select().where(
            (FileClaim.path == path) & (FileClaim.agent == agent)
        ).first()
        if mine:
            FileClaim.update(
                task_id=task_id, run_id=run_id, mode=mode, note=note, claimed_at=now()
            ).where(FileClaim.id == mine.id).execute()
        else:
            FileClaim.create(
                path=path, agent=agent, task_id=task_id, run_id=run_id,
                mode=mode, note=note, claimed_at=now(),
            )
        claimed.append(path)
    return {"claimed": claimed, "held_by_others": conflicts}


@bound
def release_files(db: SqliteDatabase, agent: str, paths: list[str] | str | None = None) -> int:
    """Release file claims - let other agents know you're done with these paths.

    If paths is None, releases all claims from this agent. Otherwise releases
    only the specified paths.

    Args:
        db: SqliteDatabase instance for this project.
        agent: Agent releasing the claims.
        paths: Path or list of paths to release, or None for all.

    Returns:
        int: Number of claims deleted.

    Examples:
        >>> released = release_files(db, "claude-worker", ["src/foo.py"])
        >>> released = release_files(db, "claude-worker")  # release all
    """
    query = FileClaim.delete().where(FileClaim.agent == agent)
    if paths is not None:
        wanted = [paths] if isinstance(paths, str) else list(paths)
        query = query.where(FileClaim.path.in_([normalize_path(p) for p in wanted]))
    return query.execute()


@bound
def release_run(db: SqliteDatabase, run_id: str) -> int:
    """Release all file claims from a single invocation.

    Called by the daemon when an agent run completes, to clean up stale claims.
    Ensures claims never outlive the process that took them.

    Args:
        db: SqliteDatabase instance for this project.
        run_id: Invocation ID whose claims to release.

    Returns:
        int: Number of claims deleted.
    """
    return FileClaim.delete().where(FileClaim.run_id == run_id).execute()


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


def _generate_snippet(text: str, query: str, context_chars: int = 100) -> str:
    """Extract context around query match and highlight matching terms.

    Searches for the query term(s) in the text, extracts surrounding context,
    and highlights matching terms with <mark> tags. If truncated, adds ellipsis.

    Args:
        text: Text to extract snippet from.
        query: Query string (may contain multiple terms).
        context_chars: Approximate characters of context around match (default 100).

    Returns:
        str: Snippet with highlighted matches and ellipsis if truncated.
    """
    if not text or not query:
        return text[:context_chars] if text else ""

    # Extract query terms (simple whitespace-based split, ignoring operators)
    terms = [t.strip('"()').lower() for t in query.split() if t not in ('AND', 'OR', 'NOT')]
    if not terms:
        return text[:context_chars]

    # Find first occurrence of any term
    text_lower = text.lower()
    first_match_pos = len(text_lower)
    for term in terms:
        pos = text_lower.find(term)
        if pos != -1 and pos < first_match_pos:
            first_match_pos = pos

    if first_match_pos == len(text_lower):
        # No match found, return beginning
        snippet = text[:context_chars]
        if len(text) > context_chars:
            snippet += "..."
        return snippet

    # Extract context around first match
    start = max(0, first_match_pos - context_chars // 2)
    end = min(len(text), first_match_pos + context_chars // 2)

    snippet = text[start:end]

    # Add ellipsis if truncated
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."

    # Highlight all matching terms with <mark> tags
    for term in terms:
        # Case-insensitive replacement with preservation of original case
        import re
        snippet = re.sub(
            rf'\b({re.escape(term)})\b',
            lambda m: f'<mark>{m.group(1)}</mark>',
            snippet,
            flags=re.IGNORECASE,
        )

    return snippet


def _fts_search_table(
    db: SqliteDatabase,
    table: str,
    query: str,
    limit: int | None = None,
) -> list[dict]:
    """Search a single FTS5 table and return results with full context.

    Queries the FTS table with MATCH operator, joins back to source table
    for complete metadata, and generates snippets using context extraction.
    Returns all matching results (no limit), allowing the caller to handle
    pagination across multiple tables.

    Args:
        db: SqliteDatabase instance for this project.
        table: Table name ('docs', 'messages', 'tasks', 'events').
        query: FTS5 query string (supports AND, OR, NOT, "phrase").
        limit: Optional maximum results per table (for performance tuning).

    Returns:
        list[dict]: Result dicts with keys: table, id, title, snippet, rank, metadata.
    """
    fts_table = f"{table}_fts"
    results = []

    # Build table-specific query with joins
    if table == "docs":
        fts_query = f"""
            SELECT f.rowid, f.rank, f.content, d.key, d.updated_by, d.updated_at
            FROM {fts_table} f
            JOIN docs d ON d.rowid = f.rowid
            WHERE f.{fts_table} MATCH ?
            ORDER BY f.rank DESC
        """
        if limit:
            fts_query += f" LIMIT {limit}"

        fts_results = db.execute_sql(fts_query, (query,)).fetchall()

        for row_id, rank, text_content, key, updated_by, updated_at in fts_results:
            source_record = {
                "key": key,
                "content": text_content,
                "updated_by": updated_by,
                "updated_at": updated_at,
            }
            title = key if key else f"Doc #{row_id}"
            snippet = _generate_snippet(text_content, query, context_chars=150)
            metadata = {k: v for k, v in source_record.items() if k not in ["content"]}

            results.append({
                "table": table,
                "id": row_id,
                "title": title,
                "snippet": snippet,
                "rank": rank,
                "metadata": metadata,
            })

    elif table == "messages":
        fts_query = f"""
            SELECT f.rowid, f.rank, f.payload, m.sender, m.recipient, m.task_id, m.msg_type, m.ts, m.input_tokens, m.output_tokens, m.cache_read_tokens, m.cache_write_tokens, m.tool_rounds, m.cost_usd, m.read_at
            FROM {fts_table} f
            JOIN messages m ON m.id = f.rowid
            WHERE f.{fts_table} MATCH ?
            ORDER BY f.rank DESC
        """
        if limit:
            fts_query += f" LIMIT {limit}"

        fts_results = db.execute_sql(fts_query, (query,)).fetchall()

        for row_id, rank, text_content, sender, recipient, task_id, msg_type, ts, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd, read_at in fts_results:
            source_record = {
                "id": row_id,
                "sender": sender,
                "recipient": recipient,
                "task_id": task_id,
                "msg_type": msg_type,
                "ts": ts,
                "payload": text_content,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "tool_rounds": tool_rounds,
                "cost_usd": cost_usd,
                "read_at": read_at,
            }
            sender_name = sender if sender else "unknown"
            first_50 = text_content[:50] if text_content else ""
            title = f"From {sender_name}: {first_50}"
            snippet = _generate_snippet(text_content, query, context_chars=150)
            metadata = {k: v for k, v in source_record.items() if k not in ["payload"]}

            results.append({
                "table": table,
                "id": row_id,
                "title": title,
                "snippet": snippet,
                "rank": rank,
                "metadata": metadata,
            })

    elif table == "tasks":
        fts_query = f"""
            SELECT f.rowid, f.rank, f.title, f.description, t.assigned_to, t.status, t.created_at, t.updated_at
            FROM {fts_table} f
            JOIN tasks t ON t.id = f.rowid
            WHERE f.{fts_table} MATCH ?
            ORDER BY f.rank DESC
        """
        if limit:
            fts_query += f" LIMIT {limit}"

        fts_results = db.execute_sql(fts_query, (query,)).fetchall()

        for row_id, rank, title_text, description_text, assigned_to, status, created_at, updated_at in fts_results:
            source_record = {
                "id": row_id,
                "title": title_text,
                "description": description_text,
                "assigned_to": assigned_to,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            title = title_text if title_text else f"Task #{row_id}"
            # Generate snippet from title (primary indexed field)
            text_content = title_text or ""
            snippet = _generate_snippet(text_content, query, context_chars=150)
            metadata = {k: v for k, v in source_record.items() if k not in ["title", "description"]}

            results.append({
                "table": table,
                "id": row_id,
                "title": title,
                "snippet": snippet,
                "rank": rank,
                "metadata": metadata,
            })

    elif table == "events":
        fts_query = f"""
            SELECT f.rowid, f.rank, f.body, e.ts, e.agent, e.task_id, e.run_id, e.kind, e.label
            FROM {fts_table} f
            JOIN events e ON e.id = f.rowid
            WHERE f.{fts_table} MATCH ?
            ORDER BY f.rank DESC
        """
        if limit:
            fts_query += f" LIMIT {limit}"

        fts_results = db.execute_sql(fts_query, (query,)).fetchall()

        for row_id, rank, text_content, ts, agent, task_id, run_id, kind, label in fts_results:
            source_record = {
                "id": row_id,
                "ts": ts,
                "agent": agent,
                "task_id": task_id,
                "run_id": run_id,
                "kind": kind,
                "label": label,
                "body": text_content,
            }
            first_50 = text_content[:50] if text_content else ""
            if task_id:
                title = f"Task #{task_id}: {first_50}"
            else:
                title = f"Event #{row_id}: {first_50}"
            snippet = _generate_snippet(text_content, query, context_chars=150)
            metadata = {k: v for k, v in source_record.items() if k not in ["body"]}

            results.append({
                "table": table,
                "id": row_id,
                "title": title,
                "snippet": snippet,
                "rank": rank,
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

    # Sort by rank descending across all tables
    all_results.sort(key=lambda x: x["rank"], reverse=True)

    # Apply offset and limit across combined results
    end_idx = offset + limit
    return all_results[offset:end_idx]
