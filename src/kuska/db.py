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

# 'needs_approval' is a hold: the task does not run, and neither does anything
# depending on it, until a human approves it or sends it back.
TASK_STATUSES = ("todo", "in_progress", "needs_approval", "blocked", "done")
HOLDING_STATUSES = ("needs_approval", "blocked")
AGENT_STATUSES = ("idle", "working", "offline")

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
    """
    # Run migrations to apply any pending schema changes
    try:
        run_migrations(database)
    except Exception as e:
        # If migrations fail, log but continue - _init_db will catch fallback cases
        import sys
        print(f"Warning: migration failed (will try Peewee fallback): {e}", file=sys.stderr)

    # Peewee table creation as fallback, in case migrations didn't cover everything
    # (e.g., for databases that existed before the first migration was created)
    _init_db(database)


def now() -> float:
    return time.time()
