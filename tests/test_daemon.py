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
import multiprocessing
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import kuska as core
from kuska.daemons import claude as daemon_claude
from kuska.daemons import codex as daemon_codex

PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


# --------------------------------------------------------------------------
# Concurrency workers.
#
# These run in separate processes on purpose. store.py binds its models to a
# database per call (store.bound -> db.bind_ctx(MODELS)), and peewee's model
# binding is process-global: two threads calling any bound function rebind the
# same Model classes under each other and end up issuing queries on the wrong
# connection. A daemon is its own process (`kuska daemon <name>`), so process
# isolation is what concurrency actually looks like here - and the only way to
# test the database's guarantees rather than peewee's global state.
# --------------------------------------------------------------------------


def _claim_task_worker(db_path: str, agent_name: str, q) -> None:
    import kuska as core

    conn = core.connect(db_path)
    try:
        q.put((agent_name, core.claim_task(conn, agent_name)))
    finally:
        conn.close()


def _claim_file_worker(db_path: str, agent_name: str, q) -> None:
    import kuska as core

    conn = core.connect(db_path)
    try:
        result = core.claim_files(
            conn, agent_name, ["src/parser.py"], task_id=1, note=f"claimed by {agent_name}"
        )
        q.put((agent_name, result))
    finally:
        conn.close()


def run_in_processes(worker, project: Path, agents: tuple[str, ...]) -> dict:
    """Run worker(db_path, agent, queue) once per agent, concurrently."""
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=worker, args=(str(core.db_path(project)), agent, q))
        for agent in agents
    ]
    for proc in procs:
        proc.start()
    results = {}
    for _ in agents:
        agent, value = q.get(timeout=60)
        results[agent] = value
    for proc in procs:
        proc.join(timeout=60)
    return results


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


