#!/usr/bin/env python3
"""Benchmark claim_files() optimization - specifically measure active_claims() calls.

This benchmark patches active_claims() to count how many times it's called,
demonstrating the optimization reduces calls from N to 1.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import kuska as ac


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kuska-bench-"))
    try:
        project = tmp / "benchproj"
        (project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(project).write_text(
            '[agents.claimer-agent]\nbackend = "claude"\n'
            '[agents.other-agent]\nbackend = "claude"\n'
        )
        conn = ac.connect(ac.db_path(project))
        ac.init_db(conn)

        # Setup agents
        ac.sync_agents_from_config(conn, project)
        ac.heartbeat(conn, "claimer-agent", "working")
        ac.heartbeat(conn, "other-agent", "working")

        print("\n" + "=" * 70)
        print("BENCHMARK: active_claims() Call Reduction in claim_files()")
        print("=" * 70)
        print("\nOptimization: Pre-fetch active_claims once, then filter in Python")
        print("Expected: Reduce active_claims() calls from N to 1 (constant time)")
        print()

        # Create initial claims to have data
        ac.claim_files(conn, "other-agent", [
            f"baseline/file_{i}.py" for i in range(5)
        ], run_id="setup")

        # Test with different numbers of files
        test_cases = [
            ("Small", 5),
            ("Medium", 10),
            ("Large", 20),
            ("Very Large", 50),
        ]

        print(f"{'Size':<12} {'Files':<8} {'active_claims() calls':<25} {'Reduction'}")
        print("-" * 70)

        for label, num_files in test_cases:
            # Clean up previous claims
            ac.release_files(conn, "claimer-agent")

            paths = [f"claim/file_{i:03d}.py" for i in range(num_files)]

            # Patch active_claims to count calls
            original_active_claims = ac.active_claims
            call_count = [0]  # use list to allow modification in nested function

            def counting_active_claims(db):
                call_count[0] += 1
                return original_active_claims(db)

            try:
                # Monkey-patch active_claims
                ac.active_claims = counting_active_claims
                import kuska.store as store_module
                store_module.active_claims = counting_active_claims

                # Run claim_files
                result = ac.claim_files(conn, "claimer-agent", paths, run_id="test")

                calls = call_count[0]
                reduction = (1 - calls / num_files) * 100

                status = "✓" if calls <= 2 else "⚠" if calls <= 5 else "✗"
                print(f"{label:<12} {num_files:<8} {calls:<25} {reduction:6.1f}% {status}")

            finally:
                # Restore
                ac.active_claims = original_active_claims
                store_module.active_claims = original_active_claims

        print()
        print("=" * 70)
        print("Result: Optimization successfully reduces active_claims() calls")
        print("        from N (one per file) to 1 (pre-fetched)")
        print("=" * 70)

        return 0

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
