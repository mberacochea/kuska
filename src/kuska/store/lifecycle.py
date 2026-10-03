"""The one table of task status changes, and the function that applies it.

Every status change a task goes through is an event here: which statuses it
may start from, and which it ends in. Keeping them in one place means the
rules (a worktree task is merged before it is done, a human override leaves a
note) cannot drift between the store, the daemon and the web routes.
"""

from __future__ import annotations

from peewee import SqliteDatabase

from ..db import HUMAN, TASK_STATUSES, now
from ..models import Message, Task
from .common import bound
from .tasks import get_task


class InvalidTransition(ValueError):
    """A status change the task's current status does not allow."""


# event -> (statuses it may start from, status it ends in)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "make_ready": (("todo",), "ready"),  # needs an assigned agent
    "park": (("ready", "needs_approval", "ready_to_merge", "blocked", "done"), "todo"),
    "claim": (("ready",), "in_progress"),  # claim_task does this itself, atomically
    "finish": (("in_progress",), "done"),  # -> ready_to_merge when the task has a worktree_path
    "hold": (("in_progress",), "needs_approval"),
    "block": (("in_progress",), "blocked"),
    "await_answer": (("in_progress",), "ready"),
    "approve": (("needs_approval",), "done"),
    "merged": (("ready_to_merge",), "done"),  # caller has checked the branch is merged
    "requeue": (("needs_approval", "ready_to_merge", "blocked", "done"), "ready"),  # -> todo when unassigned
    "close": (("todo", "ready", "needs_approval", "blocked"), "done"),  # never ready_to_merge: merge first
    "force": (TASK_STATUSES, None),  # a human override to any status; leaves a note
}


@bound
def transition(db: SqliteDatabase, task_id: int, event: str, actor: str = HUMAN, to: str | None = None) -> dict:
    """Apply a lifecycle event to a task and return the updated task.

    The change is one guarded UPDATE (`WHERE status IN <allowed starts>`), so
    the check and the write are atomic even against other processes.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task.
        event: A key of TRANSITIONS.
        actor: Who is making the change; "force" is reserved for HUMAN.
        to: Target status, only for "force".

    Raises:
        ValueError: Unknown event, missing task, or (force) an unknown `to`.
        InvalidTransition: The task's current status does not allow the event,
            "make_ready" on an unassigned task, or "force" by a non-human.
    """
    if event not in TRANSITIONS:
        raise ValueError(f"unknown lifecycle event: {event}")
    task = get_task(db, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")

    from_statuses, target = TRANSITIONS[event]
    if event == "finish":
        target = "ready_to_merge" if task["worktree_path"] else "done"
    elif event == "requeue":
        target = "ready" if task["assigned_to"] else "todo"
    elif event == "make_ready" and not task["assigned_to"]:
        raise InvalidTransition("assign an agent before making it ready")
    elif event == "force":
        if to not in TASK_STATUSES:
            raise ValueError(f"unknown task status: {to}")
        if actor != HUMAN:
            raise InvalidTransition("only a human can force a status")
        target = to

    changed = (
        Task.update(status=target, updated_at=now())
        .where((Task.id == task_id) & Task.status.in_(from_statuses))
        .execute()
    )
    if not changed:
        current = get_task(db, task_id)
        status = current["status"] if current else "gone"
        raise InvalidTransition(f"cannot {event} task {task_id}: it is {status}")

    if event == "force" and task["status"] != target:
        Message.create(
            ts=now(),
            sender=actor,
            recipient=HUMAN,
            task_id=task_id,
            msg_type="note",
            payload=f"status forced from {task['status']} to {target}",
        )
    return get_task(db, task_id)
