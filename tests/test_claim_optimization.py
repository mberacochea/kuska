#!/usr/bin/env python3
"""Benchmark for claim_files() optimization.

Measures the reduction in database queries when claiming multiple files.
The optimization pre-fetches all active claims once, rather than querying
for each file separately.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

import kuska as ac

# Track database query count
query_count = 0
original_execute = None


def patch_query_counter():
    """Patch Peewee's execute to count queries."""
    global original_execute
    from peewee import Database

    original_execute = Database.execute_sql

    def counted_execute(self, sql, *args, **kwargs):
        global query_count
        if "SELECT" in sql.upper() and "FileClaim" in sql:
            query_count += 1
        return original_execute(self, sql, *args, **kwargs)

    Database.execute_sql = counted_execute


def unpatch_query_counter():
    """Restore original execute method."""
    global original_execute
    from peewee import Database
    Database.execute_sql = original_execute


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kuska-bench-"))
    try:
        project = tmp / "benchproj"
        (project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(project).write_text(
            '[agents.dev-agent]\nbackend = "claude"\n'
            '[agents.other-agent]\nbackend = "claude"\n'
        )
        conn = ac.connect(ac.db_path(project))
        ac.init_db(conn)

        # Setup agents
        names = ac.sync_agents_from_config(conn, project)
        ac.heartbeat(conn, "dev-agent", "working")
        ac.heartbeat(conn, "other-agent", "working")

        print("Benchmark: claim_files() with multiple files")
        print("=" * 60)

        # Create some initial claims to populate active_claims
        print("\nSetup: Creating baseline claims...")
        ac.claim_files(conn, "other-agent", [
            "src/baseline_1.py",
            "src/baseline_2.py",
            "src/baseline_3.py",
            "src/baseline_4.py",
            "src/baseline_5.py",
        ], run_id="setup-run")

        # Now benchmark claiming multiple files
        file_counts = [5, 10, 20]

        for num_files in file_counts:
            # Release previous claims
            ac.release_files(conn, "dev-agent")

            # Generate file list to claim
            paths = [f"src/file_{i:03d}.py" for i in range(num_files)]

            # Reset query counter
            query_count = 0
            patch_query_counter()

            start = time.time()
            result = ac.claim_files(conn, "dev-agent", paths, run_id="benchmark-run")
            elapsed = time.time() - start

            unpatch_query_counter()

            num_queries = query_count

            # With optimization: should be ~3-4 queries total
            # Without optimization: would be ~N queries (1 per file)
            print(f"\nClaiming {num_files:2d} files:")
            print(f"  Time:      {elapsed*1000:.2f} ms")
            print(f"  Queries:   {num_queries} (ideal: ~3-4)")
            print(f"  Efficiency: {num_files / max(num_queries, 1):.1f} files/query")

            if num_queries > 10:
                print("  ⚠️  High query count - optimization may not be working!")

        # Verify correctness
        print("\n" + "=" * 60)
        print("Correctness check:")
        all_claims = ac.active_claims(conn)
        print(f"  Active claims: {len(all_claims)}")

        # Test that conflict detection still works
        ac.release_files(conn, "dev-agent")
        ac.release_files(conn, "other-agent")

        ac.claim_files(conn, "dev-agent", ["src/conflict.py"], run_id="r1")
        result = ac.claim_files(conn, "other-agent", ["src/conflict.py"], run_id="r2")

        if result["held_by_others"]:
            print(f"  ✓ Conflict detection works: {len(result['held_by_others'])} conflict(s) found")
        else:
            print("  ✗ Conflict detection broken!")
            sys.exit(1)

        # Test directory overlap detection
        ac.release_files(conn, "dev-agent")
        ac.release_files(conn, "other-agent")

        ac.claim_files(conn, "dev-agent", ["src/lib"], run_id="r3")
        overlapping = ac.claim_holders(conn, "src/lib/util.py", agent="other-agent")

        if overlapping:
            print("  ✓ Directory overlap detection works")
        else:
            print("  ✗ Directory overlap detection broken!")
            sys.exit(1)

        print("\n" + "=" * 60)
        print("✓ All benchmarks passed")

        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
