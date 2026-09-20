"""Initial schema - mirrors current Peewee models.

This migration creates the complete schema that the application currently
expects: agents, tasks, task_deps, messages, file_claims, docs, and events tables.
"""


def up(db):
    """Create the initial schema tables."""

    # Create agents table
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            backend TEXT,
            role TEXT,
            status TEXT NOT NULL DEFAULT 'offline',
            current_task_id INTEGER,
            last_heartbeat REAL
        )
    """)

    # Create tasks table
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            assigned_to TEXT,
            status TEXT NOT NULL DEFAULT 'todo',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (assigned_to) REFERENCES agents(name) ON DELETE SET NULL
        )
    """)

    # Create task_deps table (dependencies between tasks)
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS task_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            depends_on INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (depends_on) REFERENCES tasks(id) ON DELETE CASCADE,
            UNIQUE (task_id, depends_on)
        )
    """)

    # Create index for task_deps
    db.execute_sql("""
        CREATE INDEX IF NOT EXISTS task_deps_task_id_depends_on
        ON task_deps (task_id, depends_on)
    """)

    # Create messages table (audit log and cost ledger)
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            sender TEXT,
            recipient TEXT,
            task_id INTEGER,
            msg_type TEXT,
            payload TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            read_at REAL
        )
    """)

    # Create index for messages
    db.execute_sql("""
        CREATE INDEX IF NOT EXISTS messages_recipient_task_id
        ON messages (recipient, task_id)
    """)

    # Create file_claims table (advisory locks for file editing)
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS file_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            agent TEXT NOT NULL,
            task_id INTEGER,
            run_id TEXT,
            mode TEXT NOT NULL DEFAULT 'write',
            note TEXT,
            claimed_at REAL NOT NULL
        )
    """)

    # Create indexes for file_claims
    db.execute_sql("""
        CREATE INDEX IF NOT EXISTS file_claims_path
        ON file_claims (path)
    """)

    db.execute_sql("""
        CREATE INDEX IF NOT EXISTS file_claims_agent
        ON file_claims (agent)
    """)

    # Create docs table (shared project documentation)
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS docs (
            key TEXT PRIMARY KEY,
            content TEXT,
            updated_by TEXT,
            updated_at REAL NOT NULL
        )
    """)

    # Create events table (audit log of agent invocations)
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            agent TEXT,
            task_id INTEGER,
            run_id TEXT,
            kind TEXT,
            label TEXT,
            body TEXT
        )
    """)

    # Create indexes for events
    db.execute_sql("""
        CREATE INDEX IF NOT EXISTS events_task_id_id
        ON events (task_id, id)
    """)

    db.execute_sql("""
        CREATE INDEX IF NOT EXISTS events_run_id_id
        ON events (run_id, id)
    """)


def down(db):
    """Drop all schema tables (irreversible for data, but reverses schema changes)."""
    # Note: This removes all data, so it's only suitable for development/testing.
    # In production, down() migrations are often skipped or documented as destructive.

    # Drop tables in reverse order of dependencies
    db.execute_sql("DROP TABLE IF EXISTS events")
    db.execute_sql("DROP TABLE IF EXISTS docs")
    db.execute_sql("DROP TABLE IF EXISTS file_claims")
    db.execute_sql("DROP TABLE IF EXISTS messages")
    db.execute_sql("DROP TABLE IF EXISTS task_deps")
    db.execute_sql("DROP TABLE IF EXISTS tasks")
    db.execute_sql("DROP TABLE IF EXISTS agents")
