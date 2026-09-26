"""Add tags support to tasks.

This migration adds a nullable tags TEXT column to the tasks table to store
comma-separated tags for filtering and grouping tasks.

Uses peewee's migrator API for proper schema management.
"""
from peewee import *


def up(migrator, db):
    """Add tags column to the tasks table."""
    op = migrator.add_column('tasks', 'tags', TextField(null=True))
    op.run()


def down(migrator, db):
    """Remove the tags column."""
    op = migrator.drop_column('tasks', 'tags')
    op.run()
