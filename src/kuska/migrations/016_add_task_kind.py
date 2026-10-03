"""tasks.kind: what a task is for (work, answer or review).

Answer tasks used to be recognised by the tag `answer`, which is user data and
can be removed. The column replaces it; existing tagged tasks are backfilled.
Added with ALTER TABLE ADD COLUMN, never a table rebuild (FTS5 is keyed on rowid).
"""
from peewee import *


def up(migrator, db):
    """Add tasks.kind and backfill answer tasks from the old tag."""
    db.execute_sql("ALTER TABLE \"tasks\" ADD COLUMN \"kind\" VARCHAR(16) NOT NULL DEFAULT 'work'")
    db.execute_sql(
        "UPDATE tasks SET kind = 'answer' "
        "WHERE ',' || COALESCE(tags, '') || ',' LIKE '%,answer,%'"
    )


def down(migrator, db):
    """Drop tasks.kind."""
    migrator.drop_column('tasks', 'kind').run()
