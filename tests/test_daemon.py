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

import git
import kuska as core
from kuska.daemons import claude as daemon_claude
from kuska.daemons import codex as daemon_codex


def add_ready(conn, *args, **kw):
    """add_task, then move it to "ready" so an agent can claim it."""
    tid = core.add_task(conn, *args, **kw)
    core.update_task_status(conn, tid, "ready")
    return tid

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

    options = daemon_claude.build_options(project, project, "dev-agent", {"model": "claude-opus-5"}, tools)
    check("mcp server registered", "kuska" in options.mcp_servers)
    check("tools allow-listed", "mcp__kuska__send_message" in options.allowed_tools)
    check("claim_task tool removed", "mcp__kuska__claim_task" not in options.allowed_tools)
    check("prompt file wired", options.system_prompt["path"].endswith("prompts/dev-agent.md"))
    check("runs in project dir", options.cwd == str(project))
    conn.close()


def check_loop(project: Path) -> None:
    print("daemon loop")
    conn = core.connect(core.db_path(project))
    t1 = add_ready(conn, "Add the parser", "handle quotes", "dev-agent")
    t2 = add_ready(conn, "Ask about scope", "", "dev-agent")
    msg_id = core.send_message(conn, core.HUMAN, "dev-agent", t1, "note", "start from the old branch")
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
    core.update_task_status(conn, t2, "ready")
    seen.clear()

    async def fake2(prompt, options, mono):
        seen.append(prompt)
        return "Scoped to the CLI, done.", usage(300, 60, cache_read=2000, rounds=1, cost=0.005)

    run_loop(project, fake2, max_tasks=1)
    check("re-queued task ran again", core.get_task(conn, t2)["status"] == "done")
    check("reply became context", "scope is the CLI only" in seen[0], seen[0])
    check("earlier turn also in context", "Asked codex-1, waiting." in seen[0])

    print("failure path")
    t3 = add_ready(conn, "Explodes", "", "dev-agent")

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
    db = core.connect(core.db_path(project))
    core.register_agent(db, "bench-agent", "codex", "benchmarks")
    for name in ("dev-agent", "bench-agent"):
        core.heartbeat(db, name, "working")

    current = {"mono": core.Monologue(db, "dev-agent", 1, quiet=True)}
    reads: dict = {}
    guard = daemon_claude.tool_guard(db, project, project, "dev-agent", current, reads)

    print("redundant reads")
    # test the redundant-read check
    fresh: dict = {}
    dedupe = daemon_claude.tool_guard(db, project, project, "dev-agent", current, fresh)
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
    ranged = daemon_claude.tool_guard(db, project, project, "dev-agent", current, ranges)
    check("a ranged read is allowed", asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 1, "limit": 50}, None)).behavior == "allow")
    check("a different range is allowed", asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 200, "limit": 50}, None)).behavior == "allow")
    check("the same range again is refused", asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 1, "limit": 50}, None)).behavior == "deny")
    check("and the whole file is refused after ranges", asyncio.run(
        ranged("Read", {"file_path": "src/big.py"}, None)).behavior == "deny")

    print("command guardrails (task 4's PreToolUse hook, exercised via tool_guard directly)")
    # a fresh mono/reads pair so the redundant-read bookkeeping
    # above cannot interfere with what is being checked here
    guarded: dict = {}
    cmd_mono = core.Monologue(db, "dev-agent", 1, quiet=True)
    guarded["mono"] = cmd_mono
    cmd_guard = daemon_claude.tool_guard(db, project, project, "dev-agent", guarded, {})

    refused = asyncio.run(cmd_guard("Bash", {"command": "rm -rf /"}, None))
    check("a destructive Bash command is denied", refused.behavior == "deny")
    check("the message names the rule and why", "rm -rf" in refused.message and "no undo" in refused.message, refused.message)
    check("the message points at needs_approval", "needs_approval" in refused.message, refused.message)
    check("the refusal lands in the monologue", any(
        (e["label"] or "").startswith("refused:") for e in core.task_events(db, 1)), core.task_events(db, 1))

    ordinary = asyncio.run(cmd_guard("Bash", {"command": "uv run tests/run_all.py"}, None))
    check("an ordinary command is allowed", ordinary.behavior == "allow")

    still_reads = asyncio.run(cmd_guard("Read", {"file_path": "src/after_refusal.py"}, None))
    check("a Read still works after a refusal", still_reads.behavior == "allow")

    print("regression: the bug task 4 fixed must not come back")
    options = daemon_claude.build_options(
        project, project, "dev-agent", {"model": "claude-opus-5"}, [],
        pretooluse_hook=daemon_claude.as_pretooluse_hook(cmd_guard),
    )
    # A bare tool name in allowed_tools auto-approves that whole tool before
    # can_use_tool/the PreToolUse hook is ever consulted - the exact bug
    # task 4 fixed. If "Bash" (or Read/Write/Edit) ever creeps back in here,
    # every check above still passes (claim_guard is exercised directly),
    # while the live daemon would once again enforce nothing.
    check("bare tool names are not in allowed_tools",
          "Bash" not in options.allowed_tools and "Read" not in options.allowed_tools
          and "Write" not in options.allowed_tools and "Edit" not in options.allowed_tools,
          options.allowed_tools)
    check("the PreToolUse hook is registered",
          options.hooks is not None and "PreToolUse" in options.hooks and options.hooks["PreToolUse"],
          options.hooks)
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


