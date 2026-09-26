"""Add feature grouping (feature column) to tasks.

This migration adds a nullable feature TEXT column to the tasks table so a
task can belong to exactly one free-text feature group, plus an index for
the filter dropdown and group-by queries.

Uses peewee's migrator API for proper schema management.
"""
from peewee import *


def up(migrator, db):
    """Add feature column and its index to the tasks table."""
    op = migrator.add_column('tasks', 'feature', CharField(null=True))
    op.run()
    migrator.add_index('tasks', ('feature',), False).run()


def down(migrator, db):
    """Remove the feature index and column.

    Note: SQLite has limited ALTER TABLE support and doesn't natively support
    dropping columns. The migrator handles this by recreating the table without
    the column if necessary, preserving all other data.
    """
    migrator.drop_index('tasks', 'tasks_feature').run()
    op = migrator.drop_column('tasks', 'feature')
    op.run()