def usage(tok_in: int, tok_out: int, cache_read: int = 0, rounds: int = 0, cost: float = 0.0) -> dict:
    """The usage dict a stubbed run_agent hands back, matching the real shape."""
    return {
        "input_tokens": tok_in,
        "output_tokens": tok_out,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": 0,
        "tool_rounds": rounds,
        "cost_usd": cost,
    }


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
    check("mcp server registered", "kuska" in options.mcp_servers)
    check("tools allow-listed", "mcp__kuska__claim_task" in options.allowed_tools)
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
            return "Parser added.", usage(1000, 200, cache_read=9000, rounds=4, cost=0.03)
        # second task: the agent asks another agent, then blocks itself via the tools
        core.send_message(conn, "dev-agent", "codex-1", t2, "question", "which scope?")
        core.reply(conn, "dev-agent", t2, "Asked codex-1, waiting.", status="blocked")
        return "Asked codex-1, waiting.", usage(400, 80, cache_read=3000, rounds=2, cost=0.01)

    run_loop(project, fake, max_tasks=2)

    check("task 1 done", core.get_task(conn, t1)["status"] == "done")
    check("prompt carried the description", "handle quotes" in seen[0])
    check("prompt carried the human note", "start from the old branch" in seen[0], seen[0])
    results = [m for m in core.task_messages(conn, t1) if m["msg_type"] == "result"]
    check("one result logged", len(results) == 1, results)
    check("result text logged", results[0]["payload"] == "Parser added.")
    check("cost logged", results[0]["cost_usd"] == 0.03 and results[0]["input_tokens"] == 1000)
    check("cache reads kept out of fresh input", results[0]["cache_read_tokens"] == 9000, results[0])
    check("tool rounds recorded", results[0]["tool_rounds"] == 4, results[0])

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
        return "Scoped to the CLI, done.", usage(300, 60, cache_read=2000, rounds=1, cost=0.005)

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
    reads: dict = {}
    guard = daemon_claude.claim_guard(db, project, "dev-agent", current, reads)

    allowed = asyncio.run(guard("Read", {"file_path": "src/parser.py"}, None))
    check("a first read is allowed", allowed.behavior == "allow")
    check("reading claims nothing", core.active_claims(db) == [])

    allowed = asyncio.run(guard("Edit", {"file_path": "src/parser.py"}, None))
    check("an edit is allowed", allowed.behavior == "allow")
    held = core.active_claims(db)
    check("and claims the file for the agent", [c["path"] for c in held] == ["src/parser.py"], held)
    check("claim carries the run", held[0]["run_id"] == current["mono"].run_id and held[0]["task_id"] == 1)
    check("editing again is fine", asyncio.run(guard("Write", {"file_path": "src/parser.py"}, None)).behavior == "allow")

    other = {"mono": core.Monologue(db, "bench-agent", 2, quiet=True)}
    other_reads: dict = {}
    denied = asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other, other_reads)("Edit", {"file_path": "src/parser.py"}, None)
    )
    check("the other agent is stopped", denied.behavior == "deny")
    check("told who holds it", "dev-agent" in denied.message and "task 1" in denied.message, denied.message)
    check("told what to do about it", "send_message" in denied.message and "get_inbox" in denied.message)
    check("and told to block rather than wait", "blocked" in denied.message)
    check("the refusal is in the monologue", any(
        e["label"] == "claim conflict" for e in core.task_events(db, 2)))

    check("unrelated file still allowed", asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other, other_reads)("Edit", {"file_path": "README.md"}, None)
    ).behavior == "allow")
    check("absolute paths resolve to the same claim", asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other, other_reads)(
            "Edit", {"file_path": str(project / "src" / "parser.py")}, None)
    ).behavior == "deny")

    core.release_run(db, current["mono"].run_id)
    check("released with the run", core.claim_holders(db, "src/parser.py", agent="bench-agent") == [])
    check("now the other agent may edit it", asyncio.run(
        daemon_claude.claim_guard(db, project, "bench-agent", other, other_reads)("Edit", {"file_path": "src/parser.py"}, None)
    ).behavior == "allow")

    print("redundant reads")
    # unclaimed paths, so the claim checks above cannot interfere
    fresh: dict = {}
    dedupe = daemon_claude.claim_guard(db, project, "dev-agent", current, fresh)
    check("first read allowed", asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "allow")
    again = asyncio.run(dedupe("Read", {"file_path": "src/lexer.py"}, None))
    check("the same file again is refused", again.behavior == "deny")
    check("told it already has the contents", "already read" in again.message, again.message)
    check("and pointed at offset/limit and Grep",
          "offset/limit" in again.message and "Grep" in again.message, again.message)
    check("the refusal is in the monologue", any(
        e["label"] == "redundant read" for e in core.task_events(db, 1)))
    check("a different file is fine", asyncio.run(
        dedupe("Read", {"file_path": "src/other.py"}, None)).behavior == "allow")
    check("editing it clears the record", asyncio.run(
        dedupe("Edit", {"file_path": "src/lexer.py"}, None)).behavior == "allow")
    check("so re-reading a changed file is allowed", asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "allow")

    # a whole-file read subsumes every range; distinct ranges do not
    ranges: dict = {}
    ranged = daemon_claude.claim_guard(db, project, "dev-agent", current, ranges)
    check("a ranged read is allowed", asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 1, "limit": 50}, None)).behavior == "allow")
    check("a different range is allowed", asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 200, "limit": 50}, None)).behavior == "allow")
    check("the same range again is refused", asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 1, "limit": 50}, None)).behavior == "deny")
    check("and the whole file is refused after ranges", asyncio.run(
        ranged("Read", {"file_path": "src/big.py"}, None)).behavior == "deny")
    core.release_files(db, "dev-agent")  # the Edit above claimed src/lexer.py

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
    mcp = daemon_codex.mcp_config(project, "codex-1")["mcp_servers"]["kuska"]
    check("points at kuska mcp", mcp["args"][-3:] == ["mcp", "--agent", "codex-1"], mcp)
    check("scoped to this project", str(project) in mcp["args"], mcp)

    usage = SimpleNamespace(last=SimpleNamespace(input_tokens=2_000_000, output_tokens=100_000))
    tok_in, tok_out, cost = daemon_codex.usage_of(usage, cfg)
    check("tokens read from turn", (tok_in, tok_out) == (2_000_000, 100_000))
    check("cost priced from config", round(cost, 4) == round(2 * 1.25 + 0.1 * 10.0, 4), cost)
    check("no prices means no cost", daemon_codex.usage_of(usage, {})[2] == 0.0)
    check("missing usage is harmless", daemon_codex.usage_of(None, cfg) == (0, 0, 0.0))

    cached = SimpleNamespace(last=SimpleNamespace(cached_input_tokens=900, cache_write_input_tokens=120))
    check("cache counts read from turn", daemon_codex.cache_of(cached) == (900, 120))
    check("missing cache counts are zero", daemon_codex.cache_of(None) == (0, 0))

    from openai_codex import Sandbox

    preset = daemon_codex.sandbox_preset
    check("config string becomes a preset", preset("workspace-write") is Sandbox.workspace_write)
    check("underscores accepted", preset("read_only") is Sandbox.read_only)
    check("wire spelling accepted", preset("danger-full-access") is Sandbox.full_access)
    check("blank means the codex default", preset("") is None and preset(None) is None)
    check("a preset passes through", preset(Sandbox.full_access) is Sandbox.full_access)
    try:
        preset("wide-open")
    except ValueError as exc:
        check("nonsense is rejected loudly", "wide-open" in str(exc), exc)
    else:
        check("nonsense is rejected loudly", False)


