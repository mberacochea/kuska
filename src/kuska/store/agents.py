"""The agent registry and heartbeats."""

from __future__ import annotations

from peewee import SqliteDatabase

from ..db import now
from ..models import Agent, Run, Task, row, rows
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


# an agent whose last heartbeat is older than this is offline (workers beat every 30 s)
IDLE_FOR_S = 60.0


@bound
def heartbeat(db: SqliteDatabase, name: str) -> None:
    """Record that one of the agent's workers is alive.

    Status is not stored: several workers can serve one agent, so it is derived
    from `runs` and this timestamp (see `_with_status`)."""
    Agent.update(last_heartbeat=now()).where(Agent.name == name).execute()


def _with_status(db: SqliteDatabase, agents: list[dict]) -> list[dict]:
    """Add `status` and `running` (a count) to each agent record.

    working while the agent has `running` runs, else idle if its last heartbeat
    is under IDLE_FOR_S old, else offline."""
    counts: dict[str, int] = {}
    for run in Run.select(Run.agent).where(Run.status == "running"):
        counts[run.agent] = counts.get(run.agent, 0) + 1
    current = now()
    for agent in agents:
        agent["running"] = counts.get(agent["name"], 0)
        beat = agent.get("last_heartbeat")
        if agent["running"]:
            agent["status"] = "working"
        elif beat and current - beat < IDLE_FOR_S:
            agent["status"] = "idle"
        else:
            agent["status"] = "offline"
    return agents


@bound
def list_agents(db: SqliteDatabase) -> list[dict]:
    """List all registered agents, ordered by name.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: Agent records with keys: id, name, backend, role, status,
                    running, last_heartbeat, created_at. `status` is derived
                    (working / idle / offline) and `running` counts the agent's
                    running runs.
    """
    return _with_status(db, rows(Agent.select().order_by(Agent.name)))


@bound
def get_agent(db: SqliteDatabase, name: str) -> dict | None:
    """Fetch a single agent record by name, with derived `status` and `running`.

    Args:
        db: SqliteDatabase instance for this project.
        name: Agent identifier.

    Returns:
        dict: Agent record, or None if not found.
    """
    agent = row(Agent.select().where(Agent.name == name))
    return _with_status(db, [agent])[0] if agent else None


@bound
def delete_agent(db: SqliteDatabase, name: str) -> int:
    """Forget an agent. Its tasks go back to unassigned rather than vanishing;
    its messages stay, so the thread and the cost ledger keep their history."""
    freed = (
        Task.update(assigned_to=None, updated_at=now()).where(Task.assigned_to == name).execute()
    )
    Agent.delete().where(Agent.name == name).execute()
    return freed
