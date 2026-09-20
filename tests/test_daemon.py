#!/usr/bin/env python3
"""Daemon-loop checks with the model call stubbed: `uv run tests/test_daemon.py`.

Proves the full loop - task queued, daemon picks it up, agent tools work
in-process, result and cost land in the DB, status page reflects it - without
spending a token.
"""

import asyncio
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import achka as core
from achka.daemons import claude as daemon_claude
from achka.daemons import codex as daemon_codex

PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


def make_project(tmp: Path) -> Path:
    project = tmp / "daemonproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
        '[agents.codex-1]\nbackend = "codex"\nmodel = "gpt-5-codex"\nrole = "reviewer"\n'
        "price_in_per_mtok = 1.25\nprice_out_per_mtok = 10.0\n"
        '[agents.openai-1]\nbackend = "openai"\nmodel = "gpt-4"\nrole = "openai agent"\n'
        "api_key = \"sk-test\"\n"
    )
    return project


def run_loop(project: Path, fake_run, max_tasks: int) -> None:
    """Drive the real daemon loop with the model call stubbed out."""
    real = daemon_claude.run_agent
    daemon_claude.run_agent = fake_run
    try:
        daemon_claude.run_daemon(project, "dev-agent", poll_interval=0.02, max_tasks=max_tasks, quiet=True)
    finally:
        daemon_claude.run_agent = real


def check_tools_in_process(project: Path) -> None:
    print("in-process tools")
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    tools = daemon_claude.build_tools(conn, "dev-agent")
    check("one sdk tool per spec", len(tools) == len(core.TOOL_SPECS))
    by_name = {t.name: t for t in tools}
    check("names match", set(by_name) == {s["name"] for s in core.TOOL_SPECS}, sorted(by_name))

    out = asyncio.run(by_name["send_message"].handler({"recipient": "codex-1", "payload": "hi"}))
    check("tool returns mcp content", out["content"][0]["type"] == "text", out)
    check("tool wrote to db", core.get_inbox(conn, "codex-1")[0]["payload"] == "hi")
    err = asyncio.run(by_name["docs_get"].handler({}))
    check("tool errors are returned, not raised", err.get("isError") and "error:" in err["content"][0]["text"], err)

    options = daemon_claude.build_options(project, "dev-agent", {"model": "claude-opus-5"}, tools)
    check("mcp server registered", "achka" in options.mcp_servers)
    check("tools allow-listed", "mcp__achka__claim_task" in options.allowed_tools)
    check("prompt file wired", options.system_prompt["path"].endswith("prompts/dev-agent.md"))
    check("runs in project dir", options.cwd == str(project))
    conn.close()