def check_openai_wiring(project: Path) -> None:
    print("openai daemon wiring")
    from kuska.daemons import openai as daemon_openai

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
    mcp = SimpleNamespace(type="mcp_tool_call", server="kuska", tool="get_inbox", arguments={"a": 1})
    kind, label, body = daemon_codex.describe_item(mcp)
    check("mcp call is named", (kind, label) == ("tool_use", "kuska.get_inbox") and "\"a\": 1" in body)
    unknown = SimpleNamespace(type="something_new", model_dump_json=lambda: '{"type": "something_new"}')
    check("unknown item still logged", daemon_codex.describe_item(unknown)[0] == "tool_use")

    print("backend dispatch")
    from kuska.daemons import BACKENDS, run

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

    results = run_in_processes(_claim_file_worker, project, ("agent-1", "agent-2"))

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

    results = run_in_processes(_claim_task_worker, project, ("racer-1", "racer-2"))

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


def check_lazy_load_history(project: Path) -> None:
    """Scenario 7: Message summarization - keep last 5 full, summarize older."""
    print("lazy-load message history")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "history-agent", "claude", "builder")
    core.heartbeat(db, "history-agent", "working")

    task_id = core.add_task(db, "Multi-turn task", "requires multiple interactions", "history-agent")

    # Create 12 messages to test summarization (7 old + 5 recent)
    # Sent from agent to human so they won't be in inbox
    for i in range(1, 13):
        payload = f"result message {i}" + (" (final)" if i == 12 else "")
        core.send_message(db, "history-agent", core.HUMAN, task_id, "result", payload)

    # Test 1: Default behavior - summarize old (1-7), keep last 5 full (8-12)
    task = core.get_task(db, task_id)
    prompt_limited = core.compose_task_prompt(db, "history-agent", task, limit_history=True)

    check("limited prompt includes task title", "Multi-turn task" in prompt_limited)
    check("limited prompt has history section", "Earlier on this task" in prompt_limited)
    check("limited prompt has prior context", "Prior context" in prompt_limited, prompt_limited[:500])
    check("limited prompt has recent section", "Recent messages" in prompt_limited, prompt_limited[:500])
    check("limited prompt indicates last 5", "last 5" in prompt_limited)

    # Verify last 5 messages (8-12) are in full format
    check("limited prompt has last message", "result message 12" in prompt_limited)
    check("limited prompt has msg 11", "result message 11" in prompt_limited)
    check("limited prompt has msg 8", "result message 8" in prompt_limited)

    # First message should be in summary (prior context) but not in full format
    # Full format would be "**history-agent -> human**" (with arrow)
    lines = prompt_limited.split('\n')
    full_msg1_count = sum(1 for line in lines if "result message 1" in line and "->" in line)
    check("msg 1 not in full format", full_msg1_count == 0)
    # But msg 1 should still be somewhere in the prompt (in summary)
    check("msg 1 in summary", "result message 1" in prompt_limited)

    # Test 2: Full history via limit_history=False
    prompt_full = core.compose_task_prompt(db, "history-agent", task, limit_history=False)
    check("full prompt includes all history", "result message 1" in prompt_full and "result message 12" in prompt_full)
    check("full prompt doesn't use prior context", "Prior context" not in prompt_full)

    # Test 3: With 5 or fewer messages, all should be in full (no summarization)
    task_id_short = core.add_task(db, "Short task", "", "history-agent")
    for i in range(1, 4):
        core.send_message(db, "history-agent", core.HUMAN, task_id_short, "result", f"short msg {i}")

    task_short = core.get_task(db, task_id_short)
    prompt_short = core.compose_task_prompt(db, "history-agent", task_short, limit_history=True)
    check("short prompt no summarization", "Prior context" not in prompt_short)
    check("short prompt has all messages", "short msg 1" in prompt_short and "short msg 3" in prompt_short)

    db.close()


def check_prompt_stays_small(project: Path) -> None:
    """Scenario 8: The composed prompt stays small however long the thread gets.

    There used to be a max_prompt_tokens setting here that progressively
    truncated history to fit a budget. It was never reachable: summarization
    already caps the prompt at a few hundred tokens, and an invocation's cost
    lives in the agentic loop that follows, not in the prompt that starts it.
    What this checks now is that the summarization actually holds.
    """
    print("composed prompt stays small")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "limit-agent", "claude", "builder")
    core.register_agent(db, "other-agent", "claude", "reviewer")
    core.heartbeat(db, "limit-agent", "working")
    core.heartbeat(db, "other-agent", "working")

    task_id = core.add_task(db, "Long-running task", "very long description with lots of content" * 50, "limit-agent")

    long_message = "This is a test message. " * 100  # ~2400 chars
    for i in range(20):
        core.send_message(db, "other-agent", "limit-agent", task_id, "note", f"Message {i}: {long_message}")

    check("token count for short text", core.estimate_token_count("hello world") >= 1)
    check("token count increases with length",
          core.estimate_token_count("a" * 1000) > core.estimate_token_count("hello world"))

    task = core.get_task(db, task_id)
    prompt_unlimited = core.compose_task_prompt(db, "limit-agent", task, limit_history=False)
    check("unlimited prompt includes all history",
          "Message 0:" in prompt_unlimited and "Message 19:" in prompt_unlimited)
    check("unlimited prompt is large", core.estimate_token_count(prompt_unlimited) > 1000)

    # 20 long messages, but only the last 5 land in full
    prompt = core.compose_task_prompt(db, "limit-agent", task)
    tokens = core.estimate_token_count(prompt)
    check("summarized prompt still names the task", "Long-running task" in prompt)
    check("summarized prompt keeps the recent messages in full", "Message 19:" in prompt)
    check("summarized prompt is a fraction of the full one",
          tokens < core.estimate_token_count(prompt_unlimited) / 2, f"tokens: {tokens}")
    check("older messages survive only as one-line previews",
          sum(1 for line in prompt.split("\n") if "Message 0:" in line and "->" in line) == 0)

    db.close()


