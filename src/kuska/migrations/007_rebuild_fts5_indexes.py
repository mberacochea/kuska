"""Rebuild all FTS5 indexes to repair any desync from prior INSERT OR REPLACE operations.

Migration 005 created FTS5 indexes with DELETE triggers that only fire when
PRAGMA recursive_triggers is ON (it defaults OFF). The previous implementation
of docs_set() used INSERT OR REPLACE, which updates via a delete-then-insert
that SQLite doesn't recognize as a trigger-worthy DELETE on recursive_triggers=OFF.
This left orphaned entries in the FTS5 indexes.

This migration unconditionally rebuilds all four FTS5 tables (docs_fts, messages_fts,
events_fts, tasks_fts) to ensure they agree with their content tables. The rebuild
is idempotent and safe to run on any database:
- Fresh databases: rebuilds from scratch (harmless).
- Databases with prior desync: removes orphans and restores completeness.
- Databases with no desync: verifies and refills (fast, no data change).

This is a one-shot repair, not a continuous fix - the root cause was docs_set's
use of INSERT OR REPLACE, which is now fixed in store.py. This migration ensures
any existing corruption is cleaned up.
"""
from peewee import *


def up(migrator, db):
    """Rebuild all FTS5 indexes."""
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
        with db.atomic():
            fts_tables = ['docs_fts', 'messages_fts', 'events_fts', 'tasks_fts']
            for table in fts_tables:
                db.execute_sql(f"INSERT INTO {table}({table}) VALUES('rebuild')")
    finally:
        db.execute_sql("PRAGMA foreign_keys=ON")


def down(migrator, db):
    """Rebuild again (same operation, can't truly reverse a rebuild)."""
    # Rebuilding is idempotent; a rebuild is its own reverse
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
        with db.atomic():
            fts_tables = ['docs_fts', 'messages_fts', 'events_fts', 'tasks_fts']
            for table in fts_tables:
                db.execute_sql(f"INSERT INTO {table}({table}) VALUES('rebuild')")
    finally:
        db.execute_sql("PRAGMA foreign_keys=ON")
