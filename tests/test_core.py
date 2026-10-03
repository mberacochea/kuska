"""Core checks for kuska: agents, tasks, messages, search, runs, features, lifecycle.

The tests share one project and database and run in file order: later ones
build on the rows earlier ones created.
"""

import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import kuska as ac
from kuska.store import InvalidTransition, reply_to_task


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    project = tmp_path_factory.mktemp("core") / "myproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    ac.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n'
        '[agents.bench-agent]\nbackend = "codex"\nrole = "benchmarks"\n'
    )
    return project


@pytest.fixture(scope="module")
def conn(project):
    db = ac.connect(ac.db_path(project))
    ac.init_db(db)
    yield db
    db.close()


t1, t2 = 1, 2  # ids of the first two tasks, created by test_tasks


@pytest.fixture(scope="module")
def mono(conn):
    return ac.Monologue(conn, "dev-agent", t1, quiet=True)


def task_id(conn, title: str) -> int:
    """Id of the task an earlier test created with `title`."""
    return next(t["id"] for t in ac.list_tasks(conn) if t["title"] == title)


def add_ready(conn, *args, **kw):
    """add_task, then move it to "ready" so an agent can claim it."""
    tid = ac.add_task(conn, *args, **kw)
    ac.update_task_status(conn, tid, "ready")
    return tid




def test_agents(project, conn):
    names = ac.sync_agents_from_config(conn, project)
    assert names == ["dev-agent", "bench-agent"], "config synced"
    assert ac.prompt_path(project, "dev-agent").exists(), "prompt seeded"
    assert "dev-agent" in ac.read_prompt(project, "dev-agent"), "prompt readable"
    ac.write_prompt(project, "dev-agent", "custom prompt")
    assert ac.read_prompt(project, "dev-agent") == "custom prompt", "prompt round-trip"
    ac.heartbeat(conn, "dev-agent", "idle")
    assert ac.get_agent(conn, "dev-agent")["status"] == "idle", "heartbeat"
    assert len(ac.list_agents(conn)) == 2, "registry idempotent"


def test_bool_fields(project):
    # Test bool round-trip through config.toml
    ac.set_agent_config(project, "dev-agent", {"worktree": "true"})
    cfg = ac.agent_config(project, "dev-agent")
    assert cfg.get("worktree") is True, "bool field set"
    # Test unticking clears the bool field (missing key means False)
    ac.set_agent_config(project, "dev-agent", {})
    cfg = ac.agent_config(project, "dev-agent")
    assert cfg.get("worktree") is None, "bool field cleared on untick"
    # Test various truthy values
    for val in ["true", "on", "1"]:
        ac.set_agent_config(project, "dev-agent", {"worktree": val})
        cfg = ac.agent_config(project, "dev-agent")
        assert cfg.get("worktree") is True, f"bool field accepts {val}"
    # Test falsy values
    for val in ["false", "off", "0", ""]:
        ac.set_agent_config(project, "dev-agent", {"worktree": val})
        cfg = ac.agent_config(project, "dev-agent")
        assert cfg.get("worktree") is None, f"bool field rejects {val}"
    # Test that agent without worktree field behaves as before
    cfg = ac.agent_config(project, "bench-agent")
    assert cfg.get("worktree") is None, "agent without worktree field defaults to None"


def test_tasks(conn):
    t1 = ac.add_task(conn, "Write the parser", "Handle nested quotes", "dev-agent")
    t2 = ac.add_task(conn, "Benchmark it", assigned_to="bench-agent")
    ac.add_task(conn, "Unassigned idea")
    assert (t1, t2) == (1, 2), "ids"
    assert len(ac.list_tasks(conn)) == 3, "list"
    assert [t["id"] for t in ac.list_tasks(conn, "todo")] == [1, 2, 3], "filter"


