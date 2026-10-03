"""task_tags: one row per (task, tag), replacing the comma-separated tasks.tags.

Filtering the text column with LIKE '%bug%' also matched 'debug'. A table makes
tags exact and indexable. Existing tags are backfilled, normalised the same way
the code does (stripped, lowercased, empties skipped).

tasks.tags stays in the database, unused: older running kuska processes still
read it, and dropping a column is never worth breaking them. The model no longer
has the field. Index names are the ones peewee generates, so init_db adds none.
"""
from peewee import *


def up(migrator, db):
    """Create task_tags and backfill it from tasks.tags."""
    db.execute_sql(
        'CREATE TABLE IF NOT EXISTS "task_tags" ('
        '"id" INTEGER NOT NULL PRIMARY KEY, '
        '"task_id" INTEGER NOT NULL, '
        '"tag" VARCHAR(255) NOT NULL, '
        'FOREIGN KEY ("task_id") REFERENCES "tasks" ("id") ON DELETE CASCADE)'
    )
    db.execute_sql('CREATE INDEX IF NOT EXISTS "tasktag_task_id" ON "task_tags" ("task_id")')
    db.execute_sql(
        'CREATE UNIQUE INDEX IF NOT EXISTS "tasktag_task_id_tag" ON "task_tags" ("task_id", "tag")'
    )
    found = db.execute_sql("SELECT id, tags FROM tasks WHERE tags IS NOT NULL").fetchall()
    for task_id, tags in found:
        for tag in tags.split(","):
            tag = tag.strip().lower()
            if tag:
                db.execute_sql(
                    "INSERT OR IGNORE INTO task_tags (task_id, tag) VALUES (?, ?)", (task_id, tag)
                )


def down(migrator, db):
    """Drop task_tags (tasks.tags was never touched)."""
    db.execute_sql('DROP TABLE IF EXISTS "task_tags"')
