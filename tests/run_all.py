#!/usr/bin/env python3
"""Run every check: `uv run tests/run_all.py`."""

import subprocess
import sys
from pathlib import Path

def main() -> None:
    here = Path(__file__).parent

    # Discover all test files: test_*.py or *_test.py (except benchmarks)
    test_files = sorted([
        f.name for f in here.glob("test_*.py")
    ]) + sorted([
        f.name for f in here.glob("*_test.py")
    ])

    # Remove duplicates and exclude benchmarks
    test_files = sorted(set(f for f in test_files if not f.startswith("benchmark_")))

    # Run all discovered test files
    failed = []
    for suite in test_files:
        print(f"\n=== {suite} ===")
        if subprocess.run([sys.executable, str(here / suite)]).returncode:
            failed.append(suite)

    if failed:
        sys.exit(f"\nfailed: {', '.join(failed)}")
    print("\nall suites passed")


if __name__ == "__main__":
    main()
