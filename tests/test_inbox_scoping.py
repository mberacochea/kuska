"""A run's prompt carries only its own task's and task-less messages."""
import kuska as ac


def _setup(conn):
    a = ac.add_task(conn, "Task A", assigned_to="dev-agent")
    b = ac.add_task(conn, "Task B", assigned_to="dev-agent")
    ids = {}
    for key, tid in (("b", b), ("a", a), ("none", None)):
        ids[key] = ac.send_message(conn, "human", "dev-agent", tid, "note", f"msg-{key}")
    return a, b, ids


def test_prompt_scoped_to_task(conn):
    a, _b, ids = _setup(conn)
    prompt, got = ac.compose_task_prompt(conn, "dev-agent", ac.get_task(conn, a))
    assert "msg-a" in prompt and "msg-none" in prompt
    assert "msg-b" not in prompt
    assert sorted(got) == sorted([ids["a"], ids["none"]])


def test_other_task_message_stays_unread(conn):
    a, b, ids = _setup(conn)
    _, got = ac.compose_task_prompt(conn, "dev-agent", ac.get_task(conn, a))
    ac.mark_messages_read(conn, got)
    unread = [m["id"] for m in ac.get_inbox(conn, "dev-agent", mark_read=False)]
    assert unread == [ids["b"]]
    prompt_b, _ = ac.compose_task_prompt(conn, "dev-agent", ac.get_task(conn, b))
    assert "msg-b" in prompt_b


def test_unscoped_inbox_returns_all(conn):
    _, _, ids = _setup(conn)
    got = {m["id"] for m in ac.get_inbox(conn, "dev-agent", mark_read=False)}
    assert got == set(ids.values())
