"""tasks.review_of and tasks.review_outcome: link a review task to the task it reviews.

review_of points at the reviewed task (cascading delete); review_outcome is set
on the review task once it has run: passed, changes_requested or inconclusive.
Added with ALTER TABLE ADD COLUMN, never a table rebuild (FTS5 is keyed on rowid).
"""
from peewee import *


def up(migrator, db):
    """Add tasks.review_of (with its index) and tasks.review_outcome."""
    db.execute_sql(
        'ALTER TABLE "tasks" ADD COLUMN "review_of" INTEGER '
        'REFERENCES "tasks" ("id") ON DELETE CASCADE'
    )
    db.execute_sql('CREATE INDEX IF NOT EXISTS "task_review_of" ON "tasks" ("review_of")')
    db.execute_sql('ALTER TABLE "tasks" ADD COLUMN "review_outcome" VARCHAR(32)')


def down(migrator, db):
    """Drop the index and both columns."""
    db.execute_sql('DROP INDEX IF EXISTS "task_review_of"')
    migrator.drop_column('tasks', 'review_of').run()
    migrator.drop_column('tasks', 'review_outcome').run()
