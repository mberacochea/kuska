"""Per-backend daemons.

Each backend module supplies only the model call - `make_runner()` - and
`run_daemon()` hands it to the loop they all share, in loop.py.
"""

from __future__ import annotations

from . import claude, codex, openai

BACKENDS = {"claude": claude, "codex": codex, "openai": openai}


def run(backend: str, project, agent_name: str, poll_interval: float = 2.0, max_tasks=None, quiet: bool = False) -> None:
    """Start the daemon for one agent, picked by its configured backend."""
    if backend not in BACKENDS:
        raise SystemExit(f"no daemon for backend '{backend}' (have: {', '.join(BACKENDS)})")
    BACKENDS[backend].run_daemon(project, agent_name, poll_interval=poll_interval, max_tasks=max_tasks, quiet=quiet)
