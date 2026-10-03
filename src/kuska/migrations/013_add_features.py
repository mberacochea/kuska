"""Features: a table of named task groups, and tasks.feature_id pointing at it.

Migration 006 gave tasks a free-text `feature` column. A table instead gives a
feature an identity of its own - a description now, and later whatever
per-feature work hangs off it - and makes a rename one UPDATE.

tasks.feature_id is added with ALTER TABLE ADD COLUMN ... REFERENCES, as in
migration 011: a table rebuild would reassign rowids under tasks_fts.

Every distinct free-text value is backfilled into a feature row, and its tasks
linked to it. The old `feature` column is left in place, unused by this code:
dropping it would break any kuska process still running the previous version
against this database (its SELECTs name the column). A later migration can
drop it once nothing reads it.
"""
import time

from peewee import *


def up(migrator, db):
    """Create features, add tasks.feature_id, backfill from tasks.feature."""
    db.execute_sql(
        'CREATE TABLE IF NOT EXISTS "features" ('
        '"id" INTEGER NOT NULL PRIMARY KEY, '
        '"name" VARCHAR(255) NOT NULL, '
        '"description" TEXT, '
        '"created_at" REAL NOT NULL, '
        '"updated_at" REAL NOT NULL)'
    )
    # the names peewee gives these indexes (model name, not table name), so
    # init_db's create_tables(safe=True) finds them there and adds no twins
    db.execute_sql('CREATE UNIQUE INDEX IF NOT EXISTS "feature_name" ON "features" ("name")')
    db.execute_sql(
        'ALTER TABLE "tasks" ADD COLUMN "feature_id" INTEGER '
        'REFERENCES "features" ("id") ON DELETE SET NULL'
    )
    db.execute_sql('CREATE INDEX IF NOT EXISTS "task_feature_id" ON "tasks" ("feature_id")')

    columns = {c.name for c in db.get_columns("tasks")}
    if "feature" not in columns:
        return
    now = time.time()
    db.execute_sql(
        """
        INSERT INTO features (name, created_at, updated_at)
        SELECT DISTINCT lower(trim(feature)), ?, ? FROM tasks
        WHERE feature IS NOT NULL AND trim(feature) != ''
        """,
        (now, now),
    )
    db.execute_sql(
        """
        UPDATE tasks
        SET feature_id = (SELECT id FROM features WHERE name = lower(trim(tasks.feature)))
        WHERE feature IS NOT NULL AND trim(feature) != ''
        """
    )


def down(migrator, db):
    """Drop tasks.feature_id and the features table. The old free-text column
    was never removed, so nothing needs restoring."""
    db.execute_sql('DROP INDEX IF EXISTS "task_feature_id"')
    migrator.drop_column('tasks', 'feature_id').run()
    db.execute_sql('DROP TABLE IF EXISTS "features"')