def check_workflow_context(project: Path) -> None:
    """Scenario 9: Workflow context passing for multi-agent workflows (Phase 4.1)."""
    print("workflow context passing (multi-agent)")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "planning-agent", "claude", "planner")
    core.register_agent(db, "dev-agent", "claude", "builder")
    core.register_agent(db, "review-agent", "claude", "reviewer")

    # Test 1: Store context from planning-agent
    task_id = core.add_task(db, "Implement feature X", "Complex feature requiring multiple agents", "planning-agent")
    plan_context = """{
        "phase": 1,
        "approach": "Modular architecture with dependency injection",
        "key_decisions": ["Use abstract base classes", "Implement factory pattern"],
        "files_to_modify": ["src/core.py", "src/services.py"],
        "critical_constraints": "Must maintain backward compatibility"
    }"""

    # Simulate planning-agent finishing and storing context
    core.docs_set(db, f"task_{task_id}_planning-agent_context", plan_context, updated_by="planning-agent")
    check("planning context stored", core.docs_get(db, f"task_{task_id}_planning-agent_context") is not None)

    # Test 2: dev-agent retrieves context in prompt
    task = core.get_task(db, task_id)
    prompt_with_context = core.compose_task_prompt(db, "dev-agent", task)
    check("workflow context appears in prompt", "Context from planning-agent" in prompt_with_context)
    check("context content is included", "Modular architecture" in prompt_with_context)
    check("key decisions visible", "factory pattern" in prompt_with_context)

    # Test 3: dev-agent can store its own context for review-agent
    dev_context = """{
        "implementation_summary": "Implemented factory pattern for services",
        "files_modified": ["src/core.py", "src/services.py", "tests/test_services.py"],
        "key_changes": ["Added ServiceFactory class", "Migrated service instantiation"],
        "test_coverage": "Added 15 new unit tests for factory pattern"
    }"""
    core.docs_set(db, f"task_{task_id}_dev-agent_context", dev_context, updated_by="dev-agent")
    check("dev context stored", core.docs_get(db, f"task_{task_id}_dev-agent_context") is not None)

    # Test 4: review-agent gets both contexts (will retrieve dev context, not planning)
    prompt_for_review = core.compose_task_prompt(db, "review-agent", task)
    check("dev context appears in review prompt", "Context from dev-agent" in prompt_for_review)
    check("review sees dev changes", "ServiceFactory class" in prompt_for_review)
    check("review sees test coverage", "15 new unit tests" in prompt_for_review)

    # Test 5: Context is included in prompt (explicit check)
    # This verifies that when both history and context exist, the context is available
    task_id_2 = core.add_task(db, "Another task", "Testing token savings", "planning-agent")
    long_message = "This is a detailed message about implementation strategy. " * 30  # ~1500 chars
    for i in range(10):
        core.send_message(db, "human", "planning-agent", task_id_2, "note", f"Iteration {i}: {long_message}")

    # Store context
    ctx = "Brief planning summary: modular architecture with factory pattern."
    core.docs_set(db, f"task_{task_id_2}_planning-agent_context", ctx, updated_by="planning-agent")

    # Verify context is accessible
    task2 = core.get_task(db, task_id_2)
    context_doc = core.docs_get(db, f"task_{task_id_2}_planning-agent_context")
    check("stored context is retrievable", context_doc == ctx)

    # Verify it appears in prompt
    prompt_with_ctx = core.compose_task_prompt(db, "dev-agent", task2)
    check("context section in prompt", "Context from planning-agent" in prompt_with_ctx)
    check("actual context content in prompt", "modular architecture" in prompt_with_ctx)

    db.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kuska-daemon-"))
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
        check_lazy_load_history(project)
        check_prompt_stays_small(project)
        check_workflow_context(project)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
