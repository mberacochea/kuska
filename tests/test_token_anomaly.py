"""Test cost anomaly detection.

Cost rather than token volume: cache reads are priced an order of magnitude
below fresh input, so a token total mixes two prices and compares nothing.
"""

import pytest

import kuska as ac


def _complete_task(conn, title, *, cost_usd, input_tokens, output_tokens,
                   cache_read_tokens, tool_rounds):
    task_id = ac.add_task(conn, title, assigned_to="dev-agent")
    ac.send_message(
        conn,
        sender="dev-agent",
        recipient="human",
        task_id=task_id,
        msg_type="result",
        payload=f"Completed {title}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        tool_rounds=tool_rounds,
        cost_usd=cost_usd,
    )
    return task_id


@pytest.fixture
def baseline(conn):
    """Ten typical completions: mostly cache reads, one cent each."""
    ac.heartbeat(conn, "dev-agent", "idle")
    for i in range(10):
        _complete_task(
            conn, f"Task {i}", cost_usd=0.01, input_tokens=1000,
            output_tokens=4000, cache_read_tokens=40000, tool_rounds=12,
        )
    return conn


@pytest.fixture
def anomaly(baseline):
    """A task that cost ten times the baseline, plus the check's result."""
    task_id = _complete_task(
        baseline, "Expensive Task", cost_usd=0.1, input_tokens=50000,
        output_tokens=50000, cache_read_tokens=3000000, tool_rounds=90,
    )
    return ac.check_cost_anomaly(
        baseline, task_id=task_id, cost_usd=0.1,
        anomaly_threshold=2.0, window_size=10,
    )


def test_rolling_average_in_expected_range(baseline):
    avg = ac.calculate_rolling_cost_average(baseline, window_size=10)
    assert 0.005 < avg < 0.015


def test_expensive_task_is_flagged(anomaly):
    assert anomaly["is_anomaly"]


def test_anomaly_records_cost_and_multiplier(anomaly):
    assert anomaly["cost_usd"] == 0.1
    assert anomaly["multiplier"] > 2.0


def test_anomaly_logs_warning_event(anomaly, conn):
    assert anomaly["event_id"] is not None
    events = ac.recent_events(conn, limit=5)
    assert any(e["kind"] == "warning" for e in events)


def test_normal_cost_not_flagged(baseline):
    """However many tokens moved through cache, an average-cost run is normal."""
    task_id = ac.add_task(baseline, "Normal Task", assigned_to="dev-agent")
    result = ac.check_cost_anomaly(
        baseline, task_id=task_id, cost_usd=0.011,
        anomaly_threshold=2.0, window_size=10,
    )
    assert not result["is_anomaly"]
    assert result["event_id"] is None