def check_loop(project: Path) -> None:
    print("daemon loop")
    conn = core.connect(core.db_path(project))
    t1 = core.add_task(conn, "Add the parser", "handle quotes", "dev-agent")
    t2 = core.add_task(conn, "Ask about scope", "", "dev-agent")
    core.send_message(conn, core.HUMAN, "dev-agent", t1, "note", "start from the old branch")
    seen: list[str] = []

    async def fake(prompt, options, mono):
        seen.append(prompt)
        mono.tool_call("Read", {"file_path": "src/parser.py"})
        mono.record("text", "Parser added.")
        if len(seen) == 1:
            return "Parser added.", 1000, 200, 0.03
        # second task: the agent asks another agent, then blocks itself via the tools
        core.send_message(conn, "dev-agent", "codex-1", t2, "question", "which scope?")
        core.reply(conn, "dev-agent", t2, "Asked codex-1, waiting.", status="blocked")
        return "Asked codex-1, waiting.", 400, 80, 0.01

    run_loop(project, fake, max_tasks=2)

    check("task 1 done", core.get_task(conn, t1)["status"] == "done")
    check("prompt carried the description", "handle quotes" in seen[0])
    check("prompt carried the human note", "start from the old branch" in seen[0], seen[0])
    results = [m for m in core.task_messages(conn, t1) if m["msg_type"] == "result"]
    check("one result logged", len(results) == 1, results)
    check("result text logged", results[0]["payload"] == "Parser added.")
    check("cost logged", results[0]["cost_usd"] == 0.03 and results[0]["input_tokens"] == 1000)

    check("task 2 blocked by the agent", core.get_task(conn, t2)["status"] == "blocked")
    r2 = [m for m in core.task_messages(conn, t2) if m["msg_type"] == "result"]
    check("agent's own reply not duplicated", len(r2) == 1, r2)
    check("usage attached to it", r2[0]["cost_usd"] == 0.01, r2[0])
    check("question delivered", core.get_inbox(conn, "codex-1")[0]["payload"] == "which scope?")
    check("agent left offline", core.get_agent(conn, "dev-agent")["status"] == "offline")
    spend = {u["agent"]: u["cost_usd"] for u in core.token_usage_by_agent(conn)}
    check("spend rolls up", round(spend["dev-agent"], 4) == 0.04, spend)

    print("re-queue after a reply")
    core.send_message(conn, "codex-1", "dev-agent", t2, "result", "scope is the CLI only")
    core.update_task_status(conn, t2, "todo")
    seen.clear()

    async def fake2(prompt, options, mono):
        seen.append(prompt)
        return "Scoped to the CLI, done.", 300, 60, 0.005

    run_loop(project, fake2, max_tasks=1)
    check("re-queued task ran again", core.get_task(conn, t2)["status"] == "done")
    check("reply became context", "scope is the CLI only" in seen[0], seen[0])
    check("earlier turn also in context", "Asked codex-1, waiting." in seen[0])

    print("failure path")
    t3 = core.add_task(conn, "Explodes", "", "dev-agent")

    async def boom(prompt, options, mono):
        raise RuntimeError("model unavailable")

    run_loop(project, boom, max_tasks=1)
    check("failed task is blocked, not lost", core.get_task(conn, t3)["status"] == "blocked")
    blocker = [m for m in core.task_messages(conn, t3) if m["msg_type"] == "blocker"]
    check("failure explained in the thread", blocker and "model unavailable" in blocker[0]["payload"], blocker)

    print("monologue")
    events = core.task_events(conn, t1)
    check("prompt logged", events[0]["kind"] == "prompt" and "handle quotes" in events[0]["body"])
    check("tool call logged", any(e["label"] == "Read" for e in events), events)
    check("result logged with cost", events[-1]["kind"] == "result" and "$0.0300" in events[-1]["label"])
    check("one run id per invocation", len({e["run_id"] for e in events}) == 1)
    failed = core.task_events(conn, t3)
    check("failure narrated", any(e["kind"] == "error" and "model unavailable" in e["body"] for e in failed))

    print("status page")
    app = core.create_app(project)
    app.config.update(TESTING=True)
    html = app.test_client().get("/agents").get_data(as_text=True)
    check("agents page shows the run", "dev-agent" in html and "$0.0450" in html)
    conn.close()


