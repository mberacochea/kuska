"""Database migration framework for schema evolution.

Migrations are stored in the migrations/ directory as Python modules with
up() and down() functions. Each migration is tracked in the schema_migrations
table to ensure we don't apply them twice.

Usage:
    from achka.migrations import run_migrations, get_current_version, list_migrations

    # Apply all pending migrations
    run_migrations(db)

    # Check current version
    version = get_current_version(db)
    print(f"Current schema version: {version}")

    # List available migrations
    migrations = list_migrations()
    for version, name in migrations:
        print(f"{version}: {name}")
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Callable

from peewee import SqliteDatabase


def list_migrations() -> list[tuple[str, str]]:
    """List all available migrations in order.

    Returns a list of (version, name) tuples, where version is the
    migration filename prefix (e.g., '001') and name is the human-readable
    description from the filename.

    Example: [('001', 'initial_schema'), ('002', 'add_event_table')]
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        return []

    migrations = []
    for f in sorted(migrations_dir.glob("*.py")):
        if f.name == "__init__.py":
            continue
        # Parse "001_initial_schema.py" -> ("001", "initial_schema")
        parts = f.stem.split("_", 1)
        if len(parts) == 2:
            version, name = parts
            migrations.append((version, name))

    return migrations


def _load_migration(version: str) -> tuple[Callable, Callable | None]:
    """Load a migration module and return its up() and down() functions.

    Args:
        version: Migration version string (e.g., '001')

    Returns:
        (up_func, down_func) tuple. down_func may be None if not defined.

    Raises:
        ImportError if the migration module is not found or missing up().
    """
    migrations_dir = Path(__file__).parent / "migrations"

    # Find the migration file matching this version
    migration_file = None
    for f in migrations_dir.glob(f"{version}_*.py"):
        migration_file = f
        break

    if not migration_file:
        raise ImportError(f"Migration {version} not found")

    # Import the module dynamically
    spec = importlib.util.spec_from_file_location(
        f"achka.migrations.{version}", migration_file
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration {version}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Get up() and down() functions
    if not hasattr(module, "up"):
        raise ImportError(f"Migration {version} missing up() function")

    up_func = getattr(module, "up")
    down_func = getattr(module, "down", None)

    return up_func, down_func


def get_current_version(db: SqliteDatabase) -> str | None:
    """Get the current schema version that has been applied.

    Returns the version of the last applied migration, or None if no
    migrations have been applied yet.
    """
    # Ensure the schema_migrations table exists
    _ensure_migrations_table(db)

    cursor = db.execute_sql(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _ensure_migrations_table(db: SqliteDatabase) -> None:
    """Create the schema_migrations table if it doesn't exist."""
    db.execute_sql("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        )
    """)


def run_migrations(
    db: SqliteDatabase,
    target_version: str | None = None,
) -> list[str]:
    """Apply pending migrations to the database.

    Args:
        db: SqliteDatabase instance
        target_version: Optional version to migrate to. If not specified,
                       applies all pending migrations up to the latest.

    Returns:
        List of applied migration versions.

    Raises:
        ImportError if a migration module is malformed or missing.
        Any exception from the migration's up() function.
    """
    _ensure_migrations_table(db)

    current_version = get_current_version(db)
    all_migrations = list_migrations()

    # Determine which migrations to run
    applied = []
    for version, name in all_migrations:
        # Skip if already applied
        if current_version and version <= current_version:
            continue

        # Stop if we've reached target version
        if target_version and version > target_version:
            break

        # Load and run the migration
        try:
            up_func, _ = _load_migration(version)
            up_func(db)

            # Record that this migration was applied
            import time
            db.execute_sql(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            applied.append(version)
        except Exception as e:
            # Roll back the transaction on error
            db.rollback()
            raise RuntimeError(f"Migration {version} failed: {e}") from e

    return applied


def rollback_migration(db: SqliteDatabase, version: str) -> None:
    """Rollback (undo) a migration by calling its down() function.

    Args:
        db: SqliteDatabase instance
        version: Version to rollback

    Raises:
        RuntimeError if the migration doesn't have a down() function
                    or if the down() function fails.
        ImportError if the migration is not found.
    """
    _ensure_migrations_table(db)

    _, down_func = _load_migration(version)
    if down_func is None:
        raise RuntimeError(f"Migration {version} does not support rollback (no down() function)")

    try:
        down_func(db)
        # Remove the migration record
        db.execute_sql("DELETE FROM schema_migrations WHERE version = ?", (version,))
    except Exception as e:
        db.rollback()
        raise RuntimeError(f"Rollback of migration {version} failed: {e}") from e


def init_schema(db: SqliteDatabase) -> None:
    """Initialize the database schema and run all pending migrations.

    This is called during project initialization. It creates the schema_migrations
    table and runs all migrations to bring the database up to the current version.

    Args:
        db: SqliteDatabase instance
    """
    _ensure_migrations_table(db)

    # If there are no migrations applied yet, we treat the current schema version as "000"
    # (before the first migration), and run all migrations from there.
    current = get_current_version(db)
    if current is None:
        # Run all migrations
        run_migrations(db)
