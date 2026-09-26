#!/usr/bin/env python3
"""Test concurrent database initialization without write lock."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def test_concurrent_db_init():
    """Verify that multiple processes can initialize the same database
    concurrently without "database is locked" errors.

    This was the main reason for the cross-process write lock - without it,
    we need to verify that WAL + busy_timeout is sufficient.
    """

    # Create a temporary database file
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        # Script to run in each process
        init_script = f"""
import sys
sys.path.insert(0, '{Path(__file__).parent.parent / "src"}')

from kuska.db import connect, init_db

try:
    db = connect('{db_path}')
    init_db(db)
    print("OK")
    sys.exit(0)
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    import traceback
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
"""

        # Run 4 processes simultaneously
        processes = []
        for i in range(4):
            proc = subprocess.Popen(
                [sys.executable, '-c', init_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            processes.append(proc)

        # Wait for all to complete and check results
        all_ok = True
        for i, proc in enumerate(processes):
            stdout, stderr = proc.communicate(timeout=10)
            if proc.returncode != 0:
                print(f"✗ Process {i} failed:")
                print(f"  stdout: {stdout}")
                print(f"  stderr: {stderr}")
                all_ok = False
            elif "OK" not in stdout:
                print(f"✗ Process {i} didn't return OK:")
                print(f"  stdout: {stdout}")
                all_ok = False
            else:
                print(f"✓ Process {i} succeeded")

        return all_ok

    finally:
        # Clean up
        if os.path.exists(db_path):
            os.remove(db_path)
        for suffix in ['-shm', '-wal']:
            wal_path = db_path + suffix
            if os.path.exists(wal_path):
                os.remove(wal_path)


def main():
    print("concurrent database initialization")

    if test_concurrent_db_init():
        print("  ok   4 processes initialized simultaneously without lock")
        return 0
    else:
        print("  FAIL concurrent initialization failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
