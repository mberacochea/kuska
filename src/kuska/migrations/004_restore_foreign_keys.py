"""Restore the foreign keys on tasks and task_deps.

Migration 001 declared `tasks.assigned_to`, `task_deps.task_id` and
`task_deps.depends_on` as plain columns carrying a comment that said "FK to
...". A comment is not a constraint, so every database created by the migration
runner has no referential integrity on those columns: a task can be assigned to
an agent that does not exist, and a dependency can point at a deleted task.
Databases created before the runner landed were built straight from models.py
and do have the keys.

SQLite cannot add a foreign key to an existing table, so each affected table is
rebuilt: create the replacement with the constraint, copy the rows, swap it in.
Rows that already violate the constraint would make the swap fail, so they are
repaired first - a dangling assigned_to becomes NULL (what ON DELETE SET NULL
would have done), and a dependency edge pointing at a missing task is dropped
(what ON DELETE CASCADE would have done).

This migration is a no-op on a database that already has the keys.
"""
from peewee import *


def _fk_count(db, table: str) -> int:
    return len(db.execute_sql(f"PRAGMA foreign_key_list({table})").fetchall())


def _rebuild(db, table: str, create_sql: str, columns: list[str]) -> None:
    """Swap `table` for one built by `create_sql`, preserving `columns`."""
    cols = ", ".join(f'"{c}"' for c in columns)
    db.execute_sql(f'ALTER TABLE "{table}" RENAME TO "{table}__old"')
    db.execute_sql(create_sql)
    db.execute_sql(f'INSERT INTO "{table}" ({cols}) SELECT {cols} FROM "{table}__old"')
    db.execute_sql(f'DROP TABLE "{table}__old"')


def up(migrator, db):
    """Rebuild tasks and task_deps with their foreign keys."""
    # the rebuild renames and drops tables, which enforcement would fight
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
        with db.atomic():
            if not _fk_count(db, "tasks"):
                db.execute_sql(
                    "UPDATE tasks SET assigned_to = NULL WHERE assigned_to IS NOT NULL "
                    "AND assigned_to NOT IN (SELECT name FROM agents)"
                )
                # deleted_at is present only if migration 002 has run
                has_deleted_at = any(
                    r[1] == "deleted_at"
                    for r in db.execute_sql("PRAGMA table_info(tasks)").fetchall()
                )
                deleted_at_col = ', "deleted_at" REAL' if has_deleted_at else ""
                _rebuild(
                    db, "tasks",
                    'CREATE TABLE "tasks" ("id" INTEGER NOT NULL PRIMARY KEY, "title" TEXT, '
                    '"description" TEXT, "assigned_to" VARCHAR(255), "status" VARCHAR(255) NOT NULL, '
                    '"created_at" REAL NOT NULL, "updated_at" REAL NOT NULL' + deleted_at_col +
                    ', FOREIGN KEY ("assigned_to") REFERENCES "agents" ("name") ON DELETE SET NULL)',
                    ["id", "title", "description", "assigned_to", "status", "created_at", "updated_at"]
                    + (["deleted_at"] if has_deleted_at else []),
                )
                db.execute_sql('CREATE INDEX IF NOT EXISTS "task_assigned_to" ON "tasks" ("assigned_to")')

            if not _fk_count(db, "task_deps"):
                db.execute_sql(
                    "DELETE FROM task_deps WHERE task_id NOT IN (SELECT id FROM tasks) "
                    "OR depends_on NOT IN (SELECT id FROM tasks)"
                )
                _rebuild(
                    db, "task_deps",
                    'CREATE TABLE "task_deps" ("id" INTEGER NOT NULL PRIMARY KEY, '
                    '"task_id" INTEGER NOT NULL, "depends_on" INTEGER NOT NULL, '
                    'FOREIGN KEY ("task_id") REFERENCES "tasks" ("id") ON DELETE CASCADE, '
                    'FOREIGN KEY ("depends_on") REFERENCES "tasks" ("id") ON DELETE CASCADE)',
                    ["id", "task_id", "depends_on"],
                )
                db.execute_sql(
                    'CREATE UNIQUE INDEX IF NOT EXISTS "task_dep_task_id_depends_on" '
                    'ON "task_deps" ("task_id", "depends_on")'
                )
    finally:
        db.execute_sql("PRAGMA foreign_keys=ON")


def down(migrator, db):
    """Deliberately not reversible.

    Dropping the keys again would mean rebuilding both tables to make the
    schema worse, and the rows repaired on the way up cannot be un-repaired.
    """
    raise NotImplementedError(
        "004_restore_foreign_keys cannot be reversed; restore from a backup instead"
    )
