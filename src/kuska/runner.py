"""Run multiple services together: web server, MCP server, and agent daemons.

This is useful for:
  - Development: all services in one command
  - Production: single binary with everything running
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

from .daemons import run as run_daemon
from .db import connect, init_db
from .project import agent_config, config_path, db_path, find_project


def run_all(
    project_path: str | Path | None = None,
    agents: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 5055,
    poll_interval: float = 2.0,
) -> None:
    """Run web server, MCP server, and agent daemons together.

    Args:
        project_path: Project directory (default: nearest .agents/)
        agents: List of agent names to run, or None for all configured agents, or ["*"]
        host: Web server host (default: 127.0.0.1)
        port: Web server port (default: 5055)
        poll_interval: Daemon poll interval in seconds
    """
    project = find_project(project_path)
    db = connect(db_path(project))
    init_db(db)

    # Get configured agents
    cfg_path = config_path(project)
    if not cfg_path.exists():
        raise SystemExit(f"No config.toml found at {cfg_path}")

    import tomllib

    with open(cfg_path, "rb") as f:
        config = tomllib.load(f)

    configured_agents = list(config.get("agents", {}).keys())
    if not configured_agents:
        raise SystemExit(f"No agents configured in {cfg_path}")

    # Determine which agents to run
    if agents is None:
        agents_to_run = configured_agents[:1]  # Run first agent by default
        print(f"No agents specified; running: {', '.join(agents_to_run)}")
    elif agents == ["*"]:
        agents_to_run = configured_agents
        print(f"Running all configured agents: {', '.join(agents_to_run)}")
    else:
        # Validate requested agents
        for agent in agents:
            if agent not in configured_agents:
                raise SystemExit(
                    f"Agent '{agent}' not configured. "
                    f"Available: {', '.join(configured_agents)}"
                )
        agents_to_run = agents

    db.close()

    # Shared state for graceful shutdown
    stop_event = threading.Event()
    exceptions: list[Exception] = []
    threads: list[threading.Thread] = []

    def signal_handler(sig, frame):
        print("\n[runner] Shutting down gracefully...")
        stop_event.set()
        # Give threads time to shut down
        time.sleep(0.5)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def run_web_server():
        """Run the Flask web server."""
        try:
            from .web import create_app

            print(f"[web] Starting on http://{host}:{port}")
            app = create_app(project)
            # Run without debug/reload to avoid subprocess issues
            app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        except Exception as e:
            exceptions.append(e)
            stop_event.set()

    def run_mcp_server():
        """Run the MCP stdio server."""
        try:
            from .mcp_server import run_mcp

            print("[mcp] Starting MCP server")
            run_mcp(project, "coordinator", Path(db_path(project)))
        except Exception as e:
            exceptions.append(e)
            stop_event.set()

    def run_agent_daemon(agent_name: str):
        """Run a single agent daemon."""
        try:
            cfg = agent_config(project, agent_name)
            backend = cfg.get("backend", "claude")
            print(f"[{agent_name}] Starting daemon (backend: {backend})")

            # Wrap to watch for stop_event
            while not stop_event.is_set():
                try:
                    run_daemon(
                        backend,
                        project,
                        agent_name,
                        poll_interval=poll_interval,
                        max_tasks=1,  # Process one task at a time
                        quiet=False,
                    )
                except KeyboardInterrupt:
                    break
                except (FileNotFoundError, ImportError) as exc:
                    print(f"[{agent_name}] Error: cannot start {backend} backend - {exc}")
                    stop_event.set()
                    break

                # Brief pause before polling again
                if not stop_event.wait(poll_interval):
                    continue
                else:
                    break
        except Exception as e:
            exceptions.append(e)
            stop_event.set()

    # Start web server in a thread
    print("[runner] Starting services...")
    web_thread = threading.Thread(target=run_web_server, name="web", daemon=True)
    web_thread.start()
    threads.append(web_thread)

    # Start MCP server in a thread
    mcp_thread = threading.Thread(target=run_mcp_server, name="mcp", daemon=True)
    mcp_thread.start()
    threads.append(mcp_thread)

    # Start agent daemons
    for agent_name in agents_to_run:
        agent_thread = threading.Thread(
            target=run_agent_daemon, args=(agent_name,), name=agent_name, daemon=True
        )
        agent_thread.start()
        threads.append(agent_thread)

    print("[runner] All services started. Press Ctrl+C to stop.\n")

    # Wait for threads
    try:
        while threads:
            # Check if any non-daemon thread is still alive
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                break
            time.sleep(0.5)

            # Check for exceptions
            if exceptions:
                raise exceptions[0]

            # Check if stop was requested
            if stop_event.is_set():
                break
    except KeyboardInterrupt:
        print("\n[runner] Interrupted")
        stop_event.set()
        time.sleep(1)

    # Wait for threads to finish
    for t in threads:
        t.join(timeout=2)

    if exceptions:
        print("[runner] Errors occurred:")
        for exc in exceptions:
            print(f"  - {exc}")
        sys.exit(1)

    print("[runner] Shutdown complete")