def test_claiming(project, conn):
    assert ac.claim_task(conn, "dev-agent") is None, "todo task is not claimed"
    # an idle poll must not wait for the write lock another process holds
    conn2 = ac.connect(ac.db_path(project))
    conn2.execute_sql("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        idle = ac.claim_task(conn, "dev-agent")
        elapsed = time.monotonic() - t0
    finally:
        conn2.execute_sql("ROLLBACK")
        conn2.close()
    assert idle is None and elapsed < 1, "idle claim takes no write lock"
    for tid in (t1, t2):
        ac.update_task_status(conn, tid, "ready")
    assert ac.claim_task(conn, "dev-agent")["id"] == t1, "ready task is claimed"
    ac.update_task_status(conn, t1, "ready")
    assert ac.TASK_STATUSES[:2] == ("todo", "ready"), "ready in TASK_STATUSES after todo"
    claimed = ac.claim_task(conn, "dev-agent")
    assert claimed["id"] == t1, "claimed own task"
    assert ac.claim_task(conn, "dev-agent") is None, "claim is atomic"
    assert ac.get_task(conn, t1)["status"] == "in_progress", "status moved"
    assert ac.claim_task(conn, "bench-agent")["id"] == t2, "other agent unaffected"


def test_dependencies_and_approval(conn):
    d1 = add_ready(conn, "design", assigned_to="dev-agent")
    d2 = add_ready(conn, "implement", assigned_to="dev-agent")
    d3 = add_ready(conn, "document", assigned_to="dev-agent")
    loose = add_ready(conn, "unrelated chore", assigned_to="dev-agent")
    ac.add_dependency(conn, d2, d1)
    ac.add_dependency(conn, d3, d2)
    assert [d["id"] for d in ac.task_dependencies(conn, d2)] == [d1], "dependency recorded"
    assert [d["id"] for d in ac.task_dependents(conn, d1)] == [d2], "dependents seen from the other side"
    assert (ac.add_dependency(conn, d2, d1), len(ac.task_dependencies(conn, d2)))[1] == 1, "adding twice is idempotent"

    ac.update_task_status(conn, d1, "needs_approval")
    assert ac.claim_task(conn, "dev-agent")["id"] == loose, "held task is not runnable"
    assert [d["id"] for d in ac.blocking_dependencies(conn, d2)] == [d1], "its dependent is held too"
    assert [d["id"] for d in ac.blocking_dependencies(conn, d3)] == [d2], "and so is the one behind that"
    assert ac.claim_task(conn, "dev-agent") is None, "nothing else runnable"
    assert set(ac.blocking_map(conn)) == {d2, d3}, "blocking map covers the chain"

    ac.update_task_status(conn, d1, "done")
    claimed = ac.claim_task(conn, "dev-agent")
    assert claimed["id"] == d2, "approval releases the next task"
    assert ac.claim_task(conn, "dev-agent") is None, "the one after still waits"
    ac.update_task_status(conn, d2, "done")
    assert ac.claim_task(conn, "dev-agent")["id"] == d3, "chain drains in order"

    assert ac.blocking_map(conn) == {}, "nothing blocked once it drains"

    # Test ready_to_merge status blocks dependents like needs_approval
    r1 = add_ready(conn, "ready merge test", assigned_to="dev-agent")
    r2 = add_ready(conn, "depends on merge", assigned_to="dev-agent")
    ac.add_dependency(conn, r2, r1)
    ac.update_task_status(conn, r1, "ready_to_merge")
    assert ac.claim_task(conn, "dev-agent") is None, "ready_to_merge blocks dependents"
    assert "ready_to_merge" in ac.TASK_STATUSES, "ready_to_merge in TASK_STATUSES"
    assert "ready_to_merge" in ac.HOLDING_STATUSES, "ready_to_merge in HOLDING_STATUSES"
    ac.update_task_status(conn, r1, "done")
    claimed = ac.claim_task(conn, "dev-agent")
    assert claimed and claimed["id"] == r2, "ready_to_merge unblocks when done"

    with pytest.raises(ValueError) as exc:
        ac.add_dependency(conn, d1, d3)
    assert "already depends" in str(exc.value), "cycles refused"
    with pytest.raises(ValueError):
        ac.add_dependency(conn, d1, d1)
    ac.remove_dependency(conn, d3, d2)
    assert ac.task_dependencies(conn, d3) == [], "dependency removed"
    ac.delete_task(conn, d2)
    assert ac.task_dependencies(conn, d3) == [] and ac.task_dependents(conn, d1) == [], "edges die with the task"
    for tid in (d1, d3, loose):
        ac.delete_task(conn, tid)


def test_wait_for_task(project, conn):
    got: list[dict] = []
    waiter = threading.Thread(
        target=lambda: got.append(ac.wait_for_task(ac.connect(ac.db_path(project)), "dev-agent", 0.05)),
        daemon=True,
    )
    waiter.start()
    time.sleep(0.2)
    assert not got, "still blocked"
    t4 = add_ready(conn, "Late arrival", assigned_to="dev-agent")
    waiter.join(timeout=5)
    assert got and got[0]["id"] == t4, "woke on new task"


def test_messages(conn):
    ac.send_message(conn, "dev-agent", "bench-agent", t1, "question", "Which input size?")
    inbox = ac.get_inbox(conn, "bench-agent")
    assert len(inbox) == 1 and inbox[0]["payload"] == "Which input size?", "inbox delivers"
    assert ac.get_inbox(conn, "bench-agent") == [], "inbox clears"
    ac.send_message(conn, "human", "dev-agent", t1, "note", "Use 1e6 rows")
    assert len(ac.get_inbox(conn, "dev-agent", mark_read=False)) == 1, "peek keeps unread"
    assert len(ac.get_inbox(conn, "dev-agent")) == 1, "peek is repeatable"

    ac.update_task_status(conn, t1, "in_progress")
    ac.update_task_status(conn, t2, "in_progress")
    ac.reply(conn, "dev-agent", t1, "Parser done.", input_tokens=1200, output_tokens=340, cost_usd=0.0182)
    assert ac.get_task(conn, t1)["status"] == "done", "reply closes task"
    assert [m["msg_type"] for m in ac.task_messages(conn, t1)] == ["question", "note", "result"], "thread ordered"
    ac.reply(conn, "bench-agent", t2, "Blocked on hardware.", cost_usd=0.004, status="blocked")
    assert ac.get_task(conn, t2)["status"] == "blocked", "reply can block"
    assert "needs_approval" in ac.TASK_STATUSES, "needs_approval is a status"

    usage = {u["agent"]: u for u in ac.token_usage_by_agent(conn)}
    assert round(usage["dev-agent"]["cost_usd"], 4) == 0.0182, "usage aggregates"
    assert "human" not in usage, "usage excludes human"
    assert usage["dev-agent"]["turns"] == 2, "usage counts turns"


def test_events_agent_monologue(conn, mono):
    mono.record("prompt", "Task 1: Write the parser")
    mono.tool_call("Read", {"file_path": "src/parser.py"})
    mono.tool_result("Read", "def parse(text):\n    pass\n" * 40)
    mono.record("thinking", "quotes need a state machine")
    mono.tool_result("Bash", "exit 1: no such file", is_error=True)
    events = ac.task_events(conn, t1)
    assert ([e["kind"] for e in events] ==
          ["prompt", "tool_use", "tool_result", "thinking", "error"]), "logged in order"
    assert len(events[2]["body"]) > 800, "full body kept"
    assert events[1]["label"] == "Read", "tool name kept"
    assert events[4]["kind"] == "error" and events[4]["label"] == "Bash", "failed tool is an error"
    assert len(ac.run_events(conn, mono.run_id)) == 5, "grouped by run"
    assert ac.task_events(conn, t2) == [], "scoped to its task"
    # newest first: the limit is what makes this a "recent" feed at all -
    # ORDER BY id DESC LIMIT n takes the n latest events, where ascending
    # would pin it to the n oldest forever
    assert ac.recent_events(conn, limit=2)[0]["kind"] == "error", "tail is newest first"
    assert ac.recent_events(conn, agent="bench-agent") == [], "tail filters by agent"
    assert "\n" not in ac.one_line("a\nb\nc") and ac.one_line("x" * 300, 50).endswith("\u2026"), "terminal line is one line"
    with pytest.raises(ValueError):
        mono.record("gossip", "not a kind")


def test_event_filters(conn, mono):
    # telemetry outnumbers substance in a real trail, which is what makes
    # filtering in SQL rather than in the caller the whole point
    mono.record("system", "step 3 of 5", label="task_progress")
    mono.record("system", "still working", label="status")
    kinds = [e["kind"] for e in ac.task_events(conn, t1)]
    assert kinds.count("system") == 2, "no filter still means everything"
    assert "system" not in [e["kind"] for e in ac.task_events(conn, t1, exclude_kinds=("system",))], "task events exclude kinds"
    assert [e["kind"] for e in ac.task_events(conn, t1, kinds=("prompt", "thinking"))] == ["prompt", "thinking"], "task events restrict to kinds"
    assert len(ac.task_events(conn, t1, kinds="system")) == 2, "a single kind need not be a tuple"
    assert ac.task_events(conn, t1, kinds=("result",)) == [], "a filter matching nothing is empty"
    assert ac.recent_events(conn, kinds=()) == [], "an empty kinds tuple matches nothing"
    # newest-first, dev-agent, minus two kinds: order and scope both survive
    combined = ac.recent_events(conn, agent="dev-agent", exclude_kinds=("system", "tool_result"))
    assert ([e["kind"] for e in combined] ==
          ["error", "thinking", "tool_use", "prompt"]), "filters combine"
    # the newest three events are two system rows and one other, so a
    # fetch-then-filter-in-python tail would come back one row long
    trimmed = ac.recent_events(conn, limit=3, exclude_kinds="system")
    assert len(trimmed) == 3, "limit counts the events asked for"
    assert "system" not in [e["kind"] for e in trimmed], "excluded kind stays out of the tail"


def test_runs(conn, mono):
    mono.record("result", "Parser done.", label="done - $0.0182, 3 rounds")
    bench = ac.Monologue(conn, "bench-agent", t1, quiet=True)
    bench.record("prompt", "Task 1: benchmark the parser")
    assert [e["agent"] for e in ac.task_events(conn, t1, agent="bench-agent")] == ["bench-agent"], "task events filter by agent"
    runs = ac.recent_runs(conn)
    assert len(runs) == 2, "one row per run"
    assert [r["run_id"] for r in runs] == [bench.run_id, mono.run_id], "runs are newest first"
    live, finished = runs
    assert finished["event_count"] == len(ac.run_events(conn, mono.run_id)), "run counts its own events"
    assert (finished["agent"], finished["task_id"]) == ("dev-agent", t1), "run carries agent and task"
    assert finished["first_ts"] <= finished["last_ts"], "run spans first to last"
    assert (finished["result"] or "").startswith("done - $0.0182"), "finished run reports its result label"
    # a run with no terminal event is in flight, not absent: it is the one
    # a human most wants to open
    assert live["result"] is None and live["event_count"] == 1, "in-flight run still listed"
    assert [r["run_id"] for r in ac.recent_runs(conn, limit=1)] == [bench.run_id], "runs honour the limit"


def test_docs(conn):
    assert ac.docs_get(conn, "architecture") is None, "missing doc is None"
    ac.docs_set(conn, "architecture", "One SQLite DB per project.", "dev-agent")
    ac.docs_set(conn, "architecture", "One SQLite DB per project. WAL on.", "bench-agent")
    assert ac.docs_get(conn, "architecture").endswith("WAL on."), "docs upsert"
    assert [d["key"] for d in ac.docs_list(conn)] == ["architecture"], "docs list"


def test_search(conn):
    # Test FTS5 ranking: create three docs with increasing relevance to "parser"
    ac.docs_set(conn, "unrelated", "This document is about database queries and SQL.", "dev-agent")
    ac.docs_set(conn, "parser-notes", "Some notes about parsing and parser implementation.", "dev-agent")
    ac.docs_set(conn, "parser-guide", "parser parser parser parser parser - complete guide to parser design.", "dev-agent")
    # The best match should be parser-guide (most occurrences of "parser")
    search_results = ac.full_text_search(conn, "parser", tables=["docs"])
    assert len(search_results) > 0, "search finds matches"
    # Verify best result is first (most negative rank value)
    best_match = search_results[0]
    assert best_match["title"] == "parser-guide", "best match is first result"
    # Verify ranking is ascending (more negative = better)
    if len(search_results) > 1:
        assert search_results[0]["rank"] <= search_results[1]["rank"], "results ranked by relevance"

    # Test FTS5 index sync on repeated updates (regression test for INSERT OR REPLACE bug)
    # The bug: INSERT OR REPLACE doesn't fire DELETE triggers without recursive_triggers=ON,
    # leaving orphaned FTS5 entries that surface as "database disk image is malformed" errors
    ac.docs_set(conn, "fts_test", "alpha version initial", "dev-agent")
    ac.docs_set(conn, "fts_test", "beta version update", "dev-agent")
    ac.docs_set(conn, "fts_test", "gamma version final", "dev-agent")
    # Old FTS term should not be found (was in first version, replaced twice)
    old_results = ac.full_text_search(conn, "alpha", tables=["docs"])
    assert len(old_results) == 0, "stale FTS term not found"
    # Current FTS term should be found exactly once
    new_results = ac.full_text_search(conn, "gamma", tables=["docs"])
    assert len(new_results) == 1, "current FTS term found"

    # Test snippet highlighting and escaping (Bug 1: highlighting was escaped)
    ac.docs_set(conn, "snippet_test", "This document talks about parsing and parser design.", "dev-agent")
    snippet_results = ac.full_text_search(conn, "parser", tables=["docs"])
    snippet_found = [r for r in snippet_results if r["title"] == "snippet_test"]
    assert len(snippet_found) > 0 and snippet_found[0].get("snippet_match"), "snippet has match part"
    if snippet_found:
        snippet = snippet_found[0]
        # Verify snippet contains the matched term
        combined_snippet = (snippet.get("snippet_before", "") + snippet.get("snippet_match", "") + snippet.get("snippet_after", ""))
        assert "parser" in combined_snippet.lower(), "snippet contains matched term"
        # Verify no raw < from template escaping in snippet parts
        assert "&lt;" not in combined_snippet, "snippet parts not double-escaped"

    # Test XSS protection: HTML in document content is escaped, not injected
    ac.docs_set(conn, "xss_test", "Documentation with <script>alert('xss')</script> in body.", "dev-agent")
    xss_results = ac.full_text_search(conn, "script", tables=["docs"])
    xss_found = [r for r in xss_results if r["title"] == "xss_test"]
    assert len(xss_found) > 0, "XSS content found in search"
    if xss_found:
        xss_snippet = xss_found[0]
        combined = (xss_snippet.get("snippet_before", "") + xss_snippet.get("snippet_match", "") + xss_snippet.get("snippet_after", ""))
        # Jinja will escape HTML, so we should see escaped tags, not raw <script>
        assert "&lt;" in combined or "script" in combined, "HTML tags escaped in snippet"

    # Test relevance bar normalization (Bug 2: rank was negative)
    ac.docs_set(conn, "rank_test_a", "minimal", "dev-agent")
    ac.docs_set(conn, "rank_test_b", "minimal minimal minimal minimal minimal", "dev-agent")
    rank_results = ac.full_text_search(conn, "minimal", tables=["docs"])
    assert all("rank_normalized" in r for r in rank_results), "results have normalized rank"
    # Best match should have higher rank_normalized than worst
    if len(rank_results) > 1:
        best = rank_results[0]
        worst = rank_results[-1]
        assert best.get("rank_normalized", 0) >= worst.get("rank_normalized", 0), "better match has higher rank"
    # All normalized ranks should be positive
    assert all(r.get("rank_normalized", 0) >= 0 for r in rank_results), "all ranks are positive"
    # All normalized ranks should be <= 1.0
    assert all(r.get("rank_normalized", 0) <= 1.0 for r in rank_results), "all ranks <= 1.0"


def test_docs_are_markdown(conn):
    prose = "# Report\n\n## Summary\n\nDid a thing.\n"
    assert ac.as_markdown(prose) == prose, "prose untouched"
    fenced = '```json\n{"a": 1}\n```'
    assert ac.as_markdown(fenced) == fenced, "fenced json is prose"
    assert ac.as_markdown('{"a": ') == '{"a": ', "broken json untouched"
    assert ac.as_markdown("17") == "17", "bare scalar untouched"
    assert ac.as_markdown("") == "", "empty body untouched"
    dumped = ac.as_markdown(
        '{"summary": "did a thing", "files_modified": ["a.py", "b.py"],'
        ' "breaking_changes": [], "counts": {"tests": 3}}'
    )
    assert "## Summary" in dumped and "## Files modified" in dumped, "json keys become headings"
    assert "- a.py\n- b.py" in dumped, "json lists become bullets"
    assert "_none_" in dumped, "empty value marked"
    assert "### Tests" in dumped, "nested dict nests"
    assert '{"' not in dumped and '":' not in dumped, "no json punctuation left"
    titled = ac.as_markdown('{"summary": "x"}', title="Task 9: dev-agent report")
    assert titled.startswith("# Task 9: dev-agent report"), "title only when rewritten"
    assert ac.as_markdown(prose, title="Ignored") == prose, "title not bolted onto prose"
    ac.call_tool(conn, "dev-agent", "docs_set",
                 {"key": "handover", "content": '{"summary": "via the tool"}'})
    assert ac.docs_get(conn, "handover") == "## Summary\n\nvia the tool\n", "tool docs_set coerces"
    t7 = add_ready(conn, "context handover test", assigned_to="dev-agent")
    ac.store_workflow_context(conn, "dev-agent", t7, '{"summary": "handover"}')
    doc_key = f"task_{t7}_dev-agent_context"
    stored = ac.docs_get(conn, doc_key)
    assert stored.startswith(f"# Task {t7}: dev-agent report"), "workflow context coerced"
    assert ac.docs_get(conn, doc_key, task_id=t7) == stored, "workflow context linked to its task"
    assert ac.docs_get(conn, doc_key, task_id=t7 + 1) is None, "workflow context not linked to another task"


def test_tools(conn):
    t4 = task_id(conn, "Late arrival")
    t7 = task_id(conn, "context handover test")
    doc_key = f"task_{t7}_dev-agent_context"
    assert ([s["name"] for s in ac.TOOL_SPECS] == [
        "get_inbox", "send_message", "reply", "docs_get", "docs_set",
        "docs_list", "create_task", "list_tasks", "list_features", "set_task_feature",
        "search", "add_tag", "remove_tag", "list_tags"]), "tool set"
    names = lambda cfg: [s["name"] for s in ac.toolset(cfg)]
    assert (names({"flavor": "dev"}) == names({"flavor": "reviewer"})
          == list(ac.tools.BASE_TOOLS)), "dev and reviewer get the base set"
    assert ({"add_tag", "remove_tag"} <= set(names({"flavor": "planner"}))
          and "add_tag" not in names({"flavor": "dev"})), "planners also curate tags"
    assert ("set_task_feature" in names({"flavor": "planner"})
          and "set_task_feature" not in names({"flavor": "dev"})
          and "list_features" in names({"flavor": "dev"})), "planners also curate features"
    assert names({}) == names({"flavor": "dev"}), "no flavor means dev"
    assert names(None) == [s["name"] for s in ac.TOOL_SPECS], "an operator gets every tool"
    dev_tools = ac.toolset({"flavor": "dev"})
    with pytest.raises(KeyError):
        ac.call_tool(conn, "dev-agent", "add_tag", {"task_id": t4, "tags": "x"}, dev_tools)

    ac.call_tool(conn, "dev-agent", "send_message", {"recipient": "bench-agent", "payload": "ping"})
    assert ac.call_tool(conn, "bench-agent", "get_inbox", {})[0]["payload"] == "ping", "tool inbox"
    assert ac.get_inbox(conn, "bench-agent", mark_read=False), "an agent's get_inbox only peeks"
    operator = ac.toolset(None)
    assert (ac.call_tool(conn, "bench-agent", "get_inbox", {}, operator)[0]["payload"] == "ping"
          and not ac.get_inbox(conn, "bench-agent", mark_read=False)), "an operator's get_inbox marks read"

    ac.docs_set(conn, "brief", "the human's brief")
    with pytest.raises(ValueError) as exc:
        ac.call_tool(conn, "dev-agent", "docs_set", {"key": "brief", "content": "mine now"}, dev_tools)
    assert "written by the human" in str(exc.value) and ac.docs_get(conn, "brief") == "the human's brief", "agents cannot overwrite a human's doc"
    ac.call_tool(conn, "dev-agent", "docs_set", {"key": "dev-notes", "content": "v1"}, dev_tools)
    ac.call_tool(conn, "dev-agent", "docs_set", {"key": "dev-notes", "content": "v2"}, dev_tools)
    assert ac.docs_get(conn, "dev-notes") == "v2", "agents can rewrite their own docs"
    ac.call_tool(conn, "you", "docs_set", {"key": "brief", "content": "revised brief"}, operator)
    assert ac.docs_get(conn, "brief") == "revised brief", "an operator can"

    closed = add_ready(conn, "operator closes this", assigned_to="dev-agent")
    ac.call_tool(conn, "you", "reply", {"task_id": closed, "payload": "handled it", "status": "done"}, operator)
    assert ac.get_task(conn, closed)["status"] == "done", "an operator can reply on any task"
    assert ac.call_tool(conn, "dev-agent", "docs_get", {"key": "architecture"})["content"].endswith("WAL on."), "tool docs"
    docs_list = [d["key"] for d in ac.call_tool(conn, "dev-agent", "docs_list", {})]
    # FTS tests added several docs, so just check that the expected ones are present
    assert all(k in docs_list for k in ["architecture", "handover", doc_key]), "tool docs_list"
    assert [d["key"] for d in ac.call_tool(conn, "dev-agent", "docs_list", {"task_id": t7})] == [doc_key], "tool docs_list scoped to task"
    assert len(ac.call_tool(conn, "dev-agent", "list_tasks", {})) >= 1, "tool list_tasks"
    assert ac.tool_result_text({"a": 1}) == '{"a": 1}', "tool result is json"
    with pytest.raises(KeyError):
        ac.call_tool(conn, "dev-agent", "nope", {})
    assert ac.normalize_path("./src/../src/parser.py") == "src/parser.py", "paths are normalised"


def test_create_task_tool(conn):
    new_task = ac.call_tool(conn, "dev-agent", "create_task", {
        "title": "Create async parser",
        "description": "Make the parser async-friendly",
        "assigned_to": "dev-agent"
    })
    assert new_task["title"] == "Create async parser", "create_task returns task"
    assert ac.get_task(conn, new_task["id"]) is not None, "create_task creates in db"

    # Test create_task with dependencies
    dep_task = ac.call_tool(conn, "dev-agent", "create_task", {
        "title": "Test async parser",
        "assigned_to": "bench-agent",
        "depends_on": [new_task["id"]]
    })
    assert [d["id"] for d in ac.task_dependencies(conn, dep_task["id"])] == [new_task["id"]], "create_task with dependencies works"


def test_export(project, conn, mono):
    out = project / ".agents-export"
    written = ac.export_markdown(conn, out)
    assert (out / "plan.md").exists(), "plan written"
    assert "Write the parser" in (out / "tasks.md").read_text(), "tasks written"
    thread = (out / "messages" / f"task-{t1}.md").read_text()
    assert "Parser done." in thread and "$0.0182" in thread, "thread written"
    assert "## Activity" in thread and "src/parser.py" in thread, "monologue exported"
    assert f"### run {mono.run_id}" in thread, "runs delimited in export"
    assert "usage.md" in [p.name for p in written], "usage written"
    assert len(ac.export_markdown(conn, out)) == len(written), "export is rerunnable"


def test_project_discovery(project, tmp_path):
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    assert ac.find_project(nested) == project.resolve(), "finds project upward"
    with pytest.raises(SystemExit):
        ac.find_project(tmp_path)


def test_git_preflight_for_worktree(tmp_path):
    # Create a non-git project directory to test the preflight
    non_git_project = tmp_path / "non-git-project"
    (non_git_project / ".agents" / "prompts").mkdir(parents=True)
    ac.config_path(non_git_project).write_text(
        '[agents.test-agent]\nbackend = "claude"\nrole = "tester"\nworktree = true\n'
    )
    # Try to load config and verify it has worktree=true
    non_git_cfg = ac.agent_config(non_git_project, "test-agent")
    assert non_git_cfg.get("worktree") is True, "worktree=true in config"
    # Simulate what the daemon would check: git rev-parse --is-inside-work-tree
    result = subprocess.run(
        ["git", "-C", str(non_git_project), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, check=False
    )
    assert result.returncode != 0 or result.stdout.strip() != "true", "non-git dir fails git check"


def test_merge_prompt():
    # Test: persona preserved
    existing = "# my-agent\n\nMy role.\n\n## Old section\n\nOld content.\n"
    template = "# {name}\n\nTemplate role.\n\n## New section\n\nNew content.\n"
    merged = ac.merge_prompt(existing, template)
    assert merged.startswith("# my-agent\n\nMy role.\n\n## New section"), "persona preserved"
    # Test: template body current
    assert "New content." in merged and "Old content." not in merged, "template body current"
    # Test: idempotent on second merge
    merged2 = ac.merge_prompt(merged, template)
    assert merged == merged2, "idempotent on second merge"
    # Test: no-"##" file handled
    existing_no_section = "# agent\n\nJust persona.\n"
    merged_no_section = ac.merge_prompt(existing_no_section, template)
    assert merged_no_section.startswith("# agent\n\nJust persona.\n\n## New section"), "no-## file handled"
    # Test: planning-agent's custom header survives
    planning_persona = "# planning-agent\n\nCustom line 1.\n\nCustom line 2.\n\nCustom line 3.\n\n"
    planning_merged = ac.merge_prompt(planning_persona + "## Old\n\nOld.\n", template)
    assert "Custom line 1." in planning_merged and planning_merged.startswith("# planning-agent"), "planning-agent header survives"


def test_reply_to_task_requeue(conn):
    for st in ("done", "blocked", "needs_approval", "ready_to_merge"):
        rt = ac.add_task(conn, f"reply to {st}", assigned_to="dev-agent")
        ac.update_task_status(conn, rt, st)
        reply_to_task(conn, rt, "one more thing")
        assert ac.get_task(conn, rt)["status"] == "ready", f"reply reopens {st}"
    for st in ("todo", "ready", "in_progress"):
        rt = ac.add_task(conn, f"reply to {st}", assigned_to="dev-agent")
        ac.update_task_status(conn, rt, st)
        reply_to_task(conn, rt, "fyi")
        assert ac.get_task(conn, rt)["status"] == st, f"reply leaves {st} alone"
    rt = ac.add_task(conn, "reply to unassigned")
    ac.update_task_status(conn, rt, "done")
    reply_to_task(conn, rt, "anyone?")
    assert ac.get_task(conn, rt)["status"] == "done", "reply leaves unassigned done task alone"


def test_migration_012_ready_status(conn):
    spec = importlib.util.spec_from_file_location(
        "m012", Path(ac.__file__).parent / "migrations" / "012_add_ready_status.py")
    m012 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m012)
    m_assigned = ac.add_task(conn, "old queued", assigned_to="dev-agent")
    m_loose = ac.add_task(conn, "old idea")
    m_deleted = ac.add_task(conn, "old deleted", assigned_to="dev-agent")
    conn.execute_sql("UPDATE tasks SET deleted_at = 1 WHERE id = ?", (m_deleted,))
    m_done = ac.add_task(conn, "old done", assigned_to="dev-agent")
    ac.update_task_status(conn, m_done, "done")
    m_running = ac.add_task(conn, "old running", assigned_to="dev-agent")
    ac.update_task_status(conn, m_running, "in_progress")
    m012.up(None, conn)
    m012.up(None, conn)  # rerunning must be harmless
    assert ac.get_task(conn, m_done)["status"] == "done", "done task untouched"
    assert ac.get_task(conn, m_running)["status"] == "in_progress", "in_progress task untouched"
    assert ac.get_task(conn, m_assigned)["status"] == "ready", "assigned todo becomes ready"
    assert ac.get_task(conn, m_loose)["status"] == "todo", "unassigned todo stays todo"
    assert conn.execute_sql("SELECT status FROM tasks WHERE id = ?", (m_deleted,)).fetchone()[0] == "todo", "soft-deleted todo untouched"


def test_features(conn):
    f1 = ac.add_task(conn, "feature one", feature="  Run-Ledger ")
    f2 = ac.add_task(conn, "feature two", feature="run-ledger")
    f3 = ac.add_task(conn, "no feature")
    ledger = ac.get_feature_by_name(conn, "RUN-LEDGER")
    assert ledger and ledger["name"] == "run-ledger", "feature created on first use, normalised"
    assert ac.get_task(conn, f1)["feature_id"] == ac.get_task(conn, f2)["feature_id"] == ledger["id"], "same name reuses the feature"
    assert ac.get_task(conn, f1)["feature"] == "run-ledger", "task dict carries the feature name"
    assert ac.get_task(conn, f3)["feature"] is None and ac.get_task(conn, f3)["feature_id"] is None, "no feature is None"
    assert [t["id"] for t in ac.list_tasks(conn, feature="Run-Ledger")] == [f1, f2], "list_tasks filters by feature"
    from kuska.store import filter_tasks, list_tags
    assert {t["id"] for t in filter_tasks(conn, feature=["run-ledger"])} == {f1, f2}, "filter_tasks by feature"
    assert (f3 in {t["id"] for t in filter_tasks(conn, feature=[""])}
          and f1 not in {t["id"] for t in filter_tasks(conn, feature=[""])}), "filter_tasks: empty string means no feature"
    b_ok = ac.add_task(conn, "bulk owned", "", "dev-agent")
    b_free = ac.add_task(conn, "bulk unowned", "", None)
    b_run = ac.add_task(conn, "bulk running", "", "dev-agent")
    ac.update_task_status(conn, b_run, "in_progress")
    res = ac.bulk_update_status(conn, [b_ok, b_free, b_run, 99999, b_ok], "ready")
    assert (res["moved"] == [b_ok] and ac.get_task(conn, b_ok)["status"] == "ready"
          and ac.get_task(conn, b_free)["status"] == "todo"), "bulk ready moves only what may go"
    assert dict(res["skipped"]) == {b_free: "no agent", b_run: "in progress", 99999: "not found"}, "bulk skips say why"
    assert ac.get_task(conn, b_run)["status"] == "in_progress", "bulk in_progress task is untouched"
    res = ac.bulk_update_status(conn, [b_ok, b_free], "done")
    assert (res["moved"] == [b_ok, b_free] and not res["skipped"]
          and ac.get_task(conn, b_free)["status"] == "done"), "bulk to done needs no agent"
    assert ac.bulk_update_status(conn, [b_free], "done")["moved"] == [b_free], "bulk to the current status counts as moved"
    for bad in ("in_progress", "bogus"):
        with pytest.raises(ValueError):
            ac.bulk_update_status(conn, [b_ok], bad)
    for tid in (b_ok, b_free, b_run):
        ac.delete_task(conn, tid)
    ac.update_task_status(conn, f2, "done")
    counts = {f["name"]: (f["done"], f["total"]) for f in ac.list_features(conn)}
    assert counts.get("run-ledger") == (1, 2), "list_features counts tasks"
    ac.ensure_feature(conn, "empty one", description="nothing yet")
    counts = {f["name"]: (f["done"], f["total"]) for f in ac.list_features(conn)}
    assert counts.get("empty one") == (0, 0), "a feature with no tasks is listed with zero"
    assert (ac.ensure_feature(conn, "run-ledger", description="runs") == ledger["id"]
          and ac.get_feature(conn, ledger["id"])["description"] == "runs"
          and ac.ensure_feature(conn, "run-ledger", description="other") == ledger["id"]
          and ac.get_feature(conn, ledger["id"])["description"] == "runs"), "ensure_feature sets a missing description only"
    ac.update_task(conn, f3, feature="Supervisor")
    assert ac.get_task(conn, f3)["feature"] == "supervisor", "update_task moves a task into a (new) feature"
    ac.update_task(conn, f3, feature="")
    assert ac.get_task(conn, f3)["feature_id"] is None, "update_task with empty feature removes it"
    ac.update_feature(conn, ledger["id"], name="Ledger")
    assert ac.get_task(conn, f1)["feature"] == "ledger", "rename follows to tasks"
    with pytest.raises(ValueError):
        ac.update_feature(conn, ledger["id"], name="supervisor")
    assert (ac.delete_feature(conn, ledger["id"]) == 2 and ac.get_task(conn, f1)["feature_id"] is None
          and ac.get_feature(conn, ledger["id"]) is None), "delete_feature ungroups its tasks"


def test_feature_tools(conn):
    made = ac.call_tool(conn, "dev-agent", "create_task", {"title": "via tool", "feature": "tooling"})
    assert made["feature"] == "tooling", "create_task tool takes a feature"
    listed = ac.call_tool(conn, "dev-agent", "list_tasks", {"feature": "tooling"})
    assert [t["id"] for t in listed] == [made["id"]], "list_tasks tool filters by feature"
    assert "tooling" in [f["name"] for f in ac.call_tool(conn, "dev-agent", "list_features", {})], "list_features tool"
    planner = ac.toolset({"flavor": "planner"})
    moved = ac.call_tool(conn, "planning-agent", "set_task_feature", {"task_id": made["id"], "feature": "other"}, planner)
    assert moved["feature"] == "other", "set_task_feature moves a task"
    moved = ac.call_tool(conn, "planning-agent", "set_task_feature", {"task_id": made["id"], "feature": ""}, planner)
    assert moved["feature"] is None, "set_task_feature with empty removes it"


def test_migration_013_features(tmp_path):
    from kuska.migration import run_migrations
    old = ac.connect(tmp_path / "old.db")
    run_migrations(old, target_version="012_add_ready_status")
    old.execute_sql(
        "INSERT INTO tasks (title, status, feature, created_at, updated_at) VALUES "
        "('a', 'todo', 'Search ', 0, 0), ('b', 'todo', 'search', 0, 0), "
        "('c', 'todo', NULL, 0, 0), ('d', 'todo', 'ui', 0, 0)"
    )
    ac.init_db(old)
    assert [f["name"] for f in ac.list_features(old)] == ["search", "ui"], "free-text features become rows"
    assert [t["feature"] for t in ac.list_tasks(old)] == ["search", "search", None, "ui"], "tasks linked to their backfilled feature"
    assert "feature" in {c.name for c in old.get_columns("tasks")}, "old column kept for older processes"
    old.close()


def test_task_kind(conn):
    k1 = ac.add_task(conn, "kind default")
    assert ac.get_task(conn, k1)["kind"] == "work", "add_task defaults to kind work"
    k2 = ac.add_task(conn, "kind review", kind="review")
    assert ac.get_task(conn, k2)["kind"] == "review", "kind review is stored"
    with pytest.raises(ValueError):
        ac.add_task(conn, "kind bogus", kind="bogus")
    k3 = ac.add_task(conn, "tagged work", tags="answer")
    assert not ac.is_answer_task(ac.get_task(conn, k3)), "answer tag on a work task is not an answer task"
    assert ac.is_work_task(ac.get_task(conn, k3)), "is_work_task on a work task"
    kq = ac.add_task(conn, "asker", assigned_to="dev-agent")
    ac.update_task_status(conn, kq, "in_progress")
    ans = ac.ask_agent(conn, "dev-agent", "bench-agent", kq, "which way?")
    at = ac.get_task(conn, ans)
    assert at["kind"] == "answer" and at["tags"] is None and ac.is_answer_task(at), \
        "ask_agent makes a kind-answer task without tags"


def test_migration_016_task_kind(tmp_path):
    from kuska.migration import run_migrations
    old = ac.connect(tmp_path / "old16.db")
    run_migrations(old, target_version="014_add_runs")
    old.execute_sql(
        "INSERT INTO tasks (title, status, tags, created_at, updated_at) VALUES "
        "('a', 'todo', 'x,answer', 0, 0), ('b', 'todo', 'answers', 0, 0)"
    )
    ac.init_db(old)
    assert [t["kind"] for t in ac.list_tasks(old)] == ["answer", "work"], "only the answer-tagged task is backfilled"
    old.close()


def test_task_tags_table(conn):
    from kuska.store import filter_tasks, list_tags
    t = ac.add_task(conn, "tag norm", tags="Bug, ui,bug")
    assert ac.get_task(conn, t)["tags"] == "bug,ui", "add_task normalises tags"
    d = ac.add_task(conn, "tag debug", tags="debug")
    plain = ac.add_task(conn, "tag none")
    got = {x["id"] for x in filter_tasks(conn, tags=["bug"])}
    assert t in got and d not in got, "tag filter is exact: bug does not match debug"
    got = {x["id"] for x in filter_tasks(conn, tags=[""])}
    assert plain in got and t not in got and d not in got, "empty tag matches only untagged tasks"
    assert ac.get_task(conn, plain)["tags"] is None, "untagged task has tags None"
    assert {x["id"]: x["tags"] for x in ac.list_tasks(conn)}[t] == "bug,ui", "list_tasks fills tags"
    before = ac.get_task(conn, t)["updated_at"]
    time.sleep(0.01)
    ac.update_task(conn, t, tags="")
    after = ac.get_task(conn, t)
    assert after["tags"] is None and after["updated_at"] > before, "update_task tags='' clears and bumps updated_at"
    ac.add_task_tags(conn, t, "zeta, Alpha")
    ac.add_task_tags(conn, t, "alpha")
    assert ac.get_task(conn, t)["tags"] == "alpha,zeta", "add_task_tags adds without duplicates"
    ac.remove_task_tags(conn, t, "ALPHA,missing")
    assert ac.get_task(conn, t)["tags"] == "zeta", "remove_task_tags removes"
    ac.set_task_tags(conn, t, "q")
    assert ac.get_task(conn, t)["tags"] == "q", "set_task_tags replaces"
    tags = list_tags(conn)
    assert tags == sorted(set(tags)) and "debug" in tags and "q" in tags, "list_tags sorted and distinct"
    ac.delete_task(conn, d)
    n = conn.execute_sql("SELECT COUNT(*) FROM task_tags WHERE task_id = ?", (d,)).fetchone()[0]
    assert n == 0, "deleting a task deletes its task_tags rows"


def test_migration_017_task_tags(tmp_path):
    from kuska.migration import run_migrations
    old = ac.connect(tmp_path / "old17.db")
    run_migrations(old, target_version="016_add_task_kind")
    old.execute_sql(
        "INSERT INTO tasks (title, status, tags, created_at, updated_at) VALUES "
        "('a', 'todo', ' A,b ,,a', 0, 0)"
    )
    ac.init_db(old)
    assert ac.list_tasks(old)[0]["tags"] == "a,b", "backfill normalises old tags"
    names = [r[0] for r in old.execute_sql("SELECT name FROM sqlite_master WHERE tbl_name = 'task_tags' AND type = 'index'")]
    assert len(names) == len(set(names)) == 2, "init_db adds no duplicate indexes"
    old.close()


def test_runs_table(conn):
    rt1 = ac.add_task(conn, "run subject", assigned_to="dev-agent")
    rt2 = ac.add_task(conn, "other run subject", assigned_to="dev-agent")
    ac.start_run(conn, "aaaaaaaaaaaa", rt1, "dev-agent")
    r = ac.get_run(conn, "aaaaaaaaaaaa")
    assert (r["status"] == "running" and r["task_id"] == rt1 and r["agent"] == "dev-agent"
          and r["ended_at"] is None and r["cost_usd"] == 0 and r["tool_rounds"] == 0), "start_run inserts a running row"
    assert ac.get_run(conn, "nope") is None, "get_run of an unknown id is None"
    conn.execute_sql("UPDATE runs SET heartbeat_at = heartbeat_at - 100 WHERE id = 'aaaaaaaaaaaa'")
    before = ac.get_run(conn, "aaaaaaaaaaaa")["heartbeat_at"]
    ac.touch_run(conn, "aaaaaaaaaaaa")
    assert ac.get_run(conn, "aaaaaaaaaaaa")["heartbeat_at"] > before, "touch_run moves heartbeat_at forward"
    time.sleep(0.01)

    ac.start_run(conn, "bbbbbbbbbbbb", rt1, "dev-agent")
    ac.start_run(conn, "cccccccccccc", rt2, "review-agent")
    ac.end_run(conn, "bbbbbbbbbbbb", "finished", exit_reason="ok", input_tokens=5, cost_usd=0.1, bogus=1)
    r = ac.get_run(conn, "bbbbbbbbbbbb")
    assert (r["status"] == "finished" and r["exit_reason"] == "ok" and r["input_tokens"] == 5
          and r["cost_usd"] == 0.1 and r["output_tokens"] == 0 and r["ended_at"] is not None), "end_run stores status, reason and usage"
    assert "bogus" not in r, "end_run ignores keys that are not columns"
    conn.execute_sql("UPDATE runs SET heartbeat_at = heartbeat_at - 100 WHERE id = 'bbbbbbbbbbbb'")
    hb = ac.get_run(conn, "bbbbbbbbbbbb")["heartbeat_at"]
    ac.touch_run(conn, "bbbbbbbbbbbb")
    assert ac.get_run(conn, "bbbbbbbbbbbb")["heartbeat_at"] == hb, "touch_run leaves an ended run alone"
    for bad in ("running", "nonsense"):
        with pytest.raises(ValueError):
            ac.end_run(conn, "cccccccccccc", bad)
    assert ac.get_run(conn, "cccccccccccc")["status"] == "running", "a refused end_run changes nothing"

    conn.execute_sql("UPDATE runs SET heartbeat_at = heartbeat_at - 120 WHERE id = 'cccccccccccc'")
    assert [x["id"] for x in ac.stale_runs(conn, 60)] == ["cccccccccccc"], "stale_runs returns only the running run with an old heartbeat"
    assert [x["id"] for x in ac.running_runs(conn)] == ["aaaaaaaaaaaa", "cccccccccccc"], "running_runs lists the running ones, oldest first"
    assert ([x["id"] for x in ac.task_runs(conn, rt1)] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
          and [x["id"] for x in ac.task_runs(conn, rt2)] == ["cccccccccccc"]), "task_runs is oldest first and per task"

    assert ac.set_run_result_message(conn, "dev-agent", rt1, 42) == "aaaaaaaaaaaa", "set_run_result_message returns the run id"
    assert ac.get_run(conn, "aaaaaaaaaaaa")["result_message_id"] == 42, "...and stores the message id"
    assert (ac.set_run_result_message(conn, "dev-agent", rt2, 43) is None
          and ac.get_run(conn, "bbbbbbbbbbbb")["result_message_id"] is None), "set_run_result_message skips ended runs"
    ac.end_run(conn, "aaaaaaaaaaaa", "failed", exit_reason="boom")
    assert ac.set_run_result_message(conn, "dev-agent", rt1, 44) is None, "set_run_result_message is None with no running run"
    ac.delete_task(conn, rt2)
    assert ac.get_run(conn, "cccccccccccc") is not None, "runs outlive their task"
    names = [x[0] for x in conn.execute_sql(
        "SELECT name FROM sqlite_master WHERE tbl_name='runs' AND type='index' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]
    assert sorted(names) == ["run_status", "run_task_id"], "runs has one index per column, no twins"
    from kuska.tables import TABLES
    assert "runs" in TABLES, "runs is on the Data page"


def test_reply_tool_links_the_run_s_result_message(conn):
    lt = ac.add_task(conn, "linked reply subject", assigned_to="dev-agent")
    ac.update_task_status(conn, lt, "in_progress")
    ac.start_run(conn, "r1", lt, "dev-agent")
    out = ac.call_tool(conn, "dev-agent", "reply", {"task_id": lt, "payload": "ok"},
                       ac.toolset({"flavor": "dev"}))
    assert ac.get_run(conn, "r1")["result_message_id"] == out["id"], "reply tool stores its message id on the run"
    mid = ac.finish_task(conn, "dev-agent", lt, "text", since=time.time() + 1000, run_id="r1", cost_usd=0.2)
    results = [m for m in ac.task_messages(conn, lt) if m["msg_type"] == "result"]
    assert (mid == out["id"] and len(results) == 1 and results[0]["id"] == out["id"]
          and results[0]["cost_usd"] == 0.2), "finish_task with a run id books usage on the linked reply, whatever `since` says"


@pytest.fixture(scope="module")
def task_in(conn):
    def task_in(status, agent="dev-agent", worktree=None):
        tid = ac.add_task(conn, "lifecycle probe", assigned_to=agent)
        if worktree:
            ac.update_task(conn, tid, worktree_path=worktree)
        ac.update_task_status(conn, tid, status)
        return tid

    return task_in


@pytest.fixture(scope="module")
def status_of(conn):
    def status_of(tid):
        return ac.get_task(conn, tid)["status"]

    return status_of


def test_lifecycle(conn, task_in, status_of):
    from kuska.store import TRANSITIONS, transition

    simple = {
        "make_ready": "ready", "park": "todo", "claim": "in_progress", "hold": "needs_approval",
        "block": "blocked", "await_answer": "ready", "approve": "done", "merged": "done", "close": "done",
    }
    for event, expected in simple.items():
        start = TRANSITIONS[event][0][0]
        tid = task_in(start if event != "hold" else "in_progress")
        assert transition(conn, tid, event)["status"] == expected, f"{event}: {start} -> {expected}"
    for event in ("hold", "block", "await_answer", "finish"):
        assert TRANSITIONS[event][0] == ("in_progress",), f"{event} starts only from in_progress"

    tid = task_in("in_progress")
    assert transition(conn, tid, "finish")["status"] == "done", "finish without worktree -> done"
    tid = task_in("in_progress", worktree="/tmp/wt")
    assert transition(conn, tid, "finish")["status"] == "ready_to_merge", "finish with worktree -> ready_to_merge"
    tid = task_in("blocked")
    assert transition(conn, tid, "requeue")["status"] == "ready", "requeue assigned -> ready"
    tid = ac.add_task(conn, "unassigned probe")
    ac.update_task_status(conn, tid, "blocked")
    assert transition(conn, tid, "requeue")["status"] == "todo", "requeue unassigned -> todo"

    def refuses(tid, event, **kw):
        try:
            transition(conn, tid, event, **kw)
        except InvalidTransition:
            return True
        return False

    tid = ac.add_task(conn, "unassigned probe")
    assert refuses(tid, "make_ready") and status_of(tid) == "todo", "make_ready without an agent refused"
    tid = task_in("ready_to_merge")
    assert refuses(tid, "close") and status_of(tid) == "ready_to_merge", "close on ready_to_merge refused, status unchanged"
    tid = task_in("in_progress")
    assert refuses(tid, "approve") and status_of(tid) == "in_progress", "approve on in_progress refused"

    tid = task_in("todo")
    forced = transition(conn, tid, "force", to="in_progress")
    assert forced["status"] == "in_progress", "force by human sets any status"
    assert any(m["msg_type"] == "note" and "forced" in m["payload"] for m in ac.task_messages(conn, tid)), "force leaves a note"
    assert (refuses(tid, "force", actor="dev-agent", to="done")
          and status_of(tid) == "in_progress"), "force by an agent refused"
    with pytest.raises(ValueError):
        transition(conn, tid, "force", to="nonsense")
    with pytest.raises(ValueError):
        transition(conn, tid, "explode")
    with pytest.raises(ValueError, match="not found"):  # missing task
        transition(conn, 999999, "approve")


def test_lifecycle_routing(conn, tmp_path, task_in, status_of):
    wt = task_in("in_progress", worktree="/tmp/wt-probe")
    ac.reply(conn, "dev-agent", wt, "done on a branch", status="done")
    assert status_of(wt) == "ready_to_merge", "agent reply done on a worktree task gives ready_to_merge"

    idle = task_in("ready")
    with pytest.raises(InvalidTransition):
        ac.reply(conn, "dev-agent", idle, "not mine to close")
    assert status_of(idle) == "ready", "a refused reply leaves the task alone"

    todo = task_in("todo")
    ac.call_tool(conn, "you", "reply", {"task_id": todo, "payload": "closing by hand", "status": "done"},
                 ac.toolset(None))
    notes = [m for m in ac.task_messages(conn, todo) if m["msg_type"] == "note"]
    assert status_of(todo) == "done" and any("forced" in m["payload"] for m in notes), "operator reply closes a todo task, with a forced note"

    from kuska import runtime
    moved = task_in("todo")
    runtime.fail_task(conn, "dev-agent", moved, "boom", cost_usd=0.5)
    blockers = [m for m in ac.task_messages(conn, moved) if m["msg_type"] == "blocker"]
    assert status_of(moved) == "todo" and len(blockers) == 1 and blockers[0]["cost_usd"] == 0.5, "fail_task on a task a human moved keeps its status but logs the blocker"

    held = task_in("needs_approval")
    runtime.finish_task(conn, "dev-agent", held, "all done", since=time.time() + 1000, cost_usd=0.25,
                        input_tokens=7)
    results = [m for m in ac.task_messages(conn, held) if m["msg_type"] == "result"]
    assert (status_of(held) == "needs_approval" and len(results) == 1
          and results[0]["cost_usd"] == 0.25 and results[0]["input_tokens"] == 7), "finish_task with no reply keeps a human-moved status, records one result with usage"

    conn.close()

    # kuska init: worktree default depends on git state
    import os
    import tomllib

    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}

    def run_init(path):
        return subprocess.run([sys.executable, "-m", "kuska", "init", str(path)],
                              capture_output=True, text=True, env=env)

    def dev_worktree(path):
        cfg = tomllib.loads((path / ".agents" / "config.toml").read_text())
        return cfg["agents"]["dev-agent"]["worktree"]

    plain = tmp_path / "plain-proj"
    plain.mkdir()
    r = run_init(plain)
    assert dev_worktree(plain) is False, "init outside git: worktree off"
    assert "worktrees are off" in r.stdout, "init outside git: prints note"

    gitp = tmp_path / "git-proj"
    gitp.mkdir()
    (gitp / "f.txt").write_text("x")
    for cmd in (["init"], ["config", "user.email", "t@example.com"],
                ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"], ["add", "f.txt"],
                ["commit", "-m", "first"]):
        subprocess.run(["git", *cmd], cwd=gitp, capture_output=True, check=True)
    r = run_init(gitp)
    assert dev_worktree(gitp) is True, "init in git repo with commit: worktree on"

    cfgp = gitp / ".agents" / "config.toml"
    cfgp.write_text("# sentinel\n" + cfgp.read_text())
    run_init(gitp)
    assert cfgp.read_text().startswith("# sentinel\n"), "re-init keeps existing config"


LAZY_IMPORT_PROBES = [
    (
        "import kuska pulls in no front-end or SDK",
        "import sys, kuska; bad = [m for m in ('flask', 'mcp', 'claude_agent_sdk', 'openai_codex', 'openai') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)",
    ),
    ("create_app and run_mcp load on demand", "import kuska; kuska.create_app; kuska.run_mcp"),
    (
        "kuska.daemons does not import the claude SDK",
        "import sys, kuska.daemons as d; assert 'claude_agent_sdk' not in sys.modules",
    ),
]


@pytest.mark.parametrize("label, code", LAZY_IMPORT_PROBES, ids=[p[0] for p in LAZY_IMPORT_PROBES])
def test_lazy_imports(label, code):
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
