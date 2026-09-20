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
    Agent.insert(name=name, backend=backend, role=role, status="offline").on_conflict(
        conflict_target=[Agent.name],
        update={Agent.backend: backend, Agent.role: role},
    ).execute()


@bound
def heartbeat(db: SqliteDatabase, name: str, status: str, task_id: int | None = None) -> None:
    Agent.update(status=status, current_task_id=task_id, last_heartbeat=now()).where(
        Agent.name == name
    ).execute()


@bound
def list_agents(db: SqliteDatabase) -> list[dict]:
    return rows(Agent.select().order_by(Agent.name))


@bound
def get_agent(db: SqliteDatabase, name: str) -> dict | None:
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
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {status}")
    Task.update(status=status, updated_at=now()).where(Task.id == task_id).execute()


@bound
def update_task(db: SqliteDatabase, task_id: int, **fields: Any) -> None:
    """Patch title / description / assigned_to / status on one task."""
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
    Task.delete().where(Task.id == task_id).execute()


@bound
def list_tasks(db: SqliteDatabase, status: str | None = None) -> list[dict]:
    query = Task.select().order_by(Task.id)
    if status:
        query = query.where(Task.status == status)
    return rows(query)


@bound
def get_task(db: SqliteDatabase, task_id: int) -> dict | None:
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
    """Block until a runnable task is assigned to this agent, then claim it."""
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
    """Make `task_id` wait for `depends_on`. Refuses self-loops and cycles."""
    if task_id == depends_on:
        raise ValueError("a task cannot depend on itself")
    if _reaches(db, depends_on, task_id):
        raise ValueError(f"task {depends_on} already depends on task {task_id}")
    TaskDep.insert(task=task_id, depends_on=depends_on).on_conflict_ignore().execute()


@bound
def remove_dependency(db: SqliteDatabase, task_id: int, depends_on: int) -> None:
    TaskDep.delete().where(
        (TaskDep.task == task_id) & (TaskDep.depends_on == depends_on)
    ).execute()


@bound
def task_dependencies(db: SqliteDatabase, task_id: int) -> list[dict]:
    """The tasks this one waits for, as full task rows."""
    return rows(
        Task.select()
        .join(TaskDep, on=(TaskDep.depends_on == Task.id))
        .where(TaskDep.task == task_id)
        .order_by(Task.id)
    )


@bound
def task_dependents(db: SqliteDatabase, task_id: int) -> list[dict]:
    """The tasks waiting for this one."""
    return rows(
        Task.select()
        .join(TaskDep, on=(TaskDep.task == Task.id))
        .where(TaskDep.depends_on == task_id)
        .order_by(Task.id)
    )


def blocking_dependencies(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Why this task cannot run yet: its dependencies that are not done."""
    return [d for d in task_dependencies(db, task_id) if d["status"] != "done"]


@bound
def blocking_map(db: SqliteDatabase) -> dict[int, list[dict]]:
    """Every task's unfinished dependencies, in one pass, for the task list."""
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
    cost_usd: float = 0.0,
) -> int:
    message = Message.create(
        ts=now(),
        sender=sender,
        recipient=recipient,
        task_id=task_id,
        msg_type=msg_type,
        payload=payload,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
    cost_usd: float = 0.0,
    status: str = "done",
) -> int:
    """Log a task result back to the human coordinator and close the task."""
    msg_id = send_message(
        db, agent_name, HUMAN, task_id, "result", payload,
        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd,
    )
    if task_id is not None:
        update_task_status(db, task_id, status)
    return msg_id


@bound
def get_inbox(db: SqliteDatabase, agent_name: str, mark_read: bool = True) -> list[dict]:
    """Unread messages addressed to this agent, oldest first."""
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
    return rows(Message.select().where(Message.task_id == task_id).order_by(Message.ts))


@bound
def token_usage_by_agent(db: SqliteDatabase) -> list[dict]:
    query = (
        Message.select(
            Message.sender.alias("agent"),
            fn.COUNT(Message.id).alias("turns"),
            fn.COALESCE(fn.SUM(Message.input_tokens), 0).alias("input_tokens"),
            fn.COALESCE(fn.SUM(Message.output_tokens), 0).alias("output_tokens"),
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
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    event = Event.create(
        ts=now(), agent=agent, task_id=task_id, run_id=run_id, kind=kind, label=label, body=body
    )
    return int(event.id)


@bound
def task_events(db: SqliteDatabase, task_id: int) -> list[dict]:
    return rows(Event.select().where(Event.task_id == task_id).order_by(Event.id))


@bound
def run_events(db: SqliteDatabase, run_id: str) -> list[dict]:
    return rows(Event.select().where(Event.run_id == run_id).order_by(Event.id))


@bound
def recent_events(db: SqliteDatabase, agent: str | None = None, limit: int = 50) -> list[dict]:
    """Newest last, so it reads like a terminal."""
    query = Event.select().order_by(Event.id.desc()).limit(limit)
    if agent:
        query = query.where(Event.agent == agent)
    return list(reversed(rows(query)))


# --------------------------------------------------------------------------
# docs (shared project knowledge)
# --------------------------------------------------------------------------


@bound
def docs_get(db: SqliteDatabase, key: str) -> str | None:
    doc = row(Doc.select(Doc.content).where(Doc.key == key))
    return doc["content"] if doc else None


@bound
def docs_set(db: SqliteDatabase, key: str, content: str, updated_by: str = HUMAN) -> None:
    Doc.replace(key=key, content=content, updated_by=updated_by, updated_at=now()).execute()


@bound
def docs_list(db: SqliteDatabase) -> list[dict]:
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
    """Claims are keyed by project-relative path, so two agents naming the
    same file differently still collide."""
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
    """Same file, or one is a directory containing the other."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


@bound
def active_claims(db: SqliteDatabase) -> list[dict]:
    """Every claim whose holder is still alive."""
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


def claim_holders(db: SqliteDatabase, path: str, agent: str | None = None) -> list[dict]:
    """Live claims that overlap this path, optionally excluding one agent's own."""
    target = normalize_path(path)
    return [
        claim
        for claim in active_claims(db)
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

    Returns {"claimed": [...], "held_by_others": [...]} - taking a claim never
    fails, because the point is to make the overlap visible to both sides.
    """
    wanted = [paths] if isinstance(paths, str) else list(paths)
    claimed, conflicts = [], []
    for raw in wanted:
        path = normalize_path(raw)
        held = [c for c in claim_holders(db, path, agent=agent) if c["mode"] == "write" or mode == "write"]
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
    """Let go of some paths, or of everything this agent holds."""
    query = FileClaim.delete().where(FileClaim.agent == agent)
    if paths is not None:
        wanted = [paths] if isinstance(paths, str) else list(paths)
        query = query.where(FileClaim.path.in_([normalize_path(p) for p in wanted]))
    return query.execute()


@bound
def release_run(db: SqliteDatabase, run_id: str) -> int:
    """Drop everything one invocation claimed - the daemon does this when the
    run ends, so claims never outlive the process that took them."""
    return FileClaim.delete().where(FileClaim.run_id == run_id).execute()
