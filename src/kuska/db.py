"""Shared vocabulary and the database handle.

The schema itself lives in models.py - this module is what the rest of the
package imports for statuses, roles and a connection.
"""

from __future__ import annotations

import os
import time

from peewee import SqliteDatabase

from .migration import run_migrations
from .models import connect as _connect
from .models import init_db as _init_db

HUMAN = "human"

# 'todo' is a waiting list nothing runs from; a human (or a requeue/reply)
# moves a task to 'ready', and agents claim only 'ready' tasks. Answer tasks -
# one agent's question to another (store.ask_agent) - start out 'ready'.
# 'needs_approval' and 'ready_to_merge' are holds: the task does not run, and
# neither does anything depending on it, until a human approves/merges it or
# sends it back. 'needs_approval' is for agent decisions; 'ready_to_merge' means
# the agent committed its work to a branch and a human must review and merge.
TASK_STATUSES = ("todo", "ready", "in_progress", "needs_approval", "ready_to_merge", "blocked", "done")
HOLDING_STATUSES = ("needs_approval", "ready_to_merge", "blocked")
AGENT_STATUSES = ("idle", "working", "offline")
# a run (one agent invocation, table `runs`) is 'running' until it ends; 'abandoned'
# is for a run whose process vanished without ending it (found by a stale heartbeat)
RUN_STATUSES = ("running", "finished", "failed", "abandoned")

# What one agent invocation narrates as it works. `messages` stays what agents
# and humans say to each other; this is the monologue underneath it.
EVENT_KINDS = (
    "prompt", "thinking", "text", "tool_use", "tool_result", "system", "error", "result", "warning",
)


def connect(db_path: str | os.PathLike) -> SqliteDatabase:
    """Open (or create) one project's database."""
    return _connect(db_path)


def init_db(database: SqliteDatabase) -> None:
    """Create any table this version knows about that the file does not have.

    This uses the peewee ORM migration system to evolve the schema.

    A half-applied migration must not be allowed to survive as a printed
    warning: a desynced FTS5 index from a partially-applied migration 005
    looks fine right up until it 500s an innocent `UPDATE` weeks later. If a
    migration fails, this raises and startup refuses to serve rather than
    limping on with an unknown schema state.

    Migrations are serialized across processes by a filesystem lock in the
    migration module itself, not by the global write lock that previously
    affected all database operations.
    """
    # Run migrations - they use a migration-specific filesystem lock to ensure
    # only one process runs them at a time, even when multiple processes call
    # init_db() simultaneously at startup.
    run_migrations(database)

    # Peewee table creation as fallback, in case migrations didn't cover everything
    # (e.g., for databases that existed before the first migration was created)
    _init_db(database)


def now() -> float:
    return time.time()
