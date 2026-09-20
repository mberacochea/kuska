"""Per-backend daemons.

A different kind of code from the rest of the package - async SDK calls,
retries and token accounting - but every one of them is the same twenty-line
loop over the same core functions, with the model call swapped out.
"""

from __future__ import annotations

BACKENDS = {"claude": "achka.daemons.claude", "codex": "achka.daemons.codex", "openai": "achka.daemons.openai"}


def run(backend: str, project, agent_name: str, poll_interval: float = 2.0, max_tasks=None, quiet: bool = False) -> None:
    """Start the daemon for one agent, picked by its configured backend."""
    import importlib

    if backend not in BACKENDS:
        raise SystemExit(f"no daemon for backend '{backend}' (have: {', '.join(BACKENDS)})")
    module = importlib.import_module(BACKENDS[backend])
    module.run_daemon(project, agent_name, poll_interval=poll_interval, max_tasks=max_tasks, quiet=quiet)
