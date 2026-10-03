"""Events: each agent run's monologue, kept for audit."""

from __future__ import annotations

from typing import Any

from peewee import JOIN, SqliteDatabase, fn

from ..db import EVENT_KINDS, now
from ..models import Event, row, rows
from .common import bound


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
