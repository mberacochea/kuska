"""Add worktree_path column to tasks.

This migration adds a nullable worktree_path TEXT column to the tasks table
to store the path to the worktree for a task (if one exists).

Uses peewee's migrator API for proper schema management.
"""
from peewee import *


def up(migrator, db):
    """Add worktree_path column to the tasks table."""
    op = migrator.add_column('tasks', 'worktree_path', TextField(null=True))
    op.run()


def down(migrator, db):
    """Remove the worktree_path column.

    Note: SQLite has limited ALTER TABLE support and doesn't natively support
    dropping columns. The migrator handles this by recreating the table without
    the column if necessary, preserving all other data.
    """
    op = migrator.drop_column('tasks', 'worktree_path')
    op.run()
