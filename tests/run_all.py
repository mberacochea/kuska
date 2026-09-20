#!/usr/bin/env python3
"""Run every check: `uv run tests/run_all.py`."""

import subprocess
import sys
from pathlib import Path

SUITES = ["test_core.py", "test_web.py", "test_daemon.py"]


def main() -> None:
    here = Path(__file__).parent
    failed = []
    for suite in SUITES:
        print(f"\n=== {suite} ===")
        if subprocess.run([sys.executable, str(here / suite)]).returncode:
            failed.append(suite)
    if failed:
        sys.exit(f"\nfailed: {', '.join(failed)}")
    print("\nall suites passed")


if __name__ == "__main__":
    main()
