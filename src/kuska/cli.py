"""Command line entry point - process management and one-off export only.

Task and plan authoring lives in the web app, not here:

    kuska init              # create .agents/, project.db, default config.toml
    kuska serve             # Flask + HTMX web UI (project + agents pages)
    kuska daemon <name>     # run one agent's daemon (backend from config.toml)
    kuska mcp               # MCP stdio server, for Codex / other external clients
    kuska run-all           # run web server + MCP server + all agents together
    kuska export            # one-off markdown export
    kuska migrate           # manage database migrations
    kuska doctor            # check (and --repair) database integrity
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import __version__
from .db import HUMAN, connect, init_db
from .export import export_markdown
from .migration import get_current_version, run_migrations
from .project import (
    REGISTRY,
    agent_config,
    config_path,
    db_path,
    default_config,
    default_prompt,
    find_project,
    load_config,
    merge_prompt,
    prompt_path,
    read_prompt,
    registry_add,
    sync_agents_from_config,
    write_prompt,
)
from .store import docs_get, docs_set


def cmd_init(args: argparse.Namespace) -> None:
    project = Path(args.path or os.getcwd()).resolve()
    (project / ".agents" / "prompts").mkdir(parents=True, exist_ok=True)

    cfg = config_path(project)
    if not cfg.exists():
        cfg.write_text(default_config())
        print(f"wrote {cfg}")

    db = connect(db_path(project))
    init_db(db)
    names = sync_agents_from_config(db, project)
    if docs_get(db, "description") is None:
        docs_set(db, "description", f"# {project.name}\n\n", HUMAN)
    db.close()

    registry_add(args.name or project.name, project)
    print(f"initialised {db_path(project)}")
    print(f"agents: {', '.join(names) if names else '(none yet - add them to config.toml)'}")
    print(f"registered as '{args.name or project.name}' in {REGISTRY}")
    print("\nnext: kuska serve")


def cmd_serve(args: argparse.Namespace) -> None:
    from .web import create_app

    project = find_project(args.project)
    app = create_app(project)
    print(f"serving {project} on http://{args.host}:{args.port}")
    # Single-threaded by design: the app holds one process-wide open project
    # in state["db"], and closing that handle while another thread queries it
    # causes an unhandled exception. Kuska is single-user, so serial request
    # handling is the correct fix, not a global lock.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=False)


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import run_mcp

    project = find_project(args.project)
    run_mcp(project, args.agent, Path(args.db) if args.db else None)


def cmd_daemon(args: argparse.Namespace) -> None:
    from .daemons import run

    project = find_project(args.project)
    cfg = agent_config(project, args.agent)
    backend = args.backend or cfg.get("backend", "claude")
    try:
        run(backend, project, args.agent, args.poll_interval, args.max_tasks, args.quiet)
    except KeyboardInterrupt:
        print("\nstopped")
    except (FileNotFoundError, ImportError) as exc:
        # usually the backend CLI missing from PATH, which matters most in the
        # PyInstaller build, where the SDKs' vendored CLIs are left out
        raise SystemExit(f"{args.agent}: cannot start the {backend} backend - {exc}") from exc


def cmd_export(args: argparse.Namespace) -> None:
    project = find_project(args.project)
    out = Path(args.out) if args.out else project / ".agents-export"
    db = connect(db_path(project))
    init_db(db)
    written = export_markdown(db, out)
    db.close()
    for path in written:
        print(path)


def cmd_prompts(args: argparse.Namespace) -> None:
    """Merge agent prompts with the current template, or show diffs."""
    import difflib

    project = find_project(args.project)
    template = (Path(__file__).parent / "defaults" / "prompt.md").read_text()
    config = load_config(project)
    agents = config.get("agents", {})

    if not agents:
        print("no agents found in config.toml")
        return

    if args.write:
        # Merge and write
        for name, cfg in agents.items():
            existing = read_prompt(project, name)
            if not existing:
                # If no prompt exists, seed it fresh
                seeded = default_prompt(name, cfg.get("role", "a coding agent"))
                write_prompt(project, name, seeded)
            else:
                # Merge the existing prompt with the template
                merged = merge_prompt(existing, template)
                write_prompt(project, name, merged)
                print(f"merged {name}")
    else:
        # Show diffs (default behavior)
        for name, cfg in agents.items():
            existing = read_prompt(project, name)
            if not existing:
                merged = default_prompt(name, cfg.get("role", "a coding agent"))
            else:
                merged = merge_prompt(existing, template)

            # Show unified diff
            existing_lines = (existing or "").splitlines(keepends=True)
            merged_lines = merged.splitlines(keepends=True)

            diff = difflib.unified_diff(
                existing_lines,
                merged_lines,
                fromfile=f".agents/prompts/{name}.md (current)",
                tofile=f".agents/prompts/{name}.md (merged)",
                lineterm="",
            )
            diff_output = "".join(diff)
            if diff_output:
                print(f"\n{name}:")
                print(diff_output)
            else:
                print(f"{name}: no changes needed")


def cmd_migrate(args: argparse.Namespace) -> None:
    """Manage database migrations."""
    project = find_project(args.project)
    db = connect(db_path(project))

    try:
        if args.status:
            # Show current migration status using peewee migration runner
            from playhouse.migrations import Runner
            runner = Runner(
                db,
                directory=str(Path(__file__).parent / "migrations"),
                table_name='schema_migration',
            )

            status_migrations = runner.status()

            if not status_migrations:
                print("No migrations found")
                return

            current = get_current_version(db)
            print(f"Current schema version: {current if current else '(none - no migrations applied)'}")
            print("\nAvailable migrations:")

            for migration in status_migrations:
                status = "✓ applied" if migration.applied else "  pending"
                print(f"  {status}  {migration.name}")

        elif args.to:
            # Migrate to specific version
            applied = run_migrations(db, target_version=args.to)
            if applied:
                print(f"Applied migrations: {', '.join(applied)}")
                current = get_current_version(db)
                print(f"Current schema version: {current}")
            else:
                print(f"No new migrations to apply (already at or past version {args.to})")

        else:
            # Apply all pending migrations
            applied = run_migrations(db)
            if applied:
                print(f"Applied migrations: {', '.join(applied)}")
                current = get_current_version(db)
                print(f"Current schema version: {current}")
            else:
                print("No pending migrations to apply")

    finally:
        db.close()


# The four external-content FTS5 indexes migration 005 creates. Kept here
# rather than imported from the migration module since the migration is a
# one-shot script, not a place other code should import table names from.
_FTS_TABLES = ("tasks_fts", "docs_fts", "messages_fts", "events_fts")

# Map FTS table names to their content tables and primary key columns
_FTS_CONTENT_MAP = {
    "docs_fts": ("docs", "rowid"),
    "messages_fts": ("messages", "id"),
    "events_fts": ("events", "id"),
    "tasks_fts": ("tasks", "id"),
}


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check (and optionally repair) database integrity.

    Runs SQLite's own `PRAGMA integrity_check` over the whole file, then the
    FTS5 'integrity-check' special command on each of the four external-
    content indexes migration 005 created (tasks_fts, docs_fts, messages_fts,
    events_fts). Note: FTS5's integrity-check only validates the index's
    internal consistency, not that it agrees with the content table. A
    desynced FTS5 index - e.g. from orphaned entries left by INSERT OR REPLACE
    without recursive_triggers - can pass integrity-check while still missing
    rows, and surfaces later as `peewee.DatabaseError: database disk image is
    malformed` on an innocent write to the *content* table (tasks, docs, ...).

    This command also performs a row-count comparison between each FTS index
    and its content table to detect desync that integrity-check misses.

    --repair rebuilds any FTS index that fails integrity-check or row-count
    validation, via the FTS5 'rebuild' command (a full re-index from the
    content table - safe and idempotent). It does NOT repair a failing PRAGMA
    integrity_check: that means the main database file itself is damaged, and
    needs a restore from backup, not a FTS rebuild.
    """
    project = find_project(args.project)
    db = connect(db_path(project))
    problems: list[str] = []
    try:
        rows = db.execute_sql("PRAGMA integrity_check").fetchall()
        main_ok = len(rows) == 1 and rows[0][0] == "ok"
        if main_ok:
            print("database: ok (PRAGMA integrity_check)")
        else:
            print("database: PROBLEMS FOUND (PRAGMA integrity_check) -")
            for (line,) in rows:
                print(f"  {line}")
            print("  this is main-database-file damage, not an index problem;")
            print("  `kuska doctor --repair` cannot fix it - restore from backup.")

        fts_broken = []
        for table in _FTS_TABLES:
            is_broken = False

            # Check 1: FTS5 internal consistency
            try:
                db.execute_sql(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
            except Exception as exc:
                is_broken = True
                print(f"{table}: CORRUPT (integrity-check failed) - {exc}")
                print(f"  index internal consistency failed; run `kuska doctor --repair` to rebuild it.")

            # Check 2: Row count mismatch (detects orphaned entries and missing rows)
            if not is_broken:
                content_table, pk_col = _FTS_CONTENT_MAP[table]
                fts_count = db.execute_sql(f"SELECT count(*) FROM {table}").fetchone()[0]
                content_count = db.execute_sql(f"SELECT count(*) FROM {content_table}").fetchone()[0]
                if fts_count != content_count:
                    is_broken = True
                    print(f"{table}: DESYNC - index has {fts_count} rows, {content_table} has {content_count}")
                    print(f"  index is out of sync with its content table; run `kuska doctor --repair` to rebuild it.")

            if not is_broken:
                print(f"{table}: ok")

            if is_broken:
                fts_broken.append(table)

        if args.repair and fts_broken:
            print()
            for table in fts_broken:
                db.execute_sql(f"INSERT INTO {table}({table}) VALUES('rebuild')")
                print(f"{table}: rebuilt")
            print("\nre-checking after repair:")
            still_broken = []
            for table in fts_broken:
                try:
                    db.execute_sql(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
                    print(f"{table}: ok")
                except Exception as exc:
                    still_broken.append(table)
                    print(f"{table}: still CORRUPT - {exc}")
            problems = still_broken
        else:
            problems = fts_broken

        if not main_ok or problems:
            raise SystemExit(1)
        print("\nno problems found")
    finally:
        db.close()


def cmd_run_all(args: argparse.Namespace) -> None:
    """Run web server, MCP server, and agent daemons together."""
    from .runner import run_all

    agents = None
    if args.agents:
        # Parse comma-separated agent names, or special "*" for all
        if args.agents == "*":
            agents = ["*"]
        else:
            agents = [a.strip() for a in args.agents.split(",")]

    try:
        run_all(
            project_path=args.project,
            agents=agents,
            host=args.host,
            port=args.port,
            poll_interval=args.poll_interval,
        )
    except KeyboardInterrupt:
        print("\nstopped")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kuska", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"kuska {__version__}")
    parser.add_argument(
        "--project", help="project directory (default: nearest .agents/ at or above cwd)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create .agents/, project.db, default config.toml")
    p_init.add_argument("path", nargs="?", help="project directory (default: cwd)")
    p_init.add_argument("--name", help="name in the project registry (default: directory name)")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="Flask + HTMX web UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=5055)
    p_serve.add_argument("--debug", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_daemon = sub.add_parser("daemon", help="run one agent's daemon")
    p_daemon.add_argument("agent", help="agent name, as listed in .agents/config.toml")
    p_daemon.add_argument("--backend", help="override the backend from config.toml")
    p_daemon.add_argument("--poll-interval", type=float, default=2.0)
    p_daemon.add_argument("--max-tasks", type=int, help="exit after this many tasks")
    p_daemon.add_argument(
        "--quiet", action="store_true",
        help="do not narrate the agent's work on the terminal (still logged to the DB)",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_mcp = sub.add_parser("mcp", help="MCP stdio server for external clients")
    p_mcp.add_argument("--agent", default="codex", help="agent name these tools act as")
    p_mcp.add_argument("--db", help="explicit path to project.db")
    p_mcp.set_defaults(func=cmd_mcp)

    p_export = sub.add_parser("export", help="one-off markdown export")
    p_export.add_argument("--out", help="output directory (default: <project>/.agents-export)")
    p_export.set_defaults(func=cmd_export)

    p_prompts = sub.add_parser("prompts", help="merge agent prompts with the current template")
    p_prompts.add_argument(
        "--write", action="store_true",
        help="merge and write (default: print diffs)")
    p_prompts.set_defaults(func=cmd_prompts)

    p_migrate = sub.add_parser("migrate", help="manage database migrations")
    p_migrate_group = p_migrate.add_mutually_exclusive_group()
    p_migrate_group.add_argument(
        "--status", action="store_true", help="show migration status"
    )
    p_migrate_group.add_argument(
        "--to", metavar="VERSION", help="migrate to specific version (e.g., 003)"
    )
    p_migrate.set_defaults(func=cmd_migrate)

    p_doctor = sub.add_parser(
        "doctor", help="check database integrity (PRAGMA + FTS5), optionally repair"
    )
    p_doctor.add_argument(
        "--repair", action="store_true",
        help="rebuild any FTS5 index that fails its integrity-check",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_run_all = sub.add_parser(
        "run-all",
        help="run web server, MCP server, and agents together (for dev or production)",
    )
    p_run_all.add_argument(
        "--agents",
        help="comma-separated agent names, or '*' for all (default: first configured agent)",
    )
    p_run_all.add_argument("--host", default="127.0.0.1")
    p_run_all.add_argument("--port", type=int, default=5055)
    p_run_all.add_argument("--poll-interval", type=float, default=2.0)
    p_run_all.set_defaults(func=cmd_run_all)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