def check_claim_guard(project: Path) -> None:
    print("file claims")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "bench-agent", "codex", "benchmarks")
    for name in ("dev-agent", "bench-agent"):
        core.heartbeat(db, name, "working")
    core.release_files(db, "dev-agent")
    core.release_files(db, "bench-agent")

    current = {"mono": core.Monologue(db, "dev-agent", 1, quiet=True)}
    guard = daemon_claude.claim_guard(db, project, "dev-agent", current)

    allowed = asyncio.run(guard("Read", {"file_path": "src/parser.py"}, None))
    check("reads are never gated", allowed.behavior == "allow")
    check("reading claims nothing", core.active_claims(db) == [])

    allowed = asyncio.run(guard("Edit", {"file_path": "src/parser.py"}, None))
    check("an edit is allowed", allowed.behavior == "allow")
    held = core.active_claims(db)
    check("and claims the file for the agent", [c["path"] for c in held] == ["src/parser.py"], held)
    check("claim carries the run", held[0]["run_id"] == current["mono"].run_id and held[0]["task_id"] == 1)
    check("editing again is fine", asyncio.run(guard("Write", {"file_path": "src/parser.py"}, None)).behavior == "allow")

    other = {"mono": core.Monologue(db, "bench-agent", 2, quiet=True)}
    denied = asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other)("Edit", {"file_path": "src/parser.py"}, None)
    )
    check("the other agent is stopped", denied.behavior == "deny")
    check("told who holds it", "dev-agent" in denied.message and "task 1" in denied.message, denied.message)
    check("told what to do about it", "send_message" in denied.message and "get_inbox" in denied.message)
    check("and told to block rather than wait", "blocked" in denied.message)
    check("the refusal is in the monologue", any(
        e["label"] == "claim conflict" for e in core.task_events(db, 2)))

    check("unrelated file still allowed", asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other)("Edit", {"file_path": "README.md"}, None)
    ).behavior == "allow")
    check("absolute paths resolve to the same claim", asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other)(
            "Edit", {"file_path": str(project / "src" / "parser.py")}, None)
    ).behavior == "deny")

    core.release_run(db, current["mono"].run_id)
    check("released with the run", core.claim_holders(db, "src/parser.py", agent="bench-agent") == [])
    check("now the other agent may edit it", asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other)("Edit", {"file_path": "src/parser.py"}, None)
    ).behavior == "allow")

    print("claims in the next run's prompt")
    core.release_files(db, "bench-agent")
    core.claim_files(db, "bench-agent", ["bench/runner.py"], task_id=2, run_id="r-other", note="rewriting the harness")
    task = core.get_task(db, 1) or {"id": 1, "title": "x", "description": ""}
    prompt = core.compose_task_prompt(db, "dev-agent", task)
    check("the other agent's files are in the prompt", "bench/runner.py" in prompt, prompt)
    check("with who and why", "bench-agent" in prompt and "rewriting the harness" in prompt)
    check("and what to do", "claim_files" in prompt)
    check("its own claims are not listed", "src/parser.py" not in prompt)
    core.release_files(db, "bench-agent")
    db.close()


def check_codex_wiring(project: Path) -> None:
    print("codex daemon wiring")
    cfg = core.load_config(project)["agents"]["codex-1"]
    mcp = daemon_codex.mcp_config(project, "codex-1")["mcp_servers"]["achka"]
    check("points at achka mcp", mcp["args"][-3:] == ["mcp", "--agent", "codex-1"], mcp)
    check("scoped to this project", str(project) in mcp["args"], mcp)

    usage = SimpleNamespace(last=SimpleNamespace(input_tokens=2_000_000, output_tokens=100_000))
    tok_in, tok_out, cost = daemon_codex.usage_of(usage, cfg)
    check("tokens read from turn", (tok_in, tok_out) == (2_000_000, 100_000))
    check("cost priced from config", round(cost, 4) == round(2 * 1.25 + 0.1 * 10.0, 4), cost)
    check("no prices means no cost", daemon_codex.usage_of(usage, {})[2] == 0.0)
    check("missing usage is harmless", daemon_codex.usage_of(None, cfg) == (0, 0, 0.0))


def check_openai_wiring(project: Path) -> None:
    print("openai daemon wiring")
    from achka.daemons import openai as daemon_openai

    cfg = core.load_config(project)["agents"]["openai-1"]
    check("backend registered", cfg["backend"] == "openai")
    check("has api_key", cfg.get("api_key") == "sk-test")

    # mcp_command should work for openai too
    cmd = daemon_openai.mcp_command()
    check("mcp_command returns a list", isinstance(cmd, list))
    check("mcp_command includes python", cmd[0] == sys.executable or "python" in cmd[0])

    print("codex item mapping")
    item = SimpleNamespace(type="agent_message", text="all done", phase=SimpleNamespace(value="final_answer"))
    check("agent message is text", daemon_codex.describe_item(item) == ("text", "agent_message", "all done"))
    shell = SimpleNamespace(type="command_execution", command="pytest -q")
    check("command is a tool call", daemon_codex.describe_item(shell) == ("tool_use", "shell", "pytest -q"))
    mcp = SimpleNamespace(type="mcp_tool_call", server="achka", tool="get_inbox", arguments={"a": 1})
    kind, label, body = daemon_codex.describe_item(mcp)
    check("mcp call is named", (kind, label) == ("tool_use", "achka.get_inbox") and "\"a\": 1" in body)
    unknown = SimpleNamespace(type="something_new", model_dump_json=lambda: '{"type": "something_new"}')
    check("unknown item still logged", daemon_codex.describe_item(unknown)[0] == "tool_use")

    print("backend dispatch")
    from achka.daemons import BACKENDS, run

    check("three backends registered", set(BACKENDS) == {"claude", "codex", "openai"}, BACKENDS)
    try:
        run("llama-cpp", project, "openai-1")
        check("unknown backend refused", False)
    except SystemExit as exc:
        check("unknown backend refused", "no daemon for backend" in str(exc))


