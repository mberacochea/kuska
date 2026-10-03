#!/usr/bin/env python3
"""Run every check: `uv run tests/run_all.py [pytest args]`."""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=root))
