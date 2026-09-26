"""Add SQLite FTS5 full-text search indexes for key tables.

This migration creates 4 FTS5 virtual tables to enable efficient full-text search:
- docs_fts: index docs.content
- messages_fts: index messages.payload
- events_fts: index events.body
- tasks_fts: index tasks.title and tasks.description

Each FTS table is populated with existing data from its source table, and AFTER
INSERT/UPDATE/DELETE triggers are created to keep each index in sync with the
source table.

CREATE TABLE IF NOT EXISTS / CREATE TRIGGER IF NOT EXISTS make the schema
half of this migration safe to re-run. The populate step is made idempotent
with the FTS5 'rebuild' special command, which discards and rebuilds the
index from the content table - not with INSERT OR IGNORE, which cannot
dedupe an FTS5 index (there is no unique constraint for it to conflict on),
so a second run under that scheme silently double-indexed every row.

This migration is safe to run on both new and existing databases.
"""
from peewee import *


def up(migrator, db):
    """Create FTS5 virtual tables and sync triggers."""

    # Enable foreign keys off to avoid issues during table operations
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
        with db.atomic():
            # 1. Create docs_fts FTS5 virtual table
            db.execute_sql("""
                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts
                USING fts5(
                    key UNINDEXED,
                    content,
                    content=docs,
                    content_rowid=rowid
                )
            """)

            # Populate docs_fts with existing data. 'rebuild' is the documented
            # external-content way to (re)populate an FTS5 index from its
            # content table: it discards whatever the index currently holds
            # and rebuilds from content, so it is idempotent on re-runs.
            # `INSERT OR IGNORE ... SELECT` is NOT idempotent here - FTS5 has
            # no unique constraint on rowid for OR IGNORE to conflict against,
            # so a second run silently double-indexes every row instead of
            # skipping it.
            db.execute_sql("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")

            # Create triggers to keep docs_fts in sync. External content FTS5
            # tables must be written to through the special 'delete' command
            # (see https://www.sqlite.org/fts5.html#external_content_tables) -
            # a plain DELETE/UPDATE against the shadow table corrupts the index
            # because it can no longer see the old column values to remove.
            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS docs_fts_insert
                AFTER INSERT ON docs BEGIN
                    INSERT INTO docs_fts (rowid, key, content)
                    VALUES (new.rowid, new.key, new.content);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS docs_fts_delete
                AFTER DELETE ON docs BEGIN
                    INSERT INTO docs_fts (docs_fts, rowid, key, content)
                    VALUES ('delete', old.rowid, old.key, old.content);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS docs_fts_update
                AFTER UPDATE ON docs BEGIN
                    INSERT INTO docs_fts (docs_fts, rowid, key, content)
                    VALUES ('delete', old.rowid, old.key, old.content);
                    INSERT INTO docs_fts (rowid, key, content)
                    VALUES (new.rowid, new.key, new.content);
                END
            """)

            # 2. Create messages_fts FTS5 virtual table
            db.execute_sql("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(
                    payload,
                    msg_type UNINDEXED,
                    sender UNINDEXED,
                    content=messages,
                    content_rowid=id
                )
            """)

            # Populate messages_fts with existing data. See docs_fts above for
            # why 'rebuild' replaces the non-idempotent INSERT OR IGNORE.
            db.execute_sql("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

            # Create triggers to keep messages_fts in sync
            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS messages_fts_insert
                AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts (rowid, payload, msg_type, sender)
                    VALUES (new.id, new.payload, new.msg_type, new.sender);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS messages_fts_delete
                AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts (messages_fts, rowid, payload, msg_type, sender)
                    VALUES ('delete', old.id, old.payload, old.msg_type, old.sender);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS messages_fts_update
                AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts (messages_fts, rowid, payload, msg_type, sender)
                    VALUES ('delete', old.id, old.payload, old.msg_type, old.sender);
                    INSERT INTO messages_fts (rowid, payload, msg_type, sender)
                    VALUES (new.id, new.payload, new.msg_type, new.sender);
                END
            """)

            # 3. Create events_fts FTS5 virtual table
            db.execute_sql("""
                CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
                USING fts5(
                    body,
                    label UNINDEXED,
                    kind UNINDEXED,
                    content=events,
                    content_rowid=id
                )
            """)

            # Populate events_fts with existing data. See docs_fts above for
            # why 'rebuild' replaces the non-idempotent INSERT OR IGNORE.
            db.execute_sql("INSERT INTO events_fts(events_fts) VALUES('rebuild')")

            # Create triggers to keep events_fts in sync
            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS events_fts_insert
                AFTER INSERT ON events BEGIN
                    INSERT INTO events_fts (rowid, body, label, kind)
                    VALUES (new.id, new.body, new.label, new.kind);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS events_fts_delete
                AFTER DELETE ON events BEGIN
                    INSERT INTO events_fts (events_fts, rowid, body, label, kind)
                    VALUES ('delete', old.id, old.body, old.label, old.kind);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS events_fts_update
                AFTER UPDATE ON events BEGIN
                    INSERT INTO events_fts (events_fts, rowid, body, label, kind)
                    VALUES ('delete', old.id, old.body, old.label, old.kind);
                    INSERT INTO events_fts (rowid, body, label, kind)
                    VALUES (new.id, new.body, new.label, new.kind);
                END
            """)

            # 4. Create tasks_fts FTS5 virtual table
            db.execute_sql("""
                CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts
                USING fts5(
                    title,
                    description,
                    content=tasks,
                    content_rowid=id
                )
            """)

            # Populate tasks_fts with existing data. See docs_fts above for
            # why 'rebuild' replaces the non-idempotent INSERT OR IGNORE.
            db.execute_sql("INSERT INTO tasks_fts(tasks_fts) VALUES('rebuild')")

            # Create triggers to keep tasks_fts in sync
            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS tasks_fts_insert
                AFTER INSERT ON tasks BEGIN
                    INSERT INTO tasks_fts (rowid, title, description)
                    VALUES (new.id, new.title, new.description);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS tasks_fts_delete
                AFTER DELETE ON tasks BEGIN
                    INSERT INTO tasks_fts (tasks_fts, rowid, title, description)
                    VALUES ('delete', old.id, old.title, old.description);
                END
            """)

            db.execute_sql("""
                CREATE TRIGGER IF NOT EXISTS tasks_fts_update
                AFTER UPDATE ON tasks BEGIN
                    INSERT INTO tasks_fts (tasks_fts, rowid, title, description)
                    VALUES ('delete', old.id, old.title, old.description);
                    INSERT INTO tasks_fts (rowid, title, description)
                    VALUES (new.id, new.title, new.description);
                END
            """)
    finally:
        db.execute_sql("PRAGMA foreign_keys=ON")


def down(migrator, db):
    """Drop all FTS5 virtual tables and their associated triggers.

    SQLite automatically drops triggers when a virtual table is dropped
    (cascading behavior), so we only need to drop the virtual tables.
    However, we explicitly drop triggers first for clarity and to handle
    any edge cases where the automatic cascade may not occur.
    """
    db.execute_sql("PRAGMA foreign_keys=OFF")
    try:
        with db.atomic():
            # Drop all triggers explicitly
            triggers = [
                'docs_fts_insert', 'docs_fts_delete', 'docs_fts_update',
                'messages_fts_insert', 'messages_fts_delete', 'messages_fts_update',
                'events_fts_insert', 'events_fts_delete', 'events_fts_update',
                'tasks_fts_insert', 'tasks_fts_delete', 'tasks_fts_update',
            ]
            for trigger in triggers:
                db.execute_sql(f'DROP TRIGGER IF EXISTS "{trigger}"')

            # Drop all FTS5 virtual tables
            fts_tables = ['docs_fts', 'messages_fts', 'events_fts', 'tasks_fts']
            for table in fts_tables:
                db.execute_sql(f'DROP TABLE IF EXISTS "{table}"')
    finally:
        db.execute_sql("PRAGMA foreign_keys=ON")
