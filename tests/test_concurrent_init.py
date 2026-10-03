"""Test concurrent database initialization without write lock."""

import subprocess
import sys
from pathlib import Path

INIT_SCRIPT = """
import sys
sys.path.insert(0, {src!r})

from kuska.db import connect, init_db

db = connect({db_path!r})
init_db(db)
print("OK")
"""


def test_concurrent_db_init(tmp_path):
    """Verify that multiple processes can initialize the same database
    concurrently without "database is locked" errors.

    This was the main reason for the cross-process write lock - without it,
    we need to verify that WAL + busy_timeout is sufficient.
    """
    script = INIT_SCRIPT.format(
        src=str(Path(__file__).parent.parent / "src"), db_path=str(tmp_path / "test.db")
    )

    # Run 4 processes simultaneously
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]

    for i, proc in enumerate(processes):
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"process {i} failed:\nstdout: {stdout}\nstderr: {stderr}"
        assert "OK" in stdout, f"process {i} didn't return OK: {stdout}"