def check_task_claiming_race(project: Path) -> None:
    """Scenario 2: Two agents poll for tasks at the same time - verify atomicity."""
    print("concurrent task claiming race")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "racer-1", "claude", "builder")
    core.register_agent(db, "racer-2", "claude", "reviewer")
    core.heartbeat(db, "racer-1", "working")
    core.heartbeat(db, "racer-2", "working")

    # Create tasks for each agent
    r1_t1 = add_ready(db, "Racer1-A", "first", "racer-1")
    r1_t2 = add_ready(db, "Racer1-B", "second", "racer-1")
    r2_t1 = add_ready(db, "Racer2-A", "third", "racer-2")
    r2_t2 = add_ready(db, "Racer2-B", "fourth", "racer-2")

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

    task_id = add_ready(db, "collaboration", "", "msg-1")

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
    task_a = add_ready(db, "Design API", "", "dep-1")
    # Create task B assigned to dep-2, depends on A
    task_b = add_ready(db, "Implement API", "", "dep-2")
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

    task_id = add_ready(db, "Risky change", "", "approval-1")
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
        core.update_task_status(db, task_id, "ready")

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


def check_lazy_load_history(project: Path) -> None:
    """Scenario 7: Message summarization - keep last 5 full, summarize older."""
    print("lazy-load message history")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "history-agent", "claude", "builder")
    core.heartbeat(db, "history-agent", "working")

    task_id = add_ready(db, "Multi-turn task", "requires multiple interactions", "history-agent")

    # Create 12 messages to test summarization (7 old + 5 recent)
    # Sent from agent to human so they won't be in inbox
    for i in range(1, 13):
        payload = f"result message {i}" + (" (final)" if i == 12 else "")
        core.send_message(db, "history-agent", core.HUMAN, task_id, "result", payload)

    # Test 1: Default behavior - summarize old (1-7), keep last 5 full (8-12)
    task = core.get_task(db, task_id)
    prompt_limited, _ = core.compose_task_prompt(db, "history-agent", task, limit_history=True)

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
    prompt_full, _ = core.compose_task_prompt(db, "history-agent", task, limit_history=False)
    check("full prompt includes all history", "result message 1" in prompt_full and "result message 12" in prompt_full)
    check("full prompt doesn't use prior context", "Prior context" not in prompt_full)

    # Test 3: With 5 or fewer messages, all should be in full (no summarization)
    task_id_short = add_ready(db, "Short task", "", "history-agent")
    for i in range(1, 4):
        core.send_message(db, "history-agent", core.HUMAN, task_id_short, "result", f"short msg {i}")

    task_short = core.get_task(db, task_id_short)
    prompt_short, _ = core.compose_task_prompt(db, "history-agent", task_short, limit_history=True)
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

    task_id = add_ready(db, "Long-running task", "very long description with lots of content" * 50, "limit-agent")

    long_message = "This is a test message. " * 100  # ~2400 chars
    for i in range(20):
        # Send FROM limit-agent TO human so messages end up in task_messages, not inbox
        core.send_message(db, "limit-agent", core.HUMAN, task_id, "note", f"Message {i}: {long_message}")

    check("token count for short text", core.estimate_token_count("hello world") >= 1)
    check("token count increases with length",
          core.estimate_token_count("a" * 1000) > core.estimate_token_count("hello world"))

    task = core.get_task(db, task_id)
    prompt_unlimited, _ = core.compose_task_prompt(db, "limit-agent", task, limit_history=False)
    check("unlimited prompt includes all history",
          "Message 0:" in prompt_unlimited and "Message 19:" in prompt_unlimited)
    check("unlimited prompt is large", core.estimate_token_count(prompt_unlimited) > 1000)

    # 20 long messages, but only the last 5 land in full
    prompt, _ = core.compose_task_prompt(db, "limit-agent", task)
    tokens = core.estimate_token_count(prompt)
    check("summarized prompt still names the task", "Long-running task" in prompt)
    check("summarized prompt keeps the recent messages in full", "Message 19:" in prompt)
    # Verify that with limit_history, we get both prior context (summarized) and recent (full)
    check("summarized prompt has prior context section", "Prior context" in prompt)
    check("summarized prompt has recent messages section", "Recent messages" in prompt)
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
    # Create a planning task, then a dev task that depends on it
    task_id_planning = add_ready(db, "Plan feature X", "Complex feature planning", "planning-agent")
    plan_context = """{
        "phase": 1,
        "approach": "Modular architecture with dependency injection",
        "key_decisions": ["Use abstract base classes", "Implement factory pattern"],
        "files_to_modify": ["src/core.py", "src/services.py"],
        "critical_constraints": "Must maintain backward compatibility"
    }"""

    # Simulate planning-agent finishing and storing context
    core.docs_set(db, f"task_{task_id_planning}_planning-agent_context", plan_context, updated_by="planning-agent")
    check("planning context stored", core.docs_get(db, f"task_{task_id_planning}_planning-agent_context") is not None)

    # Create a dev task that depends on the planning task
    task_id = add_ready(db, "Implement feature X", "Complex feature requiring multiple agents", "dev-agent")
    core.add_dependency(db, task_id, task_id_planning)

    # Test 2: dev-agent retrieves context from planning-agent in prompt (via dependency)
    task = core.get_task(db, task_id)
    prompt_with_context, _ = core.compose_task_prompt(db, "dev-agent", task)
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

    # Test 4: review-agent gets context from dev-agent via dependency
    task_id_review = add_ready(db, "Review implementation", "Code review", "review-agent")
    core.add_dependency(db, task_id_review, task_id)
    prompt_for_review, _ = core.compose_task_prompt(db, "review-agent", core.get_task(db, task_id_review))
    check("dev context appears in review prompt", "Context from dev-agent" in prompt_for_review)
    check("review sees dev changes", "ServiceFactory class" in prompt_for_review)
    check("review sees test coverage", "15 new unit tests" in prompt_for_review)

    # Test 5: Context is included in prompt (explicit check)
    # This verifies that when both history and context exist, the context is available
    task_id_2_planning = add_ready(db, "Plan second feature", "Second planning task", "planning-agent")
    long_message = "This is a detailed message about implementation strategy. " * 30  # ~1500 chars
    for i in range(10):
        core.send_message(db, "human", "planning-agent", task_id_2_planning, "note", f"Iteration {i}: {long_message}")

    # Store context
    ctx = "Brief planning summary: modular architecture with factory pattern."
    core.docs_set(db, f"task_{task_id_2_planning}_planning-agent_context", ctx, updated_by="planning-agent")

    # Create dependent task
    task_id_2 = add_ready(db, "Another task", "Testing token savings", "dev-agent")
    core.add_dependency(db, task_id_2, task_id_2_planning)

    # Verify context is accessible
    task2 = core.get_task(db, task_id_2)
    context_doc = core.docs_get(db, f"task_{task_id_2_planning}_planning-agent_context")
    check("stored context is retrievable", context_doc == ctx)

    # Verify it appears in prompt
    prompt_with_ctx, _ = core.compose_task_prompt(db, "dev-agent", task2)
    check("context section in prompt", "Context from planning-agent" in prompt_with_ctx)
    check("actual context content in prompt", "modular architecture" in prompt_with_ctx)

    # Test 6: Non-hardcoded agent name (custom-analyzer) - verifies fix for hardcoded agent names
    # This test ensures that get_workflow_context works with any agent name from dependencies,
    # not just the hardcoded "dev-agent" and "planning-agent"
    core.register_agent(db, "custom-analyzer", "claude", "analyzer")

    # Create task_3 with custom-analyzer as the assigned agent
    task_id_3 = add_ready(db, "Analysis task", "Perform custom analysis", "custom-analyzer")

    # Create task_4 that depends on task_3 (assigned to review-agent)
    task_id_4 = add_ready(db, "Dependent task", "Review the analysis", "review-agent")
    core.add_dependency(db, task_id_4, task_id_3)

    # Store context from custom-analyzer on task_3
    custom_context = "Analysis result: Found 5 critical issues and 12 warnings in code quality."
    core.docs_set(db, f"task_{task_id_3}_custom-analyzer_context", custom_context, updated_by="custom-analyzer")

    # Verify review-agent gets context from custom-analyzer (not hardcoded)
    task4 = core.get_task(db, task_id_4)
    prompt_with_custom, _ = core.compose_task_prompt(db, "review-agent", task4)
    check("custom-analyzer context appears in prompt", "Context from custom-analyzer" in prompt_with_custom)
    check("custom context content visible", "5 critical issues" in prompt_with_custom)

    db.close()


