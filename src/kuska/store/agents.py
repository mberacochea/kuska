"""The agent registry and heartbeats."""

from __future__ import annotations

from peewee import SqliteDatabase

from ..db import now
from ..models import Agent, Task, row, rows
from .common import bound


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

    Called by the agent's daemon as it starts, picks up a task, finishes one
    and stops, so the agents page shows who is doing what.

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
