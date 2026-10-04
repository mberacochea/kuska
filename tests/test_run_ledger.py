"""Runs are the only cost ledger: stats sum them, and migration 020 backfills them."""

import pytest

import kuska as ac
from kuska.store import cost_by_task


def _run(conn, task_id, run_id, cost, tin=10, tout=5, cache_read=100, rounds=2):
    ac.start_run(conn, run_id, task_id, "dev-agent")
    ac.end_run(
        conn, run_id, "finished", input_tokens=tin, output_tokens=tout,
        cache_read_tokens=cache_read, tool_rounds=rounds, cost_usd=cost,
    )


def test_stats_totals_are_the_sum_of_runs(conn):
    for i in range(3):
        task_id = ac.add_task(conn, f"T{i}", assigned_to="dev-agent")
        _run(conn, task_id, f"run{i:09d}", 0.1 * (i + 1))
    runs = [r for t in ac.list_tasks(conn) for r in ac.task_runs(conn, t["id"])]
    usage = {u["agent"]: u for u in ac.token_usage_by_agent(conn)}["dev-agent"]
    assert usage["turns"] == len(runs) == 3
    assert usage["cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in runs))
    assert usage["input_tokens"] == 30 and usage["cache_read_tokens"] == 300
    assert usage["tool_rounds"] == 6
    assert sum(t["cost"] for t in cost_by_task(conn)) == pytest.approx(0.6)


def test_message_usage_is_not_counted(conn):
    task_id = ac.add_task(conn, "T", assigned_to="dev-agent")
    ac.send_message(conn, "dev-agent", "human", task_id, "result", "x", cost_usd=9.0)
    assert ac.token_usage_by_agent(conn) == []
    assert cost_by_task(conn) == []


def test_backfilled_db_keeps_its_totals(tmp_path):
    from kuska.migration import run_migrations

    db = ac.connect(tmp_path / "old.db")
    run_migrations(db, target_version="019_add_doc_tasks")
    for i, cost in enumerate([0.5, 0.25]):
        db.execute_sql(
            "INSERT INTO tasks (title, status, created_at, updated_at) VALUES (?, 'done', 1, 1)",
            (f"t{i}",),
        )
        db.execute_sql(
            "INSERT INTO messages (ts, sender, recipient, task_id, msg_type, payload, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd) "
            "VALUES (?, 'dev-agent', 'human', ?, 'result', 'x', 100, 20, 7, 0, 3, ?)",
            (10.0 + i, i + 1, cost),
        )
    # no usage: gets no run
    db.execute_sql(
        "INSERT INTO messages (ts, sender, recipient, task_id, msg_type, payload) "
        "VALUES (30, 'dev-agent', 'human', 1, 'note', 'n')"
    )
    # usage already on a run that points at it: not duplicated
    db.execute_sql(
        "INSERT INTO messages (ts, sender, recipient, task_id, msg_type, payload, cost_usd) "
        "VALUES (31, 'dev-agent', 'human', 1, 'result', 'y', 0.4)"
    )
    db.execute_sql(
        "INSERT INTO runs (id, task_id, agent, status, started_at, heartbeat_at, ended_at, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd, result_message_id) "
        "VALUES ('abcabcabcabc', 1, 'dev-agent', 'finished', 31, 31, 31, 0, 0, 0, 0, 0, 0.4, 4)"
    )
    run_migrations(db)

    ids = sorted(r[0] for r in db.execute_sql("SELECT id FROM runs WHERE id LIKE 'm%'"))
    assert ids == ["m1", "m2"]
    run = ac.get_run(db, "m1")
    assert (run["status"], run["agent"], run["task_id"], run["started_at"], run["ended_at"]) == (
        "finished", "dev-agent", 1, 10.0, 10.0)
    usage = ac.token_usage_by_agent(db)[0]
    assert usage["cost_usd"] == pytest.approx(0.5 + 0.25 + 0.4)
    assert (usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"]) == (200, 40, 14)
    assert usage["tool_rounds"] == 6 and usage["turns"] == 3
    # the message columns are left alone for older processes
    assert db.execute_sql("SELECT SUM(cost_usd) FROM messages").fetchone()[0] == pytest.approx(1.15)
    db.close()