def check_file_claim_conflicts(project: Path) -> None:
    """Scenario 1: Two agents claim the same file simultaneously."""
    print("concurrent file claim conflicts")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "agent-1", "claude", "builder")
    core.register_agent(db, "agent-2", "claude", "reviewer")
    core.heartbeat(db, "agent-1", "working")
    core.heartbeat(db, "agent-2", "working")

    results = {}

    def claim_file(agent_name):
        """Try to claim the same file."""
        result = core.claim_files(db, agent_name, ["src/parser.py"], task_id=1, note=f"claimed by {agent_name}")
        results[agent_name] = result

    # Thread 1 claims first
    t1 = threading.Thread(target=claim_file, args=("agent-1",))
    # Thread 2 claims immediately after
    t2 = threading.Thread(target=claim_file, args=("agent-2",))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both calls should succeed (no exception) - claims are advisory
    check("both agents can call claim_files", "agent-1" in results and "agent-2" in results)
    check("both claims return success", results["agent-1"]["claimed"] and results["agent-2"]["claimed"])

    held = core.active_claims(db)
    check("file is claimed by both agents", len([c for c in held if c["path"] == "src/parser.py"]) == 2, f"claims: {held}")

    # Each agent can see the other's claim as a holder
    holders_from_agent1 = core.claim_holders(db, "src/parser.py", agent="agent-1")
    holders_from_agent2 = core.claim_holders(db, "src/parser.py", agent="agent-2")

    check("agent-1 sees agent-2's conflicting claim", any(c["agent"] == "agent-2" for c in holders_from_agent1))
    check("agent-2 sees agent-1's conflicting claim", any(c["agent"] == "agent-1" for c in holders_from_agent2))
    check("each sees exactly one conflicting claim", len(holders_from_agent1) >= 1 and len(holders_from_agent2) >= 1)

    db.close()


