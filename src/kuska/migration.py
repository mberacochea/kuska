"""Database migration framework for schema evolution using peewee's playhouse.migrations.

Migrations are stored in the migrations/ directory as Python modules with
up(migrator, db) and down(migrator, db) functions. Each migration is tracked
in the schema_migration table to ensure we don't apply them twice.

This system uses peewee's built-in migration runner from playhouse.migrations.

Usage:
    from kuska.migration import run_migrations, get_current_version, list_migrations

    # Apply all pending migrations
    run_migrations(db)

    # Check current version
    version = get_current_version(db)
    print(f"Current schema version: {version}")

    # List available migrations
    migrations = list_migrations()
    for name in migrations:
        print(f"  {name}")
"""

from __future__ import annotations

import os
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only
    fcntl = None

from pathlib import Path

from peewee import SqliteDatabase
from playhouse.migrations import MigrationError, Runner


class _MigrationLock:
    """Lightweight filesystem lock for migration serialization across processes.

    Only used for migrations, not for all database operations (unlike the old
    cross-process write lock). This ensures that when multiple kuska processes
    (serve/mcp/daemon) start simultaneously and call init_db(), only one
    performs migrations at a time, while others wait their turn.
    """

    def __init__(self, db_path: str):
        # Lock file: <db_path>.migration.lock
        self._lock_path = f"{db_path}.migration.lock" if db_path and db_path != ":memory:" else ""
        self._fd = None

    def __enter__(self):
        if fcntl is None or not self._lock_path:
            return self
        self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _get_migrations_dir() -> Path:
    """Get the migrations directory."""
    return Path(__file__).parent / "migrations"


def _get_runner(db: SqliteDatabase) -> Runner:
    """Create a migration runner for the given database."""
    return Runner(
        db,
        directory=str(_get_migrations_dir()),
        table_name='schema_migration',  # peewee default
    )


def list_migrations() -> list[str]:
    """List all available migrations in order.

    Returns a list of migration names (e.g., ['001_initial_schema', '002_add_soft_delete'])
    """
    runner = _get_runner(SqliteDatabase(':memory:'))  # dummy db just to get the list
    migrations = runner.migrations()
    return [m.name for m in migrations]


def get_current_version(db: SqliteDatabase) -> str | None:
    """Get the current schema version that has been applied.

    Returns the name of the last applied migration, or None if no
    migrations have been applied yet.
    """
    runner = _get_runner(db)
    applied = runner.applied()

    if not applied:
        return None

    # Return the last (highest) migration name applied
    return sorted(applied.keys())[-1]


def run_migrations(
    db: SqliteDatabase,
    target_version: str | None = None,
) -> list[str]:
    """Apply pending migrations to the database.

    Migrations are protected by a filesystem lock to ensure only one process
    runs them at a time, even when multiple kuska processes start simultaneously.

    Args:
        db: SqliteDatabase instance
        target_version: Optional migration name to migrate to. If not specified,
                       applies all pending migrations.

    Returns:
        List of applied migration names.

    Raises:
        MigrationError if a migration is malformed or fails.
    """
    # Get the database path to create a migration-specific lock
    # The database attribute contains the filename string
    db_path = db.database

    lock = _MigrationLock(db_path)
    with lock:
        runner = _get_runner(db)

        try:
            # Run migrations up to the target, or all if no target specified
            applied = runner.up(target=target_version)
            return applied
        except MigrationError as e:
            raise RuntimeError(f"Migration failed: {e}") from e


def rollback_migration(db: SqliteDatabase, migration_name: str | None = None) -> list[str]:
    """Rollback (undo) migrations.

    Args:
        db: SqliteDatabase instance
        migration_name: Optional migration name to rollback to. If not specified,
                       rolls back the most recent migration.

    Returns:
        List of rolled back migration names.

    Raises:
        MigrationError if the migration doesn't exist or rollback fails.
    """
    runner = _get_runner(db)

    try:
        # Rollback the most recent, or to the target if specified
        reverted = runner.down(target=migration_name)
        return reverted
    except MigrationError as e:
        raise RuntimeError(f"Rollback failed: {e}") from e


def init_schema(db: SqliteDatabase) -> None:
    """Initialize the database schema and run all pending migrations.

    This is called during project initialization. It creates the schema_migration
    table and runs all migrations to bring the database up to the current version.

    Args:
        db: SqliteDatabase instance
    """
    # Run all pending migrations
    run_migrations(db)
