"""Add soft-delete support (deleted_at column) to tasks.

This migration adds a deleted_at column to the tasks table to support soft-deletes,
allowing tasks to be marked as deleted without actually removing them from the database.

Uses peewee's migrator API for proper schema management.
"""
from peewee import *


def up(migrator, db):
    """Add deleted_at column to tasks table for soft-delete support."""
    # Use peewee's migrator API to add the column
    # Migrator methods return Operation objects that need to be executed
    op = migrator.add_column('tasks', 'deleted_at', FloatField(null=True))
    op.run()


def down(migrator, db):
    """Remove deleted_at column.

    Note: SQLite has limited ALTER TABLE support and doesn't natively support
    dropping columns. The migrator handles this by recreating the table without
    the column if necessary, preserving all other data.
    """
    op = migrator.drop_column('tasks', 'deleted_at')
    op.run()
