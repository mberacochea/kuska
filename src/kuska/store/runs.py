"""Runs: one agent invocation on one task - status, heartbeat and usage."""

from __future__ import annotations

from peewee import SqliteDatabase

from ..db import RUN_STATUSES, now
from ..models import Run, row, rows
from .common import bound

# the usage columns end_run accepts; any other keyword is ignored
USAGE_COLUMNS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "tool_rounds", "cost_usd",
)


@bound
def start_run(db: SqliteDatabase, run_id: str, task_id: int | None, agent: str | None) -> None:
    """Record a new `running` run; its start and first heartbeat are now."""
    ts = now()
    Run.insert(
        id=run_id, task_id=task_id, agent=agent, status="running", started_at=ts, heartbeat_at=ts
    ).execute()


@bound
def touch_run(db: SqliteDatabase, run_id: str) -> None:
    """Heartbeat a run. Does nothing once the run has ended."""
    Run.update(heartbeat_at=now()).where(Run.id == run_id, Run.status == "running").execute()


@bound
def end_run(
    db: SqliteDatabase,
    run_id: str,
    status: str,
    exit_reason: str | None = None,
    result_message_id: int | None = None,
    **usage,
) -> None:
    """Close a run with a final status, and book whatever usage is given.

    Raises:
        ValueError: If status is not in RUN_STATUSES, or is `running`.
    """
    if status not in RUN_STATUSES or status == "running":
        raise ValueError(f"a run ends as one of {RUN_STATUSES[1:]}, not {status!r}")
    values = {k: v for k, v in usage.items() if k in USAGE_COLUMNS}
    Run.update(
        status=status, ended_at=now(), exit_reason=exit_reason,
        result_message_id=result_message_id, **values,
    ).where(Run.id == run_id).execute()


@bound
def get_run(db: SqliteDatabase, run_id: str) -> dict | None:
    """One run by id, or None."""
    return row(Run.select().where(Run.id == run_id))


@bound
def task_runs(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Every run of a task, oldest first."""
    return rows(Run.select().where(Run.task_id == task_id).order_by(Run.started_at, Run.id))


@bound
def running_runs(db: SqliteDatabase) -> list[dict]:
    """Runs still marked `running`, oldest first."""
    return rows(Run.select().where(Run.status == "running").order_by(Run.started_at, Run.id))


@bound
def stale_runs(db: SqliteDatabase, older_than_s: float) -> list[dict]:
    """Running runs whose last heartbeat is more than `older_than_s` seconds old."""
    return rows(
        Run.select()
        .where(Run.status == "running", Run.heartbeat_at < now() - older_than_s)
        .order_by(Run.started_at, Run.id)
    )


@bound
def set_run_result_message(
    db: SqliteDatabase, agent: str, task_id: int, message_id: int
) -> str | None:
    """Attach a result message to the newest running run of (agent, task_id).

    Returns:
        str | None: The run's id, or None if that agent has no running run on the task.
    """
    run = (
        Run.select(Run.id)
        .where(Run.agent == agent, Run.task_id == task_id, Run.status == "running")
        .order_by(Run.started_at.desc(), Run.id.desc())
        .first()
    )
    if run is None:
        return None
    Run.update(result_message_id=message_id).where(Run.id == run.id).execute()
    return str(run.id)