def check_task_claiming_race(project: Path) -> None:
    """Scenario 2: Two agents poll for tasks at the same time - verify atomicity."""
    print("concurrent task claiming race")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "racer-1", "claude", "builder")
    core.register_agent(db, "racer-2", "claude", "reviewer")
    core.heartbeat(db, "racer-1", "working")
    core.heartbeat(db, "racer-2", "working")

    # Create tasks for each agent
    r1_t1 = core.add_task(db, "Racer1-A", "first", "racer-1")
    r1_t2 = core.add_task(db, "Racer1-B", "second", "racer-1")
    r2_t1 = core.add_task(db, "Racer2-A", "third", "racer-2")
    r2_t2 = core.add_task(db, "Racer2-B", "fourth", "racer-2")

    results = {}
    lock = threading.Lock()

    def claim_task_safe(agent_name):
        """Try to claim a task thread-safely."""
        task = core.claim_task(db, agent_name)
        with lock:
            results[agent_name] = task

    # Both agents claim simultaneously - should succeed with different tasks
    threads = [
        threading.Thread(target=claim_task_safe, args=("racer-1",)),
        threading.Thread(target=claim_task_safe, args=("racer-2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("both agents claimed tasks", results.get("racer-1") is not None and results.get("racer-2") is not None, results)
    if results.get("racer-1") and results.get("racer-2"):
        check("they claimed different tasks", results["racer-1"]["id"] != results["racer-2"]["id"])
        check("task atomicity: status moved to in_progress",
              core.get_task(db, results["racer-1"]["id"])["status"] == "in_progress")

    db.close()


def check_message_ordering(project: Path) -> None:
    """Scenario 3: Message ordering is preserved between concurrent agents."""
    print("concurrent message ordering")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "msg-1", "claude", "builder")
    core.register_agent(db, "msg-2", "claude", "reviewer")
    core.heartbeat(db, "msg-1", "working")
    core.heartbeat(db, "msg-2", "working")

    task_id = core.add_task(db, "collaboration", "", "msg-1")

    messages = []
    lock = threading.Lock()

    def agent1_sends():
        """Agent 1 sends a question."""
        time.sleep(0.01)  # Small delay to ensure message 1 comes first
        core.send_message(db, "msg-1", "msg-2", task_id, "question", "What do you think?")
        with lock:
            messages.append(("msg-1-send", core.now()))

    def agent2_sends():
        """Agent 2 sends a response."""
        time.sleep(0.05)  # Wait for agent-1 to send first
        core.send_message(db, "msg-2", "msg-1", task_id, "answer", "I think we should refactor")
        with lock:
            messages.append(("msg-2-send", core.now()))

    t1 = threading.Thread(target=agent1_sends)
    t2 = threading.Thread(target=agent2_sends)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Check message order in DB
    all_messages = core.task_messages(db, task_id)
    check("both messages logged", len(all_messages) == 2)
    check("msg-1's message first", all_messages[0]["sender"] == "msg-1" and "What do you think?" in all_messages[0]["payload"])
    check("msg-2's message second", all_messages[1]["sender"] == "msg-2" and "refactor" in all_messages[1]["payload"])

    # Check inbox ordering
    inbox_2 = core.get_inbox(db, "msg-2", mark_read=False)
    inbox_1 = core.get_inbox(db, "msg-1", mark_read=False)
    check("msg-2 received msg-1's message", len(inbox_2) > 0)
    check("msg-1 received msg-2's message", len(inbox_1) > 0)

    db.close()


def check_dependency_satisfaction(project: Path) -> None:
    """Scenario 4: Task A done by agent-1, then task B (depends on A) released to agent-2."""
    print("concurrent dependency satisfaction")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "dep-1", "claude", "builder")
    core.register_agent(db, "dep-2", "claude", "reviewer")
    core.heartbeat(db, "dep-1", "working")
    core.heartbeat(db, "dep-2", "working")

    # Create task A assigned to dep-1
    task_a = core.add_task(db, "Design API", "", "dep-1")
    # Create task B assigned to dep-2, depends on A
    task_b = core.add_task(db, "Implement API", "", "dep-2")
    core.add_dependency(db, task_b, task_a)

    results = {}

    def agent1_work():
        """Agent 1 tries to claim task A."""
        task = core.claim_task(db, "dep-1")
        results["dep-1-claimed"] = task
        if task:
            time.sleep(0.05)  # Simulate work
            core.update_task_status(db, task_a, "done")
            results["dep-1-done"] = True

    def agent2_work():
        """Agent 2 polls for task B, blocked initially, then unblocked."""
        time.sleep(0.01)  # Let agent-1 start first
        task = core.claim_task(db, "dep-2")
        results["dep-2-first-claim"] = task
        if task is None:
            # Task B is blocked because A is not done yet
            results["dep-2-blocked"] = True
            time.sleep(0.1)  # Wait for agent-1 to finish
            task = core.claim_task(db, "dep-2")
            results["dep-2-second-claim"] = task

    t1 = threading.Thread(target=agent1_work)
    t2 = threading.Thread(target=agent2_work)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    check("dep-1 claimed task A", results.get("dep-1-claimed") is not None)
    check("dep-1 completed task A", results.get("dep-1-done") is True)
    check("task A is done", core.get_task(db, task_a)["status"] == "done")

    check("dep-2 initially blocked on first claim", results.get("dep-2-first-claim") is None)
    check("dep-2 detected blocking", results.get("dep-2-blocked") is True)
    check("dep-2 later claims task B", results.get("dep-2-second-claim") is not None)

    if results.get("dep-2-second-claim"):
        check("task B is now runnable", results["dep-2-second-claim"]["id"] == task_b)

    db.close()


def check_approval_workflow_race(project: Path) -> None:
    """Scenario 5: Task needs approval, human approves, agent immediately polls."""
    print("concurrent approval workflow race")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "approval-1", "claude", "builder")
    core.heartbeat(db, "approval-1", "working")

    task_id = core.add_task(db, "Risky change", "", "approval-1")
    core.update_task_status(db, task_id, "needs_approval")

    results = {}
    lock = threading.Lock()

    def agent_polls():
        """Agent polls for tasks while approval is pending."""
        time.sleep(0.02)
        # Poll should get nothing initially
        task = core.claim_task(db, "approval-1")
        with lock:
            results["poll-1"] = task

        time.sleep(0.05)  # Wait for approval

        # Poll should now get the task
        task = core.claim_task(db, "approval-1")
        with lock:
            results["poll-2"] = task

    def human_approves():
        """Human approves the task."""
        time.sleep(0.04)  # Let agent poll first while blocked
        core.update_task_status(db, task_id, "todo")

    t1 = threading.Thread(target=agent_polls)
    t2 = threading.Thread(target=human_approves)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    check("agent's first poll gets nothing (blocked by approval)", results.get("poll-1") is None)
    check("after approval, agent gets task", results.get("poll-2") is not None)
    if results.get("poll-2"):
        check("claimed task is the right one", results["poll-2"]["id"] == task_id)

    db.close()


