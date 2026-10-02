"""Add the "ready" task status.

"todo" becomes a waiting list; agents claim only "ready" tasks. Tasks that
were already queued for an agent (todo + assigned) are moved to "ready" so
work queued before the upgrade keeps running. Soft-deleted tasks are left
alone. Status is a plain text column, so there is no schema change.
"""
from peewee import *


def up(migrator, db):
    """Move assigned, live todo tasks to ready."""
    db.execute_sql(
        "UPDATE tasks SET status = 'ready' "
        "WHERE status = 'todo' AND assigned_to IS NOT NULL AND deleted_at IS NULL"
    )


def down(migrator, db):
    """Return ready tasks to todo."""
    db.execute_sql("UPDATE tasks SET status = 'todo' WHERE status = 'ready'")
