"""Command line entry point - process management and one-off export only.

Task and plan authoring lives in the web app, not here:

    kuska init              # create .agents/, project.db, default config.toml
    kuska serve             # Flask + HTMX web UI (project + agents pages)
    kuska daemon <name>     # run one agent's daemon (backend from config.toml)
    kuska mcp               # MCP stdio server, for Codex / other external clients
    kuska run-all           # run web server + MCP server + all agents together
    kuska export            # one-off markdown export
    kuska migrate           # manage database migrations
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
    DEFAULT_CONFIG,
    REGISTRY,
    agent_config,
    config_path,
    db_path,
    find_project,
    registry_add,
    sync_agents_from_config,
)
from .store import docs_get, docs_set


def cmd_init(args: argparse.Namespace) -> None:
    project = Path(args.path or os.getcwd()).resolve()
    (project / ".agents" / "prompts").mkdir(parents=True, exist_ok=True)

    cfg = config_path(project)
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG)
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
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


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

    p_migrate = sub.add_parser("migrate", help="manage database migrations")
    p_migrate_group = p_migrate.add_mutually_exclusive_group()
    p_migrate_group.add_argument(
        "--status", action="store_true", help="show migration status"
    )
    p_migrate_group.add_argument(
        "--to", metavar="VERSION", help="migrate to specific version (e.g., 003)"
    )
    p_migrate.set_defaults(func=cmd_migrate)

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
