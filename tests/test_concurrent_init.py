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


def test_model_binding_is_per_thread(tmp_path):
    """Two threads, each with its own database, must never see each other's
    binding: every row lands in the file its thread connected to."""
    import threading

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from kuska import store
    from kuska.db import connect, init_db

    n = 300
    dbs = {}
    for name in ("a", "b"):
        dbs[name] = connect(str(tmp_path / f"{name}.db"))
        init_db(dbs[name])
    errors = []

    def work(name):
        try:
            for i in range(n):
                store.add_task(dbs[name], f"{name}-{i}", "")
        except Exception as e:  # noqa: BLE001 - the test reports any failure
            errors.append((name, e))

    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=work, args=(k,)) for k in dbs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)

    assert not errors, errors
    for name, db in dbs.items():
        titles = [r[0] for r in db.execute_sql("SELECT title FROM tasks").fetchall()]
        assert len(titles) == n, (name, len(titles))
        assert all(t.startswith(f"{name}-") for t in titles), name
