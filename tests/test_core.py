#!/usr/bin/env python3
"""Standalone checks for kuska - no test framework, just `uv run tests/test_core.py`."""

import shutil
import sys
import tempfile
import threading
import time
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

        print("tasks")
        t1 = ac.add_task(conn, "Write the parser", "Handle nested quotes", "dev-agent")
        t2 = ac.add_task(conn, "Benchmark it", assigned_to="bench-agent")
        ac.add_task(conn, "Unassigned idea")
        check("ids", (t1, t2) == (1, 2))
        check("list", len(ac.list_tasks(conn)) == 3)
        check("filter", [t["id"] for t in ac.list_tasks(conn, "todo")] == [1, 2, 3])

        print("claiming")
        claimed = ac.claim_task(conn, "dev-agent")
        check("claimed own task", claimed["id"] == t1, claimed)
        check("claim is atomic", ac.claim_task(conn, "dev-agent") is None)
        check("status moved", ac.get_task(conn, t1)["status"] == "in_progress")
        check("other agent unaffected", ac.claim_task(conn, "bench-agent")["id"] == t2)

        print("dependencies and approval")
        d1 = ac.add_task(conn, "design", assigned_to="dev-agent")
        d2 = ac.add_task(conn, "implement", assigned_to="dev-agent")
        d3 = ac.add_task(conn, "document", assigned_to="dev-agent")
        loose = ac.add_task(conn, "unrelated chore", assigned_to="dev-agent")
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
        t4 = ac.add_task(conn, "Late arrival", assigned_to="dev-agent")
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

        print("docs")
        check("missing doc is None", ac.docs_get(conn, "architecture") is None)
        ac.docs_set(conn, "architecture", "One SQLite DB per project.", "dev-agent")
        ac.docs_set(conn, "architecture", "One SQLite DB per project. WAL on.", "bench-agent")
        check("docs upsert", ac.docs_get(conn, "architecture").endswith("WAL on."))
        check("docs list", [d["key"] for d in ac.docs_list(conn)] == ["architecture"])

        print("tools")
        check("tool set", [s["name"] for s in ac.TOOL_SPECS] == [
            "get_inbox", "send_message", "claim_task", "reply", "docs_get", "docs_set",
            "docs_list", "claim_files", "release_files", "who_has", "heartbeat",
            "create_task", "list_tasks", "search"])
        ac.call_tool(conn, "dev-agent", "heartbeat", {"status": "working", "task_id": t4})
        check("tool heartbeat", ac.get_agent(conn, "dev-agent")["current_task_id"] == t4)
        ac.call_tool(conn, "dev-agent", "send_message", {"recipient": "bench-agent", "payload": "ping"})
        check("tool inbox", ac.call_tool(conn, "bench-agent", "get_inbox", {})[0]["payload"] == "ping")
        check("tool docs", ac.call_tool(conn, "dev-agent", "docs_get", {"key": "architecture"})["content"].endswith("WAL on."))
        check("tool docs_list", [d["key"] for d in ac.call_tool(conn, "dev-agent", "docs_list", {})] == ["architecture"])
        check("tool list_tasks", len(ac.call_tool(conn, "dev-agent", "list_tasks", {})) >= 1)
        check("tool result is json", ac.tool_result_text({"a": 1}) == '{"a": 1}')
        try:
            ac.call_tool(conn, "dev-agent", "nope", {})
            check("unknown tool raises", False)
        except KeyError:
            check("unknown tool raises", True)

        print("file claims")
        ac.heartbeat(conn, "dev-agent", "working")
        ac.heartbeat(conn, "bench-agent", "working")
        taken = ac.claim_files(conn, "dev-agent", ["src/parser.py", "src/lib"], task_id=t1, run_id="run-a", note="rewriting")
        check("claim taken", taken["claimed"] == ["src/parser.py", "src/lib"], taken)
        check("nobody else held them", taken["held_by_others"] == [])
        check("same file is held", [c["agent"] for c in ac.claim_holders(conn, "src/parser.py", agent="bench-agent")] == ["dev-agent"])
        check("directory covers what is under it", [c["path"] for c in ac.claim_holders(conn, "src/lib/util.py", agent="bench-agent")] == ["src/lib"])
        check("a file above it also overlaps", ac.claim_holders(conn, "src", agent="bench-agent") != [])
        check("unrelated file is free", ac.claim_holders(conn, "README.md", agent="bench-agent") == [])
        check("an agent never blocks itself", ac.claim_holders(conn, "src/parser.py", agent="dev-agent") == [])
        check("paths are normalised", ac.normalize_path("./src/../src/parser.py") == "src/parser.py")

        clash = ac.claim_files(conn, "bench-agent", ["src/parser.py"], run_id="run-b")
        check("claiming anyway reports the clash", [c["agent"] for c in clash["held_by_others"]] == ["dev-agent"], clash)
        check("both claims exist", len(ac.active_claims(conn)) == 3)
        check("note travels with the claim", [c["note"] for c in ac.active_claims(conn) if c["agent"] == "dev-agent"][0] == "rewriting")

        check("release by run", ac.release_run(conn, "run-a") == 2)
        check("only the other agent is left", [c["agent"] for c in ac.active_claims(conn)] == ["bench-agent"])
        check("release everything an agent holds", ac.release_files(conn, "bench-agent") == 1)
        check("nothing held", ac.active_claims(conn) == [])

        ac.claim_files(conn, "dev-agent", ["src/parser.py"], run_id="run-c")
        ac.heartbeat(conn, "dev-agent", "offline")
        conn.execute_sql("UPDATE agents SET last_heartbeat = ? WHERE name = ?",
                         (ac.now() - ac.CLAIM_STALE_AFTER - 10, "dev-agent"))
        check("a dead agent holds nothing", ac.active_claims(conn) == [])
        check("so the file is free again", ac.claim_holders(conn, "src/parser.py", agent="bench-agent") == [])
        ac.release_files(conn, "dev-agent")

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

        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