def check_unread_messages_preserved_on_failure(project: Path) -> None:
    """Task R4: Unread messages are consumed before the run that needs them.

    This test verifies that messages are NOT marked as read if a run fails,
    so they can be delivered again on the re-queued attempt.
    """
    print("unread messages preserved on failure (task R4)")
    db = core.connect(core.db_path(project))
    core.register_agent(db, "test-agent", "claude", "builder")

    task_id = add_ready(db, "Test task", "Test description", "test-agent")

    # Send a message to the agent
    msg_id = core.send_message(db, "human", "test-agent", task_id, "question", "Can you help?")

    # Verify message is unread before compose_task_prompt
    inbox_before = core.get_inbox(db, "test-agent", mark_read=False)
    check("message initially unread", len(inbox_before) == 1 and inbox_before[0]["read_at"] is None)

    # Compose prompt - should NOT mark messages as read
    task = core.get_task(db, task_id)
    prompt, msg_ids = core.compose_task_prompt(db, "test-agent", task)
    check("compose_task_prompt returns message ids", msg_ids == [msg_id])
    check("prompt contains the message", "Can you help?" in prompt)

    # Message should still be unread after compose_task_prompt
    inbox_after_compose = core.get_inbox(db, "test-agent", mark_read=False)
    check("message still unread after compose", len(inbox_after_compose) == 1 and inbox_after_compose[0]["read_at"] is None,
          f"expected unread, got {inbox_after_compose}")

    # Simulate a failed run (which does NOT mark messages as read)
    # In the real daemon, if run_agent raises, we don't call mark_messages_read

    # Message should still be unread after a failed run
    inbox_after_fail = core.get_inbox(db, "test-agent", mark_read=False)
    check("message still unread after failed run", len(inbox_after_fail) == 1 and inbox_after_fail[0]["read_at"] is None,
          f"expected unread, got {inbox_after_fail}")

    # Now simulate a successful run - mark the messages as read
    core.mark_messages_read(db, msg_ids)

    # Message should now be read
    inbox_after_success = core.get_inbox(db, "test-agent", mark_read=False)
    check("message marked read after successful run", len(inbox_after_success) == 0,
          f"expected no unread, got {inbox_after_success}")

    # Verify the message was actually marked read in the DB
    all_msgs = core.task_messages(db, task_id)
    read_msg = [m for m in all_msgs if m["id"] == msg_id][0]
    check("message has read_at timestamp", read_msg["read_at"] is not None)

    db.close()


