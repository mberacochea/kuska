"""Per-backend daemons.

Each backend module supplies only the model call - `make_runner()` - and
`run_daemon()` hands it to the loop they all share, in loop.py.
"""

from __future__ import annotations

import importlib

# Module paths, not modules: a daemon runs exactly one backend, so only that
# backend's SDK (claude_agent_sdk, openai_codex, openai) is imported, and a
# broken or missing SDK for another backend cannot stop this agent starting.
# Do not turn these back into eager imports. Because they are reached through
# importlib, kuska.spec lists them as PyInstaller hidden imports.
BACKENDS = {
    "claude": "kuska.daemons.claude",
    "codex": "kuska.daemons.codex",
    "openai": "kuska.daemons.openai",
}


def run(backend: str, project, agent_name: str, poll_interval: float = 2.0, max_tasks=None, quiet: bool = False, stop=None, worker=None) -> None:
    """Start the daemon for one agent, picked by its configured backend."""
    if backend not in BACKENDS:
        raise SystemExit(f"no daemon for backend '{backend}' (have: {', '.join(BACKENDS)})")
    importlib.import_module(BACKENDS[backend]).run_daemon(project, agent_name, poll_interval=poll_interval, max_tasks=max_tasks, quiet=quiet, stop=stop, worker=worker)
