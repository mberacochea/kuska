"""Runs: one row per agent invocation, with its own status, heartbeat and usage.

Until now a run existed only as events.run_id, and its cost was booked on
whichever messages row happened to be the result. This adds the table; the
daemon starts writing to it in a later change.

tasks are not referenced by a foreign key (as with events.task_id): deleting a
task keeps its runs for the ledger.
"""


def up(migrator, db):
    """Create runs and its indexes."""
    db.execute_sql(
        'CREATE TABLE IF NOT EXISTS "runs" ('
        '"id" VARCHAR(255) NOT NULL PRIMARY KEY, '
        '"task_id" INTEGER, '
        '"agent" VARCHAR(255), '
        '"status" VARCHAR(255) NOT NULL, '
        '"exit_reason" TEXT, '
        '"started_at" REAL NOT NULL, '
        '"heartbeat_at" REAL NOT NULL, '
        '"ended_at" REAL, '
        '"input_tokens" INTEGER NOT NULL, '
        '"output_tokens" INTEGER NOT NULL, '
        '"cache_read_tokens" INTEGER NOT NULL, '
        '"cache_write_tokens" INTEGER NOT NULL, '
        '"tool_rounds" INTEGER NOT NULL, '
        '"cost_usd" REAL NOT NULL, '
        '"result_message_id" INTEGER)'
    )
    # the names peewee gives these indexes, so init_db adds no twins
    db.execute_sql('CREATE INDEX IF NOT EXISTS "run_task_id" ON "runs" ("task_id")')
    db.execute_sql('CREATE INDEX IF NOT EXISTS "run_status" ON "runs" ("status")')


def down(migrator, db):
    """Drop the runs table (its indexes go with it)."""
    db.execute_sql('DROP TABLE IF EXISTS "runs"')