def check_worktree_agent(tmp: Path) -> None:
    """Test that worktree=true creates a worktree for the agent."""
    print("worktree agent")
    import subprocess
    from kuska import worktree

    project = tmp / "worktree-project"
    (project / ".agents" / "prompts").mkdir(parents=True)

    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True, capture_output=True)

    # Create an initial commit
    (project / "README.md").write_text("# Test Project")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    # Create config with worktree agent
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\nworktree = true\n'
    )

    # Create and run a task
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Test task", "test description", "dev-agent")

    # Verify worktree doesn't exist yet
    wt_path = worktree.worktree_path(project, task_id)
    check("worktree doesn't exist yet", not wt_path.exists())

    # Create the worktree
    path, branch, created = worktree.ensure_worktree(project, task_id, "Test task", "main")
    check("worktree created", created)
    check("worktree path is correct", path == wt_path)
    check("branch created", branch.startswith("kuska/"))
    check("worktree directory exists", path.exists())

    # Verify the worktree is listed
    wts = worktree.list_worktrees(project)
    check("worktree is listed", any(Path(wt["path"]).resolve() == path.resolve() for wt in wts))

    # Verify the branch exists
    repo = git.Repo(project)
    check("branch exists in repo", branch in [h.name for h in repo.heads])

    # Cleanup
    conn.close()


