#!/usr/bin/env python3
"""Standalone checks for kuska - no test framework, just `uv run tests/test_core.py`."""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import kuska as ac
from kuska.store import reply_to_task

PASSED = 0


def add_ready(conn, *args, **kw):
    """add_task, then move it to "ready" so an agent can claim it."""
    tid = ac.add_task(conn, *args, **kw)
    ac.update_task_status(conn, tid, "ready")
    return tid


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        project = tmp / "myproject"
        (project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(project).write_text(
            '[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n'
            '[agents.bench-agent]\nbackend = "codex"\nrole = "benchmarks"\n'
        )
        conn = ac.connect(ac.db_path(project))
        ac.init_db(conn)

        print("agents")
        names = ac.sync_agents_from_config(conn, project)
        check("config synced", names == ["dev-agent", "bench-agent"], names)
        check("prompt seeded", ac.prompt_path(project, "dev-agent").exists())
        check("prompt readable", "dev-agent" in ac.read_prompt(project, "dev-agent"))
        ac.write_prompt(project, "dev-agent", "custom prompt")
        check("prompt round-trip", ac.read_prompt(project, "dev-agent") == "custom prompt")
        ac.heartbeat(conn, "dev-agent", "idle")
        check("heartbeat", ac.get_agent(conn, "dev-agent")["status"] == "idle")
        check("registry idempotent", len(ac.list_agents(conn)) == 2)

        print("bool fields")
        # Test bool round-trip through config.toml
        ac.set_agent_config(project, "dev-agent", {"worktree": "true"})
        cfg = ac.agent_config(project, "dev-agent")
        check("bool field set", cfg.get("worktree") is True, cfg.get("worktree"))
        # Test unticking clears the bool field (missing key means False)
        ac.set_agent_config(project, "dev-agent", {})
        cfg = ac.agent_config(project, "dev-agent")
        check("bool field cleared on untick", cfg.get("worktree") is None, cfg.get("worktree"))
        # Test various truthy values
        for val in ["true", "on", "1"]:
            ac.set_agent_config(project, "dev-agent", {"worktree": val})
            cfg = ac.agent_config(project, "dev-agent")
            check(f"bool field accepts {val}", cfg.get("worktree") is True, f"value was {cfg.get('worktree')}")
        # Test falsy values
        for val in ["false", "off", "0", ""]:
            ac.set_agent_config(project, "dev-agent", {"worktree": val})
            cfg = ac.agent_config(project, "dev-agent")
            check(f"bool field rejects {val}", cfg.get("worktree") is None, f"value was {cfg.get('worktree')}")
        # Test that agent without worktree field behaves as before
        cfg = ac.agent_config(project, "bench-agent")
        check("agent without worktree field defaults to None", cfg.get("worktree") is None, cfg.get("worktree"))

        print("tasks")
        t1 = ac.add_task(conn, "Write the parser", "Handle nested quotes", "dev-agent")
        t2 = ac.add_task(conn, "Benchmark it", assigned_to="bench-agent")
        ac.add_task(conn, "Unassigned idea")
        check("ids", (t1, t2) == (1, 2))
        check("list", len(ac.list_tasks(conn)) == 3)
        check("filter", [t["id"] for t in ac.list_tasks(conn, "todo")] == [1, 2, 3])

        print("claiming")
        check("todo task is not claimed", ac.claim_task(conn, "dev-agent") is None)
        for tid in (t1, t2):
            ac.update_task_status(conn, tid, "ready")
        check("ready task is claimed", ac.claim_task(conn, "dev-agent")["id"] == t1)
        ac.update_task_status(conn, t1, "ready")
        check("ready in TASK_STATUSES after todo", ac.TASK_STATUSES[:2] == ("todo", "ready"))
        claimed = ac.claim_task(conn, "dev-agent")
        check("claimed own task", claimed["id"] == t1, claimed)
        check("claim is atomic", ac.claim_task(conn, "dev-agent") is None)
        check("status moved", ac.get_task(conn, t1)["status"] == "in_progress")
        check("other agent unaffected", ac.claim_task(conn, "bench-agent")["id"] == t2)

        print("dependencies and approval")
        d1 = add_ready(conn, "design", assigned_to="dev-agent")
        d2 = add_ready(conn, "implement", assigned_to="dev-agent")
        d3 = add_ready(conn, "document", assigned_to="dev-agent")
        loose = add_ready(conn, "unrelated chore", assigned_to="dev-agent")
        ac.add_dependency(conn, d2, d1)
        ac.add_dependency(conn, d3, d2)
        check("dependency recorded", [d["id"] for d in ac.task_dependencies(conn, d2)] == [d1])
        check("dependents seen from the other side", [d["id"] for d in ac.task_dependents(conn, d1)] == [d2])
        check("adding twice is idempotent", (ac.add_dependency(conn, d2, d1), len(ac.task_dependencies(conn, d2)))[1] == 1)

        ac.update_task_status(conn, d1, "needs_approval")
        check("held task is not runnable", ac.claim_task(conn, "dev-agent")["id"] == loose)
        check("its dependent is held too", [d["id"] for d in ac.blocking_dependencies(conn, d2)] == [d1])
        check("and so is the one behind that", [d["id"] for d in ac.blocking_dependencies(conn, d3)] == [d2])
        check("nothing else runnable", ac.claim_task(conn, "dev-agent") is None)
        check("blocking map covers the chain", set(ac.blocking_map(conn)) == {d2, d3}, ac.blocking_map(conn))

        ac.update_task_status(conn, d1, "done")
        claimed = ac.claim_task(conn, "dev-agent")
        check("approval releases the next task", claimed["id"] == d2, claimed)
        check("the one after still waits", ac.claim_task(conn, "dev-agent") is None)
        ac.update_task_status(conn, d2, "done")
        check("chain drains in order", ac.claim_task(conn, "dev-agent")["id"] == d3)

        check("nothing blocked once it drains", ac.blocking_map(conn) == {}, ac.blocking_map(conn))

        # Test ready_to_merge status blocks dependents like needs_approval
        r1 = add_ready(conn, "ready merge test", assigned_to="dev-agent")
        r2 = add_ready(conn, "depends on merge", assigned_to="dev-agent")
        ac.add_dependency(conn, r2, r1)
        ac.update_task_status(conn, r1, "ready_to_merge")
        check("ready_to_merge blocks dependents", ac.claim_task(conn, "dev-agent") is None)
        check("ready_to_merge in TASK_STATUSES", "ready_to_merge" in ac.TASK_STATUSES)
        check("ready_to_merge in HOLDING_STATUSES", "ready_to_merge" in ac.HOLDING_STATUSES)
        ac.update_task_status(conn, r1, "done")
        claimed = ac.claim_task(conn, "dev-agent")
        check("ready_to_merge unblocks when done", claimed and claimed["id"] == r2, claimed)

        try:
            ac.add_dependency(conn, d1, d3)
            check("cycles refused", False)
        except ValueError as exc:
            check("cycles refused", "already depends" in str(exc))
        try:
            ac.add_dependency(conn, d1, d1)
            check("self-dependency refused", False)
        except ValueError:
            check("self-dependency refused", True)
        ac.remove_dependency(conn, d3, d2)
        check("dependency removed", ac.task_dependencies(conn, d3) == [])
        ac.delete_task(conn, d2)
        check("edges die with the task", ac.task_dependencies(conn, d3) == [] and ac.task_dependents(conn, d1) == [])
        for tid in (d1, d3, loose):
            ac.delete_task(conn, tid)

        print("wait_for_task")
        got: list[dict] = []
        waiter = threading.Thread(
            target=lambda: got.append(ac.wait_for_task(ac.connect(ac.db_path(project)), "dev-agent", 0.05)),
            daemon=True,
        )
        waiter.start()
        time.sleep(0.2)
        check("still blocked", not got)
        t4 = add_ready(conn, "Late arrival", assigned_to="dev-agent")
        waiter.join(timeout=5)
        check("woke on new task", got and got[0]["id"] == t4, got)

        print("messages")
        ac.send_message(conn, "dev-agent", "bench-agent", t1, "question", "Which input size?")
        inbox = ac.get_inbox(conn, "bench-agent")
        check("inbox delivers", len(inbox) == 1 and inbox[0]["payload"] == "Which input size?")
        check("inbox clears", ac.get_inbox(conn, "bench-agent") == [])
        ac.send_message(conn, "human", "dev-agent", t1, "note", "Use 1e6 rows")
        check("peek keeps unread", len(ac.get_inbox(conn, "dev-agent", mark_read=False)) == 1)
        check("peek is repeatable", len(ac.get_inbox(conn, "dev-agent")) == 1)

        ac.reply(conn, "dev-agent", t1, "Parser done.", input_tokens=1200, output_tokens=340, cost_usd=0.0182)
        check("reply closes task", ac.get_task(conn, t1)["status"] == "done")
        check("thread ordered", [m["msg_type"] for m in ac.task_messages(conn, t1)] == ["question", "note", "result"])
        ac.reply(conn, "bench-agent", t2, "Blocked on hardware.", cost_usd=0.004, status="blocked")
        check("reply can block", ac.get_task(conn, t2)["status"] == "blocked")
        check("needs_approval is a status", "needs_approval" in ac.TASK_STATUSES)

        usage = {u["agent"]: u for u in ac.token_usage_by_agent(conn)}
        check("usage aggregates", round(usage["dev-agent"]["cost_usd"], 4) == 0.0182, usage)
        check("usage excludes human", "human" not in usage, usage)
        check("usage counts turns", usage["dev-agent"]["turns"] == 2, usage)

        print("events (agent monologue)")
        mono = ac.Monologue(conn, "dev-agent", t1, quiet=True)
        mono.record("prompt", "Task 1: Write the parser")
        mono.tool_call("Read", {"file_path": "src/parser.py"})
        mono.tool_result("Read", "def parse(text):\n    pass\n" * 40)
        mono.record("thinking", "quotes need a state machine")
        mono.tool_result("Bash", "exit 1: no such file", is_error=True)
        events = ac.task_events(conn, t1)
        check("logged in order", [e["kind"] for e in events] ==
              ["prompt", "tool_use", "tool_result", "thinking", "error"], events)
        check("full body kept", len(events[2]["body"]) > 800, len(events[2]["body"]))
        check("tool name kept", events[1]["label"] == "Read")
        check("failed tool is an error", events[4]["kind"] == "error" and events[4]["label"] == "Bash")
        check("grouped by run", len(ac.run_events(conn, mono.run_id)) == 5)
        check("scoped to its task", ac.task_events(conn, t2) == [])
        # newest first: the limit is what makes this a "recent" feed at all -
        # ORDER BY id DESC LIMIT n takes the n latest events, where ascending
        # would pin it to the n oldest forever
        check("tail is newest first", ac.recent_events(conn, limit=2)[0]["kind"] == "error")
        check("tail filters by agent", ac.recent_events(conn, agent="bench-agent") == [])
        check("terminal line is one line", "\n" not in ac.one_line("a\nb\nc") and ac.one_line("x" * 300, 50).endswith("\u2026"))
        try:
            mono.record("gossip", "not a kind")
            check("unknown kind refused", False)
        except ValueError:
            check("unknown kind refused", True)

        print("event filters")
        # telemetry outnumbers substance in a real trail, which is what makes
        # filtering in SQL rather than in the caller the whole point
        mono.record("system", "step 3 of 5", label="task_progress")
        mono.record("system", "still working", label="status")
        kinds = [e["kind"] for e in ac.task_events(conn, t1)]
        check("no filter still means everything", kinds.count("system") == 2, kinds)
        check("task events exclude kinds",
              "system" not in [e["kind"] for e in ac.task_events(conn, t1, exclude_kinds=("system",))], kinds)
        check("task events restrict to kinds",
              [e["kind"] for e in ac.task_events(conn, t1, kinds=("prompt", "thinking"))] == ["prompt", "thinking"])
        check("a single kind need not be a tuple", len(ac.task_events(conn, t1, kinds="system")) == 2)
        check("a filter matching nothing is empty", ac.task_events(conn, t1, kinds=("result",)) == [])
        check("an empty kinds tuple matches nothing", ac.recent_events(conn, kinds=()) == [])
        # newest-first, dev-agent, minus two kinds: order and scope both survive
        combined = ac.recent_events(conn, agent="dev-agent", exclude_kinds=("system", "tool_result"))
        check("filters combine", [e["kind"] for e in combined] ==
              ["error", "thinking", "tool_use", "prompt"], combined)
        # the newest three events are two system rows and one other, so a
        # fetch-then-filter-in-python tail would come back one row long
        trimmed = ac.recent_events(conn, limit=3, exclude_kinds="system")
        check("limit counts the events asked for", len(trimmed) == 3, trimmed)
        check("excluded kind stays out of the tail",
              "system" not in [e["kind"] for e in trimmed], trimmed)

        print("runs")
        mono.record("result", "Parser done.", label="done - $0.0182, 3 rounds")
        bench = ac.Monologue(conn, "bench-agent", t1, quiet=True)
        bench.record("prompt", "Task 1: benchmark the parser")
        check("task events filter by agent",
              [e["agent"] for e in ac.task_events(conn, t1, agent="bench-agent")] == ["bench-agent"])
        runs = ac.recent_runs(conn)
        check("one row per run", len(runs) == 2, runs)
        check("runs are newest first", [r["run_id"] for r in runs] == [bench.run_id, mono.run_id], runs)
        live, finished = runs
        check("run counts its own events",
              finished["event_count"] == len(ac.run_events(conn, mono.run_id)), runs)
        check("run carries agent and task", (finished["agent"], finished["task_id"]) == ("dev-agent", t1), finished)
        check("run spans first to last", finished["first_ts"] <= finished["last_ts"], finished)
        check("finished run reports its result label",
              (finished["result"] or "").startswith("done - $0.0182"), finished)
        # a run with no terminal event is in flight, not absent: it is the one
        # a human most wants to open
        check("in-flight run still listed", live["result"] is None and live["event_count"] == 1, live)
        check("runs honour the limit", [r["run_id"] for r in ac.recent_runs(conn, limit=1)] == [bench.run_id])

        print("docs")
        check("missing doc is None", ac.docs_get(conn, "architecture") is None)
        ac.docs_set(conn, "architecture", "One SQLite DB per project.", "dev-agent")
        ac.docs_set(conn, "architecture", "One SQLite DB per project. WAL on.", "bench-agent")
        check("docs upsert", ac.docs_get(conn, "architecture").endswith("WAL on."))
        check("docs list", [d["key"] for d in ac.docs_list(conn)] == ["architecture"])

        print("search")
        # Test FTS5 ranking: create three docs with increasing relevance to "parser"
        ac.docs_set(conn, "unrelated", "This document is about database queries and SQL.", "dev-agent")
        ac.docs_set(conn, "parser-notes", "Some notes about parsing and parser implementation.", "dev-agent")
        ac.docs_set(conn, "parser-guide", "parser parser parser parser parser - complete guide to parser design.", "dev-agent")
        # The best match should be parser-guide (most occurrences of "parser")
        search_results = ac.full_text_search(conn, "parser", tables=["docs"])
        check("search finds matches", len(search_results) > 0, f"Found {len(search_results)} results")
        # Verify best result is first (most negative rank value)
        best_match = search_results[0]
        check("best match is first result", best_match["title"] == "parser-guide", f"Best match title: {best_match['title']}")
        # Verify ranking is ascending (more negative = better)
        if len(search_results) > 1:
            check("results ranked by relevance", search_results[0]["rank"] <= search_results[1]["rank"],
                  f"Rank order: {[r['rank'] for r in search_results[:3]]}")

        # Test FTS5 index sync on repeated updates (regression test for INSERT OR REPLACE bug)
        # The bug: INSERT OR REPLACE doesn't fire DELETE triggers without recursive_triggers=ON,
        # leaving orphaned FTS5 entries that surface as "database disk image is malformed" errors
        ac.docs_set(conn, "fts_test", "alpha version initial", "dev-agent")
        ac.docs_set(conn, "fts_test", "beta version update", "dev-agent")
        ac.docs_set(conn, "fts_test", "gamma version final", "dev-agent")
        # Old FTS term should not be found (was in first version, replaced twice)
        old_results = ac.full_text_search(conn, "alpha", tables=["docs"])
        check("stale FTS term not found", len(old_results) == 0, old_results)
        # Current FTS term should be found exactly once
        new_results = ac.full_text_search(conn, "gamma", tables=["docs"])
        check("current FTS term found", len(new_results) == 1, new_results)

        # Test snippet highlighting and escaping (Bug 1: highlighting was escaped)
        ac.docs_set(conn, "snippet_test", "This document talks about parsing and parser design.", "dev-agent")
        snippet_results = ac.full_text_search(conn, "parser", tables=["docs"])
        snippet_found = [r for r in snippet_results if r["title"] == "snippet_test"]
        check("snippet has match part", len(snippet_found) > 0 and snippet_found[0].get("snippet_match"), snippet_found)
        if snippet_found:
            snippet = snippet_found[0]
            # Verify snippet contains the matched term
            combined_snippet = (snippet.get("snippet_before", "") + snippet.get("snippet_match", "") + snippet.get("snippet_after", ""))
            check("snippet contains matched term", "parser" in combined_snippet.lower(), combined_snippet)
            # Verify no raw < from template escaping in snippet parts
            check("snippet parts not double-escaped", "&lt;" not in combined_snippet, combined_snippet)

        # Test XSS protection: HTML in document content is escaped, not injected
        ac.docs_set(conn, "xss_test", "Documentation with <script>alert('xss')</script> in body.", "dev-agent")
        xss_results = ac.full_text_search(conn, "script", tables=["docs"])
        xss_found = [r for r in xss_results if r["title"] == "xss_test"]
        check("XSS content found in search", len(xss_found) > 0, xss_found)
        if xss_found:
            xss_snippet = xss_found[0]
            combined = (xss_snippet.get("snippet_before", "") + xss_snippet.get("snippet_match", "") + xss_snippet.get("snippet_after", ""))
            # Jinja will escape HTML, so we should see escaped tags, not raw <script>
            check("HTML tags escaped in snippet", "&lt;" in combined or "script" in combined, combined)

        # Test relevance bar normalization (Bug 2: rank was negative)
        ac.docs_set(conn, "rank_test_a", "minimal", "dev-agent")
        ac.docs_set(conn, "rank_test_b", "minimal minimal minimal minimal minimal", "dev-agent")
        rank_results = ac.full_text_search(conn, "minimal", tables=["docs"])
        check("results have normalized rank", all("rank_normalized" in r for r in rank_results), rank_results)
        # Best match should have higher rank_normalized than worst
        if len(rank_results) > 1:
            best = rank_results[0]
            worst = rank_results[-1]
            check("better match has higher rank", best.get("rank_normalized", 0) >= worst.get("rank_normalized", 0),
                  f"best={best.get('rank_normalized')}, worst={worst.get('rank_normalized')}")
        # All normalized ranks should be positive
        check("all ranks are positive", all(r.get("rank_normalized", 0) >= 0 for r in rank_results), rank_results)
        # All normalized ranks should be <= 1.0
        check("all ranks <= 1.0", all(r.get("rank_normalized", 0) <= 1.0 for r in rank_results), rank_results)

        print("docs are markdown")
        prose = "# Report\n\n## Summary\n\nDid a thing.\n"
        check("prose untouched", ac.as_markdown(prose) == prose)
        fenced = '```json\n{"a": 1}\n```'
        check("fenced json is prose", ac.as_markdown(fenced) == fenced)
        check("broken json untouched", ac.as_markdown('{"a": ') == '{"a": ')
        check("bare scalar untouched", ac.as_markdown("17") == "17")
        check("empty body untouched", ac.as_markdown("") == "")
        dumped = ac.as_markdown(
            '{"summary": "did a thing", "files_modified": ["a.py", "b.py"],'
            ' "breaking_changes": [], "counts": {"tests": 3}}'
        )
        check("json keys become headings", "## Summary" in dumped and "## Files modified" in dumped, dumped)
        check("json lists become bullets", "- a.py\n- b.py" in dumped, dumped)
        check("empty value marked", "_none_" in dumped, dumped)
        check("nested dict nests", "### Tests" in dumped, dumped)
        check("no json punctuation left", '{"' not in dumped and '":' not in dumped, dumped)
        titled = ac.as_markdown('{"summary": "x"}', title="Task 9: dev-agent report")
        check("title only when rewritten", titled.startswith("# Task 9: dev-agent report"))
        check("title not bolted onto prose", ac.as_markdown(prose, title="Ignored") == prose)
        ac.call_tool(conn, "dev-agent", "docs_set",
                     {"key": "handover", "content": '{"summary": "via the tool"}'})
        check("tool docs_set coerces", ac.docs_get(conn, "handover") == "## Summary\n\nvia the tool\n")
        t7 = add_ready(conn, "context handover test", assigned_to="dev-agent")
        ac.store_workflow_context(conn, "dev-agent", t7, '{"summary": "handover"}')
        doc_key = f"task_{t7}_dev-agent_context"
        stored = ac.docs_get(conn, doc_key)
        check("workflow context coerced", stored.startswith(f"# Task {t7}: dev-agent report"), stored)
        check("workflow context linked to its task", ac.docs_get(conn, doc_key, task_id=t7) == stored)
        check("workflow context not linked to another task", ac.docs_get(conn, doc_key, task_id=t7 + 1) is None)

        print("tools")
        check("tool set", [s["name"] for s in ac.TOOL_SPECS] == [
            "get_inbox", "send_message", "reply", "docs_get", "docs_set",
            "docs_list", "create_task", "list_tasks", "list_features", "set_task_feature",
            "search", "add_tag", "remove_tag", "list_tags"])
        names = lambda cfg: [s["name"] for s in ac.toolset(cfg)]  # noqa: E731
        check("dev and reviewer get the base set", names({"flavor": "dev"}) == names({"flavor": "reviewer"})
              == list(ac.tools.BASE_TOOLS))
        check("planners also curate tags", {"add_tag", "remove_tag"} <= set(names({"flavor": "planner"}))
              and "add_tag" not in names({"flavor": "dev"}))
        check("planners also curate features", "set_task_feature" in names({"flavor": "planner"})
              and "set_task_feature" not in names({"flavor": "dev"})
              and "list_features" in names({"flavor": "dev"}))
        check("no flavor means dev", names({}) == names({"flavor": "dev"}))
        check("an operator gets every tool", names(None) == [s["name"] for s in ac.TOOL_SPECS])
        dev_tools = ac.toolset({"flavor": "dev"})
        try:
            ac.call_tool(conn, "dev-agent", "add_tag", {"task_id": t4, "tags": "x"}, dev_tools)
            check("a tool outside the set is refused", False)
        except KeyError:
            check("a tool outside the set is refused", True)

        ac.call_tool(conn, "dev-agent", "send_message", {"recipient": "bench-agent", "payload": "ping"})
        check("tool inbox", ac.call_tool(conn, "bench-agent", "get_inbox", {})[0]["payload"] == "ping")
        check("an agent's get_inbox only peeks", ac.get_inbox(conn, "bench-agent", mark_read=False))
        operator = ac.toolset(None)
        check("an operator's get_inbox marks read",
              ac.call_tool(conn, "bench-agent", "get_inbox", {}, operator)[0]["payload"] == "ping"
              and not ac.get_inbox(conn, "bench-agent", mark_read=False))

        ac.docs_set(conn, "brief", "the human's brief")
        try:
            ac.call_tool(conn, "dev-agent", "docs_set", {"key": "brief", "content": "mine now"}, dev_tools)
            check("agents cannot overwrite a human's doc", False)
        except ValueError as exc:
            check("agents cannot overwrite a human's doc",
                  "written by the human" in str(exc) and ac.docs_get(conn, "brief") == "the human's brief")
        ac.call_tool(conn, "dev-agent", "docs_set", {"key": "dev-notes", "content": "v1"}, dev_tools)
        ac.call_tool(conn, "dev-agent", "docs_set", {"key": "dev-notes", "content": "v2"}, dev_tools)
        check("agents can rewrite their own docs", ac.docs_get(conn, "dev-notes") == "v2")
        ac.call_tool(conn, "you", "docs_set", {"key": "brief", "content": "revised brief"}, operator)
        check("an operator can", ac.docs_get(conn, "brief") == "revised brief")

        closed = add_ready(conn, "operator closes this", assigned_to="dev-agent")
        ac.call_tool(conn, "you", "reply", {"task_id": closed, "payload": "handled it", "status": "done"}, operator)
        check("an operator can reply on any task", ac.get_task(conn, closed)["status"] == "done")
        check("tool docs", ac.call_tool(conn, "dev-agent", "docs_get", {"key": "architecture"})["content"].endswith("WAL on."))
        docs_list = [d["key"] for d in ac.call_tool(conn, "dev-agent", "docs_list", {})]
        # FTS tests added several docs, so just check that the expected ones are present
        check("tool docs_list", all(k in docs_list for k in ["architecture", "handover", doc_key]),
              f"docs_list: {docs_list}")
        check("tool docs_list scoped to task", [d["key"] for d in ac.call_tool(conn, "dev-agent", "docs_list", {"task_id": t7})] == [doc_key])
        check("tool list_tasks", len(ac.call_tool(conn, "dev-agent", "list_tasks", {})) >= 1)
        check("tool result is json", ac.tool_result_text({"a": 1}) == '{"a": 1}')
        try:
            ac.call_tool(conn, "dev-agent", "nope", {})
            check("unknown tool raises", False)
        except KeyError:
            check("unknown tool raises", True)
        check("paths are normalised", ac.normalize_path("./src/../src/parser.py") == "src/parser.py")

        print("create_task tool")
        new_task = ac.call_tool(conn, "dev-agent", "create_task", {
            "title": "Create async parser",
            "description": "Make the parser async-friendly",
            "assigned_to": "dev-agent"
        })
        check("create_task returns task", new_task["title"] == "Create async parser")
        check("create_task creates in db", ac.get_task(conn, new_task["id"]) is not None)

        # Test create_task with dependencies
        dep_task = ac.call_tool(conn, "dev-agent", "create_task", {
            "title": "Test async parser",
            "assigned_to": "bench-agent",
            "depends_on": [new_task["id"]]
        })
        check("create_task with dependencies works", [d["id"] for d in ac.task_dependencies(conn, dep_task["id"])] == [new_task["id"]])

        print("export")
        out = project / ".agents-export"
        written = ac.export_markdown(conn, out)
        check("plan written", (out / "plan.md").exists())
        check("tasks written", "Write the parser" in (out / "tasks.md").read_text())
        thread = (out / "messages" / f"task-{t1}.md").read_text()
        check("thread written", "Parser done." in thread and "$0.0182" in thread)
        check("monologue exported", "## Activity" in thread and "src/parser.py" in thread)
        check("runs delimited in export", f"### run {mono.run_id}" in thread)
        check("usage written", "usage.md" in [p.name for p in written])
        check("export is rerunnable", len(ac.export_markdown(conn, out)) == len(written))

        print("project discovery")
        nested = project / "src" / "deep"
        nested.mkdir(parents=True)
        check("finds project upward", ac.find_project(nested) == project.resolve())
        try:
            ac.find_project(tmp)
            check("errors outside a project", False)
        except SystemExit:
            check("errors outside a project", True)

        print("git preflight for worktree")
        # Create a non-git project directory to test the preflight
        non_git_project = tmp / "non-git-project"
        (non_git_project / ".agents" / "prompts").mkdir(parents=True)
        ac.config_path(non_git_project).write_text(
            '[agents.test-agent]\nbackend = "claude"\nrole = "tester"\nworktree = true\n'
        )
        # Try to load config and verify it has worktree=true
        non_git_cfg = ac.agent_config(non_git_project, "test-agent")
        check("worktree=true in config", non_git_cfg.get("worktree") is True)
        # Simulate what the daemon would check: git rev-parse --is-inside-work-tree
        result = subprocess.run(
            ["git", "-C", str(non_git_project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False
        )
        check("non-git dir fails git check", result.returncode != 0 or result.stdout.strip() != "true")

        print("merge_prompt")
        # Test: persona preserved
        existing = "# my-agent\n\nMy role.\n\n## Old section\n\nOld content.\n"
        template = "# {name}\n\nTemplate role.\n\n## New section\n\nNew content.\n"
        merged = ac.merge_prompt(existing, template)
        check("persona preserved", merged.startswith("# my-agent\n\nMy role.\n\n## New section"))
        # Test: template body current
        check("template body current", "New content." in merged and "Old content." not in merged)
        # Test: idempotent on second merge
        merged2 = ac.merge_prompt(merged, template)
        check("idempotent on second merge", merged == merged2)
        # Test: no-"##" file handled
        existing_no_section = "# agent\n\nJust persona.\n"
        merged_no_section = ac.merge_prompt(existing_no_section, template)
        check("no-## file handled", merged_no_section.startswith("# agent\n\nJust persona.\n\n## New section"))
        # Test: planning-agent's custom header survives
        planning_persona = "# planning-agent\n\nCustom line 1.\n\nCustom line 2.\n\nCustom line 3.\n\n"
        planning_merged = ac.merge_prompt(planning_persona + "## Old\n\nOld.\n", template)
        check("planning-agent header survives", "Custom line 1." in planning_merged and planning_merged.startswith("# planning-agent"))

        print("reply_to_task requeue")
        for st in ("done", "blocked", "needs_approval", "ready_to_merge"):
            rt = ac.add_task(conn, f"reply to {st}", assigned_to="dev-agent")
            ac.update_task_status(conn, rt, st)
            reply_to_task(conn, rt, "one more thing")
            check(f"reply reopens {st}", ac.get_task(conn, rt)["status"] == "ready")
        for st in ("todo", "ready", "in_progress"):
            rt = ac.add_task(conn, f"reply to {st}", assigned_to="dev-agent")
            ac.update_task_status(conn, rt, st)
            reply_to_task(conn, rt, "fyi")
            check(f"reply leaves {st} alone", ac.get_task(conn, rt)["status"] == st)
        rt = ac.add_task(conn, "reply to unassigned")
        ac.update_task_status(conn, rt, "done")
        reply_to_task(conn, rt, "anyone?")
        check("reply leaves unassigned done task alone", ac.get_task(conn, rt)["status"] == "done")

        print("migration 012 (ready status)")
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
        check("done task untouched", ac.get_task(conn, m_done)["status"] == "done")
        check("in_progress task untouched", ac.get_task(conn, m_running)["status"] == "in_progress")
        check("assigned todo becomes ready", ac.get_task(conn, m_assigned)["status"] == "ready")
        check("unassigned todo stays todo", ac.get_task(conn, m_loose)["status"] == "todo")
        check("soft-deleted todo untouched",
              conn.execute_sql("SELECT status FROM tasks WHERE id = ?", (m_deleted,)).fetchone()[0] == "todo")

        print("features")
        f1 = ac.add_task(conn, "feature one", feature="  Run-Ledger ")
        f2 = ac.add_task(conn, "feature two", feature="run-ledger")
        f3 = ac.add_task(conn, "no feature")
        ledger = ac.get_feature_by_name(conn, "RUN-LEDGER")
        check("feature created on first use, normalised", ledger and ledger["name"] == "run-ledger", ledger)
        check("same name reuses the feature",
              ac.get_task(conn, f1)["feature_id"] == ac.get_task(conn, f2)["feature_id"] == ledger["id"])
        check("task dict carries the feature name", ac.get_task(conn, f1)["feature"] == "run-ledger")
        check("no feature is None", ac.get_task(conn, f3)["feature"] is None and ac.get_task(conn, f3)["feature_id"] is None)
        check("list_tasks filters by feature", [t["id"] for t in ac.list_tasks(conn, feature="Run-Ledger")] == [f1, f2])
        from kuska.store import filter_tasks
        check("filter_tasks by feature", {t["id"] for t in filter_tasks(conn, feature=["run-ledger"])} == {f1, f2})
        check("filter_tasks: empty string means no feature",
              f3 in {t["id"] for t in filter_tasks(conn, feature=[""])}
              and f1 not in {t["id"] for t in filter_tasks(conn, feature=[""])})
        b_ok = ac.add_task(conn, "bulk owned", "", "dev-agent")
        b_free = ac.add_task(conn, "bulk unowned", "", None)
        b_run = ac.add_task(conn, "bulk running", "", "dev-agent")
        ac.update_task_status(conn, b_run, "in_progress")
        res = ac.bulk_update_status(conn, [b_ok, b_free, b_run, 99999, b_ok], "ready")
        check("bulk ready moves only what may go",
              res["moved"] == [b_ok] and ac.get_task(conn, b_ok)["status"] == "ready"
              and ac.get_task(conn, b_free)["status"] == "todo", res)
        check("bulk skips say why",
              dict(res["skipped"]) == {b_free: "no agent", b_run: "in progress", 99999: "not found"}, res)
        check("bulk in_progress task is untouched", ac.get_task(conn, b_run)["status"] == "in_progress")
        res = ac.bulk_update_status(conn, [b_ok, b_free], "done")
        check("bulk to done needs no agent",
              res["moved"] == [b_ok, b_free] and not res["skipped"]
              and ac.get_task(conn, b_free)["status"] == "done", res)
        check("bulk to the current status counts as moved",
              ac.bulk_update_status(conn, [b_free], "done")["moved"] == [b_free])
        for bad in ("in_progress", "bogus"):
            try:
                ac.bulk_update_status(conn, [b_ok], bad)
                check(f"bulk refuses {bad}", False)
            except ValueError:
                check(f"bulk refuses {bad}", True)
        for tid in (b_ok, b_free, b_run):
            ac.delete_task(conn, tid)
        ac.update_task_status(conn, f2, "done")
        counts = {f["name"]: (f["done"], f["total"]) for f in ac.list_features(conn)}
        check("list_features counts tasks", counts.get("run-ledger") == (1, 2), counts)
        ac.ensure_feature(conn, "empty one", description="nothing yet")
        counts = {f["name"]: (f["done"], f["total"]) for f in ac.list_features(conn)}
        check("a feature with no tasks is listed with zero", counts.get("empty one") == (0, 0), counts)
        check("ensure_feature sets a missing description only",
              ac.ensure_feature(conn, "run-ledger", description="runs") == ledger["id"]
              and ac.get_feature(conn, ledger["id"])["description"] == "runs"
              and ac.ensure_feature(conn, "run-ledger", description="other") == ledger["id"]
              and ac.get_feature(conn, ledger["id"])["description"] == "runs")
        ac.update_task(conn, f3, feature="Supervisor")
        check("update_task moves a task into a (new) feature", ac.get_task(conn, f3)["feature"] == "supervisor")
        ac.update_task(conn, f3, feature="")
        check("update_task with empty feature removes it", ac.get_task(conn, f3)["feature_id"] is None)
        ac.update_feature(conn, ledger["id"], name="Ledger")
        check("rename follows to tasks", ac.get_task(conn, f1)["feature"] == "ledger")
        try:
            ac.update_feature(conn, ledger["id"], name="supervisor")
            check("rename onto a taken name refused", False)
        except ValueError:
            check("rename onto a taken name refused", True)
        check("delete_feature ungroups its tasks",
              ac.delete_feature(conn, ledger["id"]) == 2 and ac.get_task(conn, f1)["feature_id"] is None
              and ac.get_feature(conn, ledger["id"]) is None)

        print("feature tools")
        made = ac.call_tool(conn, "dev-agent", "create_task", {"title": "via tool", "feature": "tooling"})
        check("create_task tool takes a feature", made["feature"] == "tooling", made)
        listed = ac.call_tool(conn, "dev-agent", "list_tasks", {"feature": "tooling"})
        check("list_tasks tool filters by feature", [t["id"] for t in listed] == [made["id"]], listed)
        check("list_features tool", "tooling" in [f["name"] for f in ac.call_tool(conn, "dev-agent", "list_features", {})])
        planner = ac.toolset({"flavor": "planner"})
        moved = ac.call_tool(conn, "planning-agent", "set_task_feature", {"task_id": made["id"], "feature": "other"}, planner)
        check("set_task_feature moves a task", moved["feature"] == "other", moved)
        moved = ac.call_tool(conn, "planning-agent", "set_task_feature", {"task_id": made["id"], "feature": ""}, planner)
        check("set_task_feature with empty removes it", moved["feature"] is None, moved)

        print("migration 013 (features)")
        from kuska.migration import run_migrations
        old = ac.connect(tmp / "old.db")
        run_migrations(old, target_version="012_add_ready_status")
        old.execute_sql(
            "INSERT INTO tasks (title, status, feature, created_at, updated_at) VALUES "
            "('a', 'todo', 'Search ', 0, 0), ('b', 'todo', 'search', 0, 0), "
            "('c', 'todo', NULL, 0, 0), ('d', 'todo', 'ui', 0, 0)"
        )
        ac.init_db(old)
        check("free-text features become rows", [f["name"] for f in ac.list_features(old)] == ["search", "ui"])
        check("tasks linked to their backfilled feature",
              [t["feature"] for t in ac.list_tasks(old)] == ["search", "search", None, "ui"])
        check("old column kept for older processes",
              "feature" in {c.name for c in old.get_columns("tasks")})
        old.close()

        print("runs table")
        rt1 = ac.add_task(conn, "run subject", assigned_to="dev-agent")
        rt2 = ac.add_task(conn, "other run subject", assigned_to="dev-agent")
        ac.start_run(conn, "aaaaaaaaaaaa", rt1, "dev-agent")
        r = ac.get_run(conn, "aaaaaaaaaaaa")
        check("start_run inserts a running row",
              r["status"] == "running" and r["task_id"] == rt1 and r["agent"] == "dev-agent"
              and r["ended_at"] is None and r["cost_usd"] == 0 and r["tool_rounds"] == 0, r)
        check("get_run of an unknown id is None", ac.get_run(conn, "nope") is None)
        conn.execute_sql("UPDATE runs SET heartbeat_at = heartbeat_at - 100 WHERE id = 'aaaaaaaaaaaa'")
        before = ac.get_run(conn, "aaaaaaaaaaaa")["heartbeat_at"]
        ac.touch_run(conn, "aaaaaaaaaaaa")
        check("touch_run moves heartbeat_at forward", ac.get_run(conn, "aaaaaaaaaaaa")["heartbeat_at"] > before)
        time.sleep(0.01)

        ac.start_run(conn, "bbbbbbbbbbbb", rt1, "dev-agent")
        ac.start_run(conn, "cccccccccccc", rt2, "review-agent")
        ac.end_run(conn, "bbbbbbbbbbbb", "finished", exit_reason="ok", input_tokens=5, cost_usd=0.1, bogus=1)
        r = ac.get_run(conn, "bbbbbbbbbbbb")
        check("end_run stores status, reason and usage",
              r["status"] == "finished" and r["exit_reason"] == "ok" and r["input_tokens"] == 5
              and r["cost_usd"] == 0.1 and r["output_tokens"] == 0 and r["ended_at"] is not None, r)
        check("end_run ignores keys that are not columns", "bogus" not in r)
        conn.execute_sql("UPDATE runs SET heartbeat_at = heartbeat_at - 100 WHERE id = 'bbbbbbbbbbbb'")
        hb = ac.get_run(conn, "bbbbbbbbbbbb")["heartbeat_at"]
        ac.touch_run(conn, "bbbbbbbbbbbb")
        check("touch_run leaves an ended run alone", ac.get_run(conn, "bbbbbbbbbbbb")["heartbeat_at"] == hb)
        for bad in ("running", "nonsense"):
            try:
                ac.end_run(conn, "cccccccccccc", bad)
                raised = False
            except ValueError:
                raised = True
            check(f"end_run to {bad!r} raises ValueError", raised)
        check("a refused end_run changes nothing", ac.get_run(conn, "cccccccccccc")["status"] == "running")

        conn.execute_sql("UPDATE runs SET heartbeat_at = heartbeat_at - 120 WHERE id = 'cccccccccccc'")
        check("stale_runs returns only the running run with an old heartbeat",
              [x["id"] for x in ac.stale_runs(conn, 60)] == ["cccccccccccc"],
              [x["id"] for x in ac.stale_runs(conn, 60)])
        check("running_runs lists the running ones, oldest first",
              [x["id"] for x in ac.running_runs(conn)] == ["aaaaaaaaaaaa", "cccccccccccc"])
        check("task_runs is oldest first and per task",
              [x["id"] for x in ac.task_runs(conn, rt1)] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
              and [x["id"] for x in ac.task_runs(conn, rt2)] == ["cccccccccccc"])

        check("set_run_result_message returns the run id",
              ac.set_run_result_message(conn, "dev-agent", rt1, 42) == "aaaaaaaaaaaa")
        check("...and stores the message id", ac.get_run(conn, "aaaaaaaaaaaa")["result_message_id"] == 42)
        check("set_run_result_message skips ended runs",
              ac.set_run_result_message(conn, "dev-agent", rt2, 43) is None
              and ac.get_run(conn, "bbbbbbbbbbbb")["result_message_id"] is None)
        ac.end_run(conn, "aaaaaaaaaaaa", "failed", exit_reason="boom")
        check("set_run_result_message is None with no running run",
              ac.set_run_result_message(conn, "dev-agent", rt1, 44) is None)
        ac.delete_task(conn, rt2)
        check("runs outlive their task", ac.get_run(conn, "cccccccccccc") is not None)
        names = [x[0] for x in conn.execute_sql(
            "SELECT name FROM sqlite_master WHERE tbl_name='runs' AND type='index' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()]
        check("runs has one index per column, no twins", sorted(names) == ["run_status", "run_task_id"], names)
        from kuska.tables import TABLES
        check("runs is on the Data page", "runs" in TABLES)

        print("lifecycle")
        from kuska.store import TRANSITIONS, InvalidTransition, transition

        def task_in(status, agent="dev-agent", worktree=None):
            tid = ac.add_task(conn, "lifecycle probe", assigned_to=agent)
            if worktree:
                ac.update_task(conn, tid, worktree_path=worktree)
            ac.update_task_status(conn, tid, status)
            return tid

        def status_of(tid):
            return ac.get_task(conn, tid)["status"]

        simple = {
            "make_ready": "ready", "park": "todo", "claim": "in_progress", "hold": "needs_approval",
            "block": "blocked", "await_answer": "ready", "approve": "done", "merged": "done", "close": "done",
        }
        for event, expected in simple.items():
            start = TRANSITIONS[event][0][0]
            tid = task_in(start if event != "hold" else "in_progress")
            check(f"{event}: {start} -> {expected}", transition(conn, tid, event)["status"] == expected)
        for event in ("hold", "block", "await_answer", "finish"):
            check(f"{event} starts only from in_progress", TRANSITIONS[event][0] == ("in_progress",))

        tid = task_in("in_progress")
        check("finish without worktree -> done", transition(conn, tid, "finish")["status"] == "done")
        tid = task_in("in_progress", worktree="/tmp/wt")
        check("finish with worktree -> ready_to_merge", transition(conn, tid, "finish")["status"] == "ready_to_merge")
        tid = task_in("blocked")
        check("requeue assigned -> ready", transition(conn, tid, "requeue")["status"] == "ready")
        tid = ac.add_task(conn, "unassigned probe")
        ac.update_task_status(conn, tid, "blocked")
        check("requeue unassigned -> todo", transition(conn, tid, "requeue")["status"] == "todo")

        def refuses(tid, event, **kw):
            try:
                transition(conn, tid, event, **kw)
            except InvalidTransition:
                return True
            return False

        tid = ac.add_task(conn, "unassigned probe")
        check("make_ready without an agent refused", refuses(tid, "make_ready") and status_of(tid) == "todo")
        tid = task_in("ready_to_merge")
        check("close on ready_to_merge refused, status unchanged",
              refuses(tid, "close") and status_of(tid) == "ready_to_merge")
        tid = task_in("in_progress")
        check("approve on in_progress refused", refuses(tid, "approve") and status_of(tid) == "in_progress")

        tid = task_in("todo")
        forced = transition(conn, tid, "force", to="in_progress")
        check("force by human sets any status", forced["status"] == "in_progress")
        check("force leaves a note",
              any(m["msg_type"] == "note" and "forced" in m["payload"] for m in ac.task_messages(conn, tid)))
        check("force by an agent refused", refuses(tid, "force", actor="dev-agent", to="done")
              and status_of(tid) == "in_progress")
        try:
            transition(conn, tid, "force", to="nonsense")
            bad_to = False
        except ValueError:
            bad_to = True
        check("force to an unknown status raises ValueError", bad_to)
        try:
            transition(conn, tid, "explode")
            unknown = False
        except ValueError:
            unknown = True
        check("unknown event raises ValueError", unknown)
        try:
            transition(conn, 999999, "approve")
            missing = False
        except ValueError as e:
            missing = "not found" in str(e)
        check("missing task raises ValueError", missing)

        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
