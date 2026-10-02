"""Link docs to the task they belong to.

Adds a nullable docs.task_id column, referencing tasks(id) ON DELETE CASCADE,
so a plan or handover report dies with its task instead of becoming an
orphan, and callers can ask "which docs belong to task 42" without parsing
key strings like "task_42_dev-agent_context".

Added with a plain ALTER TABLE ADD COLUMN rather than the rebuild-and-copy
pattern migration 004 uses: a rebuild reassigns rowids, and docs_fts (see
migration 005) is an external-content FTS5 index keyed on docs.rowid - a
rebuild here would desync it. ALTER TABLE ADD COLUMN leaves existing rows
and their rowids untouched, and SQLite accepts a REFERENCES clause on a
column added this way (existing rows get NULL, which foreign key
enforcement always accepts).

Existing docs already encode a task in their key by convention
("task_<id>_<agent>_context" - see runtime.py's store_workflow_context), so
those rows are backfilled from that pattern rather than left unlinked.
"""
from peewee import *


def up(migrator, db):
    """Add docs.task_id and backfill it from the "task_<id>_..." key convention."""
    db.execute_sql(
        'ALTER TABLE "docs" ADD COLUMN "task_id" INTEGER '
        'REFERENCES "tasks" ("id") ON DELETE CASCADE'
    )
    migrator.add_index('docs', ('task_id',), False).run()

    db.execute_sql(
        """
        UPDATE docs
        SET task_id = CAST(
            substr(key, 6, instr(substr(key, 6), '_') - 1) AS INTEGER
        )
        WHERE key LIKE 'task\\_%' ESCAPE '\\'
          AND instr(substr(key, 6), '_') > 1
          AND substr(key, 6, instr(substr(key, 6), '_') - 1) GLOB '[0-9]*'
        """
    )


def down(migrator, db):
    """Drop the index and column."""
    migrator.drop_index('docs', 'docs_task_id').run()
    op = migrator.drop_column('docs', 'task_id')
    op.run()
