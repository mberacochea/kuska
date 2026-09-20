#!/usr/bin/env python3
"""Test cost anomaly detection.

Cost rather than token volume: cache reads are priced an order of magnitude
below fresh input, so a token total mixes two prices and compares nothing.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import kuska as ac

PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kuska-token-test-"))
    try:
        project = tmp / "myproject"
        (project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(project).write_text(
            '[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n'
        )
        conn = ac.connect(ac.db_path(project))
        ac.init_db(conn)

        print("cost anomaly detection")

        # Register agent
        ac.register_agent(conn, "dev-agent", "claude", "builder")
        ac.heartbeat(conn, "dev-agent", "idle")

        # Create baseline tasks with normal token usage
        print("  creating baseline tasks...")
        for i in range(10):
            task_id = ac.add_task(conn, f"Task {i}", assigned_to="dev-agent")
            # Simulate a typical completion: mostly cache reads, one cent
            ac.send_message(
                conn,
                sender="dev-agent",
                recipient="human",
                task_id=task_id,
                msg_type="result",
                payload=f"Completed task {i}",
                input_tokens=1000,
                output_tokens=4000,
                cache_read_tokens=40000,
                tool_rounds=12,
                cost_usd=0.01,
            )

        # Check rolling average (should be around $0.01)
        avg = ac.calculate_rolling_cost_average(conn, window_size=10)
        check("rolling average calculated", avg > 0, f"avg={avg}")
        check("rolling average in expected range", 0.005 < avg < 0.015, f"avg={avg}")

        # Create a task that actually cost far more
        print("  creating anomalous task...")
        anomaly_task_id = ac.add_task(conn, "Expensive Task", assigned_to="dev-agent")
        ac.send_message(
            conn,
            sender="dev-agent",
            recipient="human",
            task_id=anomaly_task_id,
            msg_type="result",
            payload="Task that burned through round-trips",
            input_tokens=50000,
            output_tokens=50000,
            cache_read_tokens=3000000,
            tool_rounds=90,
            cost_usd=0.1,
        )

        # Check for anomaly (should detect $0.10 >> $0.01 average)
        result = ac.check_cost_anomaly(
            conn,
            task_id=anomaly_task_id,
            cost_usd=0.1,
            anomaly_threshold=2.0,
            window_size=10,
        )

        check("anomaly detected", result["is_anomaly"], f"result={result}")
        check("anomaly cost recorded", result["cost_usd"] == 0.1, f"cost={result['cost_usd']}")
        check("anomaly multiplier > 2x", result["multiplier"] > 2.0, f"multiplier={result['multiplier']}")
        check("anomaly event logged", result["event_id"] is not None, f"event_id={result['event_id']}")

        # Verify warning event was logged
        events = ac.recent_events(conn, limit=5)
        check("warning event in recent events", any(e["kind"] == "warning" for e in events),
              f"events={[e['kind'] for e in events]}")

        # A run at the average cost must not be flagged, however many tokens it
        # moved through cache - that is the whole point of costing it.
        normal_task_id = ac.add_task(conn, "Normal Task", assigned_to="dev-agent")
        result2 = ac.check_cost_anomaly(
            conn,
            task_id=normal_task_id,
            cost_usd=0.011,
            anomaly_threshold=2.0,
            window_size=10,
        )

        check("normal cost not flagged", not result2["is_anomaly"], f"result={result2}")
        check("normal cost no event", result2["event_id"] is None, f"event_id={result2['event_id']}")

        print(f"\nPassed: {PASSED} checks")
        return 0

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