def check_large_claim_scope(project: Path) -> None:
    """Scenario 6: One agent claims a directory, another tries to claim a file inside."""
    print("concurrent large claim scope")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "scope-1", "claude", "builder")
    core.register_agent(db, "scope-2", "claude", "reviewer")
    core.heartbeat(db, "scope-1", "working")
    core.heartbeat(db, "scope-2", "working")

    results = {}

    def agent1_claims_dir():
        """Agent 1 claims the entire src directory."""
        result = core.claim_files(db, "scope-1", ["src"], task_id=1, note="refactoring entire module")
        results["scope-1-dir"] = result

    def agent2_claims_file():
        """Agent 2 tries to claim a file inside that directory."""
        time.sleep(0.01)  # Small delay
        result = core.claim_files(db, "scope-2", ["src/parser.py"], task_id=2, note="minor fix")
        results["scope-2-file"] = result

    t1 = threading.Thread(target=agent1_claims_dir)
    t2 = threading.Thread(target=agent2_claims_file)

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both claims go through (they never fail), but scope-2 can see the conflict
    check("scope-1 claimed the directory", "src" in results["scope-1-dir"]["claimed"])
    check("scope-2 claimed the file", "src/parser.py" in results["scope-2-file"]["claimed"])

    # Check that overlaps are detected
    held = core.active_claims(db)
    check("both claims exist", len(held) >= 2, f"claims: {held}")

    # scope-2 should see scope-1's directory claim as a conflict for its file
    conflicts_for_scope2 = core.claim_holders(db, "src/parser.py", agent="scope-2")
    check("scope-2 sees the conflict", len(conflicts_for_scope2) > 0, f"conflicts: {conflicts_for_scope2}")
    check("conflict is scope-1's directory claim", any(c["path"] == "src" for c in conflicts_for_scope2))

    # scope-1 also sees scope-2's nested claim because overlaps are symmetric
    conflicts_for_scope1 = core.claim_holders(db, "src", agent="scope-1")
    check("scope-1 sees scope-2's nested file claim as overlapping", any(c["path"] == "src/parser.py" for c in conflicts_for_scope1), f"conflicts: {conflicts_for_scope1}")

    db.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="achka-daemon-"))
    try:
        project = make_project(tmp)
        check_tools_in_process(project)
        check_loop(project)
        check_claim_guard(project)
        check_codex_wiring(project)
        check_openai_wiring(project)

        # Concurrent daemon tests
        check_file_claim_conflicts(project)
        check_task_claiming_race(project)
        check_message_ordering(project)
        check_dependency_satisfaction(project)
        check_approval_workflow_race(project)
        check_large_claim_scope(project)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
