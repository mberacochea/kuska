"""doc_tasks: many-to-many link between docs and tasks.

docs.task_id (migration 011) holds one task per doc. A doc can relate to
several tasks and a task to several docs, so links now live in this table.
docs.task_id stays, unused for lookups but still driving ON DELETE CASCADE;
existing links are copied across, and docs keyed "task_<id>_..." are linked
by that convention. Index names are the ones peewee generates.
"""
import re

from peewee import *

# "task_42_dev-agent_context", "task-42-notes": the task id is evident from the key.
_KEY_TASK = re.compile(r"^task[_-](\d+)[_-]")


def up(migrator, db):
    """Create doc_tasks and backfill it from docs.task_id."""
    db.execute_sql(
        'CREATE TABLE IF NOT EXISTS "doc_tasks" ('
        '"id" INTEGER NOT NULL PRIMARY KEY, '
        '"doc_id" VARCHAR(255) NOT NULL, '
        '"task_id" INTEGER NOT NULL, '
        'FOREIGN KEY ("doc_id") REFERENCES "docs" ("key") ON DELETE CASCADE, '
        'FOREIGN KEY ("task_id") REFERENCES "tasks" ("id") ON DELETE CASCADE)'
    )
    db.execute_sql('CREATE INDEX IF NOT EXISTS "doctask_doc_id" ON "doc_tasks" ("doc_id")')
    db.execute_sql('CREATE INDEX IF NOT EXISTS "doctask_task_id" ON "doc_tasks" ("task_id")')
    db.execute_sql(
        'CREATE UNIQUE INDEX IF NOT EXISTS "doctask_doc_id_task_id" '
        'ON "doc_tasks" ("doc_id", "task_id")'
    )
    db.execute_sql(
        "INSERT OR IGNORE INTO doc_tasks (doc_id, task_id) "
        "SELECT key, task_id FROM docs WHERE task_id IS NOT NULL"
    )
    # docs written without a task_id but named after a task; anything without
    # an evident task in its name is left unlinked, as is a key naming a task
    # that does not exist
    for (key,) in db.execute_sql("SELECT key FROM docs").fetchall():
        match = _KEY_TASK.match(key)
        if match:
            db.execute_sql(
                "INSERT OR IGNORE INTO doc_tasks (doc_id, task_id) "
                "SELECT ?, id FROM tasks WHERE id = ?",
                (key, int(match.group(1))),
            )


def down(migrator, db):
    """Drop doc_tasks (docs.task_id was never touched)."""
    db.execute_sql('DROP TABLE IF EXISTS "doc_tasks"')