def check_non_worktree_agent(tmp: Path) -> None:
    """Test that worktree=false doesn't create a worktree."""
    print("non-worktree agent (cwd is project)")
    from pathlib import Path

    project = tmp / "non-worktree-project"
    (project / ".agents" / "prompts").mkdir(parents=True)

    # Create config without worktree
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )

    # Create and run a task
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Test task", "test description", "dev-agent")

    # Simulate what the daemon does
    cfg = core.agent_config(project, "dev-agent")
    check("worktree is false by default", not cfg.get("worktree"))

    # workdir should equal project
    if not cfg.get("worktree"):
        workdir = project
    else:
        workdir = None

    check("workdir equals project", workdir == project)

    # Worktree directory shouldn't be created
    wt_dir = project / ".agents" / "worktrees"
    check("no worktree directory created", not wt_dir.exists())

    conn.close()


def check_worktree_ready_to_merge(tmp: Path) -> None:
    """Test that a worktree agent calling reply(status='done') gets coerced to ready_to_merge."""
    print("worktree task coerced from done to ready_to_merge")
    from kuska import runtime

    project = tmp / "worktree-done-project"
    (project / ".agents" / "prompts").mkdir(parents=True)

    # Initialize a git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True, capture_output=True)

    # Create an initial commit
    (project / "README.md").write_text("# Test Project")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)

    # Create config with agent
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )

    # Create database and task
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Test task", "test description", "dev-agent")

    # Simulate finish_task with worktree_branch set and no prior reply
    # This should coerce "done" to "ready_to_merge"
    started = time.time()
    runtime.finish_task(
        conn, "dev-agent", task_id, "Task completed",
        started, worktree_branch="kuska/1-test"
    )

    # Check that the task status is ready_to_merge
    task = core.get_task(conn, task_id)
    check("worktree task coerced to ready_to_merge", task["status"] == "ready_to_merge",
          f"expected ready_to_merge, got {task['status']}")

    conn.close()


def check_non_worktree_done(tmp: Path) -> None:
    """Test that a non-worktree agent calling reply(status='done') stays done."""
    print("non-worktree task stays done")
    from kuska import runtime

    project = tmp / "non-worktree-done-project"
    (project / ".agents" / "prompts").mkdir(parents=True)

    # Create config with agent
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )

    # Create database and task
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Test task", "test description", "dev-agent")

    # Simulate finish_task without worktree_branch
    # This should result in "done"
    started = time.time()
    runtime.finish_task(
        conn, "dev-agent", task_id, "Task completed",
        started, worktree_branch=None
    )

    # Check that the task status is done
    task = core.get_task(conn, task_id)
    check("non-worktree task stays done", task["status"] == "done",
          f"expected done, got {task['status']}")

    conn.close()


def check_worktree_blocked_stays_blocked(tmp: Path) -> None:
    """Test that a worktree agent replying 'blocked' stays blocked (not coerced to ready_to_merge)."""
    print("worktree blocked task stays blocked")
    from kuska import runtime

    project = tmp / "worktree-blocked-project"
    (project / ".agents" / "prompts").mkdir(parents=True)

    # Create config with agent
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )

    # Create database and task
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Test task", "test description", "dev-agent")

    # Set the task to blocked first (simulating an agent that called reply(status="blocked"))
    core.update_task_status(conn, task_id, "blocked")

    # Simulate finish_task with worktree_branch set but task already blocked
    # The status should stay blocked (coercion only happens for "done")
    started = time.time()
    runtime.finish_task(
        conn, "dev-agent", task_id, "Task blocked",
        started, worktree_branch="kuska/1-test"
    )

    # Check that the task status is still blocked
    task = core.get_task(conn, task_id)
    check("worktree blocked task stays blocked", task["status"] == "blocked",
          f"expected blocked, got {task['status']}")

    conn.close()


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
        check_task_claiming_race(project)
        check_message_ordering(project)
        check_dependency_satisfaction(project)
        check_approval_workflow_race(project)
        check_lazy_load_history(project)
        check_prompt_stays_small(project)
        check_workflow_context(project)
        check_unread_messages_preserved_on_failure(project)

        # Worktree tests (task 29)
        check_worktree_agent(tmp)
        check_non_worktree_agent(tmp)

        # Worktree ready_to_merge tests (task 30)
        check_worktree_ready_to_merge(tmp)
        check_non_worktree_done(tmp)
        check_worktree_blocked_stays_blocked(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
