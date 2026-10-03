"""Run the not-yet-converted script suites as pytest tests.

Each script stops at its first failing check, so one case here is one suite,
not one check. `uv run pytest -k test_web` runs a single suite. Convert a
suite to plain pytest functions and drop it from here and from `collect_ignore`
in conftest.py.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent

LEGACY_SUITES = [
    "test_core.py",
    "test_daemon.py",
    "test_web.py",
    "test_worktree.py",
    "test_search.py",
    "test_guardrails.py",
    "test_concurrent_init.py",
    "eventfmt_test.py",
]


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


@pytest.mark.parametrize("name", LEGACY_SUITES, ids=LEGACY_SUITES)
def test_legacy_suite(name):
    result = subprocess.run(
        [sys.executable, str(HERE / name)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, (
        f"{name} exited {result.returncode}\n"
        f"--- stdout (last 60 lines) ---\n{_tail(result.stdout)}\n"
        f"--- stderr (last 60 lines) ---\n{_tail(result.stderr)}"
    )
