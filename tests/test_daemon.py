"""Daemon-loop checks with the model call stubbed.

Proves the full loop - task queued, daemon picks it up, agent tools work
in-process, result and cost land in the DB, status page reflects it - without
spending a token.
"""

import asyncio
import json
import multiprocessing
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import git
import kuska as core
import openai
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from kuska import runtime, worktree
from kuska.daemons import BACKENDS, loop
from kuska.daemons import claude as daemon_claude
from kuska.daemons import codex as daemon_codex
from kuska.daemons import openai as daemon_openai
from kuska.daemons import run as run_backend
from openai_codex import Sandbox
from openai_codex.models import (
    ItemCompletedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)


def add_ready(conn, *args, **kw):
    """add_task, then move it to "ready" so an agent can claim it."""
    tid = core.add_task(conn, *args, **kw)
    core.update_task_status(conn, tid, "ready")
    return tid

# --------------------------------------------------------------------------
# Concurrency workers.
#
# These run in separate processes on purpose: `kuska daemon <name>` is its own
# process, and racing processes is what tests the database's guarantees (WAL,
# busy_timeout, atomic claims). Threads sharing one process - run-all - are
# covered by test_threads.py.
# --------------------------------------------------------------------------


def _claim_task_worker(db_path: str, agent_name: str, q) -> None:
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


@pytest.fixture
def project(tmp_path):
    project = tmp_path / "daemonproject"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
        '[agents.codex-1]\nbackend = "codex"\nmodel = "gpt-5-codex"\nrole = "reviewer"\n'
        "price_in_per_mtok = 1.25\nprice_out_per_mtok = 10.0\n"
        '[agents.openai-1]\nbackend = "openai"\nmodel = "gpt-4"\nrole = "openai agent"\n'
        "api_key = \"sk-test\"\n"
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    conn.close()
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


def test_tools_in_process(project):
    """in-process tools"""
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    tools = daemon_claude.build_tools(conn, "dev-agent")
    assert len(tools) == len(core.TOOL_SPECS), "one sdk tool per spec"
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {s["name"] for s in core.TOOL_SPECS}, "names match"

    out = asyncio.run(by_name["send_message"].handler({"recipient": "codex-1", "payload": "hi"}))
    assert out["content"][0]["type"] == "text", "tool returns mcp content"
    assert core.get_inbox(conn, "codex-1")[0]["payload"] == "hi", "tool wrote to db"
    err = asyncio.run(by_name["docs_get"].handler({}))
    assert err.get("isError") and "error:" in err["content"][0]["text"], "tool errors are returned, not raised"

    options = daemon_claude.build_options(project, project, "dev-agent", {"model": "claude-opus-5"}, tools)
    assert "kuska" in options.mcp_servers, "mcp server registered"
    assert "mcp__kuska__send_message" in options.allowed_tools, "tools allow-listed"
    assert "mcp__kuska__claim_task" not in options.allowed_tools, "claim_task tool removed"
    assert options.system_prompt["path"].endswith("prompts/dev-agent.md"), "prompt file wired"
    assert options.cwd == str(project), "runs in project dir"
    conn.close()


def test_loop(project):
    """daemon loop"""
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
        # through the tool, as a real agent does, so the reply is linked to its run
        core.call_tool(conn, "dev-agent", "reply",
                       {"task_id": t2, "payload": "Asked codex-1, waiting.", "status": "blocked"},
                       core.toolset({"flavor": "dev"}))
        return "Asked codex-1, waiting.", usage(400, 80, cache_read=3000, rounds=2, cost=0.01)

    run_loop(project, fake, max_tasks=2)

    assert core.get_task(conn, t1)["status"] == "done", "task 1 done"
    assert "handle quotes" in seen[0], "prompt carried the description"
    assert "start from the old branch" in seen[0], "prompt carried the human note"
    results = [m for m in core.task_messages(conn, t1) if m["msg_type"] == "result"]
    assert len(results) == 1, "one result logged"
    assert results[0]["payload"] == "Parser added.", "result text logged"
    assert results[0]["cost_usd"] == 0.03 and results[0]["input_tokens"] == 1000, "cost logged"
    assert results[0]["cache_read_tokens"] == 9000, "cache reads kept out of fresh input"
    assert results[0]["tool_rounds"] == 4, "tool rounds recorded"

    assert core.get_task(conn, t2)["status"] == "blocked", "task 2 blocked by the agent"
    r2 = [m for m in core.task_messages(conn, t2) if m["msg_type"] == "result"]
    assert len(r2) == 1, "agent's own reply not duplicated"
    assert r2[0]["cost_usd"] == 0.01, "usage attached to it"
    assert core.get_inbox(conn, "codex-1")[0]["payload"] == "which scope?", "question delivered"
    assert core.get_agent(conn, "dev-agent")["status"] == "offline", "agent left offline"
    spend = {u["agent"]: u["cost_usd"] for u in core.token_usage_by_agent(conn)}
    assert round(spend["dev-agent"], 4) == 0.04, "spend rolls up"

    # re-queue after a reply
    core.send_message(conn, "codex-1", "dev-agent", t2, "result", "scope is the CLI only")
    core.update_task_status(conn, t2, "ready")
    seen.clear()

    async def fake2(prompt, options, mono):
        seen.append(prompt)
        return "Scoped to the CLI, done.", usage(300, 60, cache_read=2000, rounds=1, cost=0.005)

    run_loop(project, fake2, max_tasks=1)
    assert core.get_task(conn, t2)["status"] == "done", "re-queued task ran again"
    assert "scope is the CLI only" in seen[0], "reply became context"
    assert "Asked codex-1, waiting." in seen[0], "earlier turn also in context"

    # failure path
    t3 = add_ready(conn, "Explodes", "", "dev-agent")

    async def boom(prompt, options, mono):
        raise RuntimeError("model unavailable")

    run_loop(project, boom, max_tasks=1)
    assert core.get_task(conn, t3)["status"] == "blocked", "failed task is blocked, not lost"
    blocker = [m for m in core.task_messages(conn, t3) if m["msg_type"] == "blocker"]
    assert blocker and "model unavailable" in blocker[0]["payload"], "failure explained in the thread"

    # monologue
    events = core.task_events(conn, t1)
    assert events[0]["kind"] == "prompt" and "handle quotes" in events[0]["body"], "prompt logged"
    assert any(e["label"] == "Read" for e in events), "tool call logged"
    assert events[-1]["kind"] == "result" and "$0.0300" in events[-1]["label"], "result logged with cost"
    assert len({e["run_id"] for e in events}) == 1, "one run id per invocation"
    failed = core.task_events(conn, t3)
    assert any(e["kind"] == "error" and "model unavailable" in e["body"] for e in failed), "failure narrated"

    # status page
    app = core.create_app(project)
    app.config.update(TESTING=True)
    html = app.test_client().get("/agents").get_data(as_text=True)
    assert "dev-agent" in html and "$0.0450" in html, "agents page shows the run"
    conn.close()


def test_tool_guard(project):
    db = core.connect(core.db_path(project))
    core.register_agent(db, "bench-agent", "codex", "benchmarks")
    for name in ("dev-agent", "bench-agent"):
        core.heartbeat(db, name, "working")

    current = {"mono": core.Monologue(db, "dev-agent", 1, quiet=True)}
    reads: dict = {}
    guard = daemon_claude.tool_guard(db, project, project, "dev-agent", current, reads)

    # redundant reads
    # test the redundant-read check
    fresh: dict = {}
    dedupe = daemon_claude.tool_guard(db, project, project, "dev-agent", current, fresh)
    assert (asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "allow"), "first read allowed"
    again = asyncio.run(dedupe("Read", {"file_path": "src/lexer.py"}, None))
    assert again.behavior == "deny", "the same file again is refused"
    assert "already read" in again.message, "told it already has the contents"
    assert "offset/limit" in again.message and "Grep" in again.message, "and pointed at offset/limit and Grep"
    assert (any(
        e["label"] == "redundant read" for e in core.task_events(db, 1))), "the refusal is in the monologue"
    assert (asyncio.run(
        dedupe("Read", {"file_path": "src/other.py"}, None)).behavior == "allow"), "a different file is fine"
    assert (asyncio.run(
        dedupe("Edit", {"file_path": "src/lexer.py"}, None)).behavior == "allow"), "editing it clears the record"
    assert (asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "allow"), "so re-reading a changed file is allowed"
    assert (asyncio.run(
        dedupe("Grep", {"pattern": "x"}, None)).behavior == "allow" and asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "deny"), "a Grep leaves the record alone"
    asyncio.run(dedupe("Bash", {"command": "ruff format src"}, None))
    assert (asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "allow"), "a shell command may have changed it, so it can be read again"
    asyncio.run(dedupe("Task", {"prompt": "refactor the lexer"}, None))
    assert (asyncio.run(
        dedupe("Read", {"file_path": "src/lexer.py"}, None)).behavior == "allow"), "so may a subagent"

    # a whole-file read subsumes every range; distinct ranges do not
    ranges: dict = {}
    ranged = daemon_claude.tool_guard(db, project, project, "dev-agent", current, ranges)
    assert (asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 1, "limit": 50}, None)).behavior == "allow"), "a ranged read is allowed"
    assert (asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 200, "limit": 50}, None)).behavior == "allow"), "a different range is allowed"
    assert (asyncio.run(
        ranged("Read", {"file_path": "src/big.py", "offset": 1, "limit": 50}, None)).behavior == "deny"), "the same range again is refused"
    assert (asyncio.run(
        ranged("Read", {"file_path": "src/big.py"}, None)).behavior == "deny"), "and the whole file is refused after ranges"

    # command guardrails (task 4's PreToolUse hook, exercised via tool_guard directly)
    # a fresh mono/reads pair so the redundant-read bookkeeping
    # above cannot interfere with what is being checked here
    guarded: dict = {}
    cmd_mono = core.Monologue(db, "dev-agent", 1, quiet=True)
    guarded["mono"] = cmd_mono
    cmd_guard = daemon_claude.tool_guard(db, project, project, "dev-agent", guarded, {})

    refused = asyncio.run(cmd_guard("Bash", {"command": "rm -rf /"}, None))
    assert refused.behavior == "deny", "a destructive Bash command is denied"
    assert "rm -rf" in refused.message and "no undo" in refused.message, "the message names the rule and why"
    assert "needs_approval" in refused.message, "the message points at needs_approval"
    assert (any(
        (e["label"] or "").startswith("refused:") for e in core.task_events(db, 1))), "the refusal lands in the monologue"

    ordinary = asyncio.run(cmd_guard("Bash", {"command": "uv run tests/run_all.py"}, None))
    assert ordinary.behavior == "allow", "an ordinary command is allowed"

    still_reads = asyncio.run(cmd_guard("Read", {"file_path": "src/after_refusal.py"}, None))
    assert still_reads.behavior == "allow", "a Read still works after a refusal"

    # regression: the bug task 4 fixed must not come back
    options = daemon_claude.build_options(
        project, project, "dev-agent", {"model": "claude-opus-5"}, [],
        pretooluse_hook=daemon_claude.as_pretooluse_hook(cmd_guard),
    )
    # A bare tool name in allowed_tools auto-approves that whole tool before
    # can_use_tool/the PreToolUse hook is ever consulted - the exact bug
    # task 4 fixed. If "Bash" (or Read/Write/Edit) ever creeps back in here,
    # every check above still passes (claim_guard is exercised directly),
    # while the live daemon would once again enforce nothing.
    assert ("Bash" not in options.allowed_tools and "Read" not in options.allowed_tools
          and "Write" not in options.allowed_tools and "Edit" not in options.allowed_tools), "bare tool names are not in allowed_tools"
    assert options.hooks is not None and "PreToolUse" in options.hooks and options.hooks["PreToolUse"], "the PreToolUse hook is registered"
    db.close()


def test_codex_wiring(project):
    """codex daemon wiring"""
    cfg = core.load_config(project)["agents"]["codex-1"]
    mcp = daemon_codex.mcp_config(project, "codex-1")["mcp_servers"]["kuska"]
    assert mcp["args"][-3:] == ["mcp", "--agent", "codex-1"], "points at kuska mcp"
    assert str(project) in mcp["args"], "scoped to this project"

    usage = SimpleNamespace(last=SimpleNamespace(input_tokens=2_000_000, output_tokens=100_000))
    tok_in, tok_out, cost = daemon_codex.usage_of(usage, cfg)
    assert (tok_in, tok_out) == (2_000_000, 100_000), "tokens read from turn"
    assert round(cost, 4) == round(2 * 1.25 + 0.1 * 10.0, 4), "cost priced from config"
    assert daemon_codex.usage_of(usage, {})[2] == 0.0, "no prices means no cost"
    assert daemon_codex.usage_of(None, cfg) == (0, 0, 0.0), "missing usage is harmless"

    cached = SimpleNamespace(last=SimpleNamespace(cached_input_tokens=900, cache_write_input_tokens=120))
    assert daemon_codex.cache_of(cached) == (900, 120), "cache counts read from turn"
    assert daemon_codex.cache_of(None) == (0, 0), "missing cache counts are zero"

    preset = daemon_codex.sandbox_preset
    assert preset("workspace-write") is Sandbox.workspace_write, "config string becomes a preset"
    assert preset("read_only") is Sandbox.read_only, "underscores accepted"
    assert preset("danger-full-access") is Sandbox.full_access, "wire spelling accepted"
    assert preset("") is None and preset(None) is None, "blank means the codex default"
    assert preset(Sandbox.full_access) is Sandbox.full_access, "a preset passes through"
    try:
        preset("wide-open")
    except ValueError as exc:
        assert "wide-open" in str(exc), "nonsense is rejected loudly"
    else:
        assert False, "nonsense is rejected loudly"


def test_openai_wiring(project):
    """openai daemon wiring"""
    cfg = core.load_config(project)["agents"]["openai-1"]
    assert cfg["backend"] == "openai", "backend registered"
    assert cfg.get("api_key") == "sk-test", "has api_key"

    # mcp_command should work for openai too
    cmd = daemon_openai.mcp_command()
    assert isinstance(cmd, list), "mcp_command returns a list"
    assert cmd[0] == sys.executable or "python" in cmd[0], "mcp_command includes python"

    # the real run_agent against the real `kuska mcp` subprocess; only the
    # OpenAI client is faked
    conn = core.connect(core.db_path(project))
    core.send_message(conn, core.HUMAN, "openai-1", None, "note", "hello over mcp")
    conn.close()
    replies = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[SimpleNamespace(id="c1", function=SimpleNamespace(name="get_inbox", arguments="{}"))],
        ))], usage=None),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="read it", tool_calls=None))], usage=None),
    ]
    sent = []

    async def create(**kwargs):
        sent.append(kwargs["messages"])
        return replies.pop(0)

    real_client = openai.AsyncOpenAI
    openai.AsyncOpenAI = lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    mono = core.Monologue(core.connect(core.db_path(project)), "openai-1", None, quiet=True)
    try:
        text, _ = asyncio.run(daemon_openai.run_agent(project, "openai-1", cfg, "check your inbox", mono))
    finally:
        openai.AsyncOpenAI = real_client
    assert text == "read it" and "hello over mcp" in sent[1][-1]["content"], "run_agent talks to kuska mcp"

    # codex item mapping
    item = SimpleNamespace(type="agent_message", text="all done", phase=SimpleNamespace(value="final_answer"))
    assert daemon_codex.describe_item(item) == ("text", "agent_message", "all done"), "agent message is text"
    shell = SimpleNamespace(type="command_execution", command="pytest -q")
    assert daemon_codex.describe_item(shell) == ("tool_use", "shell", "pytest -q"), "command is a tool call"
    mcp = SimpleNamespace(type="mcp_tool_call", server="kuska", tool="get_inbox", arguments={"a": 1})
    kind, label, body = daemon_codex.describe_item(mcp)
    assert (kind, label) == ("tool_use", "kuska.get_inbox") and "\"a\": 1" in body, "mcp call is named"
    unknown = SimpleNamespace(type="something_new", model_dump_json=lambda: '{"type": "something_new"}')
    assert daemon_codex.describe_item(unknown)[0] == "tool_use", "unknown item still logged"

    # backend dispatch
    assert set(BACKENDS) == {"claude", "codex", "openai"}, "three backends registered"
    try:
        run_backend("llama-cpp", project, "openai-1")
        assert False, "unknown backend refused"
    except SystemExit as exc:
        assert "no daemon for backend" in str(exc), "unknown backend refused"


def test_task_claiming_race(project):
    """Scenario 2: Two agents poll for tasks at the same time - verify atomicity."""
    # concurrent task claiming race
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

    assert results.get("racer-1") is not None and results.get("racer-2") is not None, "both agents claimed tasks"
    if results.get("racer-1") and results.get("racer-2"):
        assert results["racer-1"]["id"] != results["racer-2"]["id"], "they claimed different tasks"
        assert core.get_task(db, results["racer-1"]["id"])["status"] == "in_progress", "task atomicity: status moved to in_progress"

    db.close()


def test_message_ordering(project):
    """Scenario 3: Message ordering is preserved between concurrent agents."""
    # concurrent message ordering
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
    assert len(all_messages) == 2, "both messages logged"
    assert all_messages[0]["sender"] == "msg-1" and "What do you think?" in all_messages[0]["payload"], "msg-1's message first"
    assert all_messages[1]["sender"] == "msg-2" and "refactor" in all_messages[1]["payload"], "msg-2's message second"

    # Check inbox ordering
    inbox_2 = core.get_inbox(db, "msg-2", mark_read=False)
    inbox_1 = core.get_inbox(db, "msg-1", mark_read=False)
    assert len(inbox_2) > 0, "msg-2 received msg-1's message"
    assert len(inbox_1) > 0, "msg-1 received msg-2's message"

    db.close()


def test_dependency_satisfaction(project):
    """Scenario 4: Task A done by agent-1, then task B (depends on A) released to agent-2."""
    # concurrent dependency satisfaction
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

    assert results.get("dep-1-claimed") is not None, "dep-1 claimed task A"
    assert results.get("dep-1-done") is True, "dep-1 completed task A"
    assert core.get_task(db, task_a)["status"] == "done", "task A is done"

    assert results.get("dep-2-first-claim") is None, "dep-2 initially blocked on first claim"
    assert results.get("dep-2-blocked") is True, "dep-2 detected blocking"
    assert results.get("dep-2-second-claim") is not None, "dep-2 later claims task B"

    if results.get("dep-2-second-claim"):
        assert results["dep-2-second-claim"]["id"] == task_b, "task B is now runnable"

    db.close()


def test_approval_workflow_race(project):
    """Scenario 5: Task needs approval, human approves, agent immediately polls."""
    # concurrent approval workflow race
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

    assert results.get("poll-1") is None, "agent's first poll gets nothing (blocked by approval)"
    assert results.get("poll-2") is not None, "after approval, agent gets task"
    if results.get("poll-2"):
        assert results["poll-2"]["id"] == task_id, "claimed task is the right one"

    db.close()


def test_lazy_load_history(project):
    """Scenario 7: Message summarization - keep last 5 full, summarize older."""
    # lazy-load message history
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

    assert "Multi-turn task" in prompt_limited, "limited prompt includes task title"
    assert "Earlier on this task" in prompt_limited, "limited prompt has history section"
    assert "Prior context" in prompt_limited, "limited prompt has prior context"
    assert "Recent messages" in prompt_limited, "limited prompt has recent section"
    assert "last 5" in prompt_limited, "limited prompt indicates last 5"

    # Verify last 5 messages (8-12) are in full format
    assert "result message 12" in prompt_limited, "limited prompt has last message"
    assert "result message 11" in prompt_limited, "limited prompt has msg 11"
    assert "result message 8" in prompt_limited, "limited prompt has msg 8"

    # First message should be in summary (prior context) but not in full format
    # Full format would be "**history-agent -> human**" (with arrow)
    lines = prompt_limited.split('\n')
    full_msg1_count = sum(1 for line in lines if "result message 1" in line and "->" in line)
    assert full_msg1_count == 0, "msg 1 not in full format"
    # But msg 1 should still be somewhere in the prompt (in summary)
    assert "result message 1" in prompt_limited, "msg 1 in summary"

    # Test 2: Full history via limit_history=False
    prompt_full, _ = core.compose_task_prompt(db, "history-agent", task, limit_history=False)
    assert "result message 1" in prompt_full and "result message 12" in prompt_full, "full prompt includes all history"
    assert "Prior context" not in prompt_full, "full prompt doesn't use prior context"

    # Test 3: With 5 or fewer messages, all should be in full (no summarization)
    task_id_short = add_ready(db, "Short task", "", "history-agent")
    for i in range(1, 4):
        core.send_message(db, "history-agent", core.HUMAN, task_id_short, "result", f"short msg {i}")

    task_short = core.get_task(db, task_id_short)
    prompt_short, _ = core.compose_task_prompt(db, "history-agent", task_short, limit_history=True)
    assert "Prior context" not in prompt_short, "short prompt no summarization"
    assert "short msg 1" in prompt_short and "short msg 3" in prompt_short, "short prompt has all messages"

    db.close()


def test_prompt_stays_small(project):
    """Scenario 8: The composed prompt stays small however long the thread gets.

    There used to be a max_prompt_tokens setting here that progressively
    truncated history to fit a budget. It was never reachable: summarization
    already caps the prompt at a few hundred tokens, and an invocation's cost
    lives in the agentic loop that follows, not in the prompt that starts it.
    What this checks now is that the summarization actually holds.
    """
    # composed prompt stays small
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

    assert core.estimate_token_count("hello world") >= 1, "token count for short text"
    assert core.estimate_token_count("a" * 1000) > core.estimate_token_count("hello world"), "token count increases with length"

    task = core.get_task(db, task_id)
    prompt_unlimited, _ = core.compose_task_prompt(db, "limit-agent", task, limit_history=False)
    assert "Message 0:" in prompt_unlimited and "Message 19:" in prompt_unlimited, "unlimited prompt includes all history"
    assert core.estimate_token_count(prompt_unlimited) > 1000, "unlimited prompt is large"

    # 20 long messages, but only the last 5 land in full
    prompt, _ = core.compose_task_prompt(db, "limit-agent", task)
    tokens = core.estimate_token_count(prompt)
    assert "Long-running task" in prompt, "summarized prompt still names the task"
    assert "Message 19:" in prompt, "summarized prompt keeps the recent messages in full"
    # Verify that with limit_history, we get both prior context (summarized) and recent (full)
    assert "Prior context" in prompt, "summarized prompt has prior context section"
    assert "Recent messages" in prompt, "summarized prompt has recent messages section"
    assert sum(1 for line in prompt.split("\n") if "Message 0:" in line and "->" in line) == 0, "older messages survive only as one-line previews"

    db.close()


def test_workflow_context(project):
    """Scenario 9: Workflow context passing for multi-agent workflows (Phase 4.1)."""
    # workflow context passing (multi-agent)
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
    assert core.docs_get(db, f"task_{task_id_planning}_planning-agent_context") is not None, "planning context stored"

    # Create a dev task that depends on the planning task
    task_id = add_ready(db, "Implement feature X", "Complex feature requiring multiple agents", "dev-agent")
    core.add_dependency(db, task_id, task_id_planning)

    # Test 2: dev-agent retrieves context from planning-agent in prompt (via dependency)
    task = core.get_task(db, task_id)
    prompt_with_context, _ = core.compose_task_prompt(db, "dev-agent", task)
    assert "Context from planning-agent" in prompt_with_context, "workflow context appears in prompt"
    assert "Modular architecture" in prompt_with_context, "context content is included"
    assert "factory pattern" in prompt_with_context, "key decisions visible"

    # Test 3: dev-agent can store its own context for review-agent
    dev_context = """{
        "implementation_summary": "Implemented factory pattern for services",
        "files_modified": ["src/core.py", "src/services.py", "tests/test_services.py"],
        "key_changes": ["Added ServiceFactory class", "Migrated service instantiation"],
        "test_coverage": "Added 15 new unit tests for factory pattern"
    }"""
    core.docs_set(db, f"task_{task_id}_dev-agent_context", dev_context, updated_by="dev-agent")
    assert core.docs_get(db, f"task_{task_id}_dev-agent_context") is not None, "dev context stored"

    # Test 4: review-agent gets context from dev-agent via dependency
    task_id_review = add_ready(db, "Review implementation", "Code review", "review-agent")
    core.add_dependency(db, task_id_review, task_id)
    prompt_for_review, _ = core.compose_task_prompt(db, "review-agent", core.get_task(db, task_id_review))
    assert "Context from dev-agent" in prompt_for_review, "dev context appears in review prompt"
    assert "ServiceFactory class" in prompt_for_review, "review sees dev changes"
    assert "15 new unit tests" in prompt_for_review, "review sees test coverage"

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
    assert context_doc == ctx, "stored context is retrievable"

    # Verify it appears in prompt
    prompt_with_ctx, _ = core.compose_task_prompt(db, "dev-agent", task2)
    assert "Context from planning-agent" in prompt_with_ctx, "context section in prompt"
    assert "modular architecture" in prompt_with_ctx, "actual context content in prompt"

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
    assert "Context from custom-analyzer" in prompt_with_custom, "custom-analyzer context appears in prompt"
    assert "5 critical issues" in prompt_with_custom, "custom context content visible"

    # handover: every dependency, with the result as fallback
    api = add_ready(db, "Build the API", "", "dev-agent")
    ui = add_ready(db, "Build the UI", "", "custom-analyzer")
    ship = add_ready(db, "Ship it", "", "review-agent")
    core.add_dependency(db, ship, api)
    core.add_dependency(db, ship, ui)
    core.update_task_status(db, api, "in_progress")
    core.call_tool(db, "dev-agent", "reply", {
        "task_id": api, "payload": "API done.", "handover": "## API\n\nRoutes live under /v2.",
    })
    core.update_task_status(db, ui, "in_progress")
    core.reply(db, "custom-analyzer", ui, "UI done; the settings page still needs copy.")
    prompt, _ = core.compose_task_prompt(db, "review-agent", core.get_task(db, ship))
    assert "Routes live under /v2." in prompt, "reply's handover reaches the dependent"
    assert "settings page still needs copy" in prompt, "a dependency without a handover passes its result"
    assert f"(task {api}: Build the API)" in prompt and f"(task {ui}: Build the UI)" in prompt, "each section names its task"

    db.close()


def test_unread_messages_preserved_on_failure(project):
    """Task R4: Unread messages are consumed before the run that needs them.

    This test verifies that messages are NOT marked as read if a run fails,
    so they can be delivered again on the re-queued attempt.
    """
    # unread messages preserved on failure (task R4)
    db = core.connect(core.db_path(project))
    core.register_agent(db, "test-agent", "claude", "builder")

    task_id = add_ready(db, "Test task", "Test description", "test-agent")

    # Send a message to the agent
    msg_id = core.send_message(db, "human", "test-agent", task_id, "question", "Can you help?")

    # Verify message is unread before compose_task_prompt
    inbox_before = core.get_inbox(db, "test-agent", mark_read=False)
    assert len(inbox_before) == 1 and inbox_before[0]["read_at"] is None, "message initially unread"

    # Compose prompt - should NOT mark messages as read
    task = core.get_task(db, task_id)
    prompt, msg_ids = core.compose_task_prompt(db, "test-agent", task)
    assert msg_ids == [msg_id], "compose_task_prompt returns message ids"
    assert "Can you help?" in prompt, "prompt contains the message"

    # Message should still be unread after compose_task_prompt
    inbox_after_compose = core.get_inbox(db, "test-agent", mark_read=False)
    assert len(inbox_after_compose) == 1 and inbox_after_compose[0]["read_at"] is None, "message still unread after compose"

    # Simulate a failed run (which does NOT mark messages as read)
    # In the real daemon, if run_agent raises, we don't call mark_messages_read

    # Message should still be unread after a failed run
    inbox_after_fail = core.get_inbox(db, "test-agent", mark_read=False)
    assert len(inbox_after_fail) == 1 and inbox_after_fail[0]["read_at"] is None, "message still unread after failed run"

    # Now simulate a successful run - mark the messages as read
    core.mark_messages_read(db, msg_ids)

    # Message should now be read
    inbox_after_success = core.get_inbox(db, "test-agent", mark_read=False)
    assert len(inbox_after_success) == 0, "message marked read after successful run"

    # Verify the message was actually marked read in the DB
    all_msgs = core.task_messages(db, task_id)
    read_msg = [m for m in all_msgs if m["id"] == msg_id][0]
    assert read_msg["read_at"] is not None, "message has read_at timestamp"

    db.close()


def test_worktree_agent(tmp_path):
    """Test that worktree=true creates a worktree for the agent."""
    # worktree agent
    project = tmp_path / "worktree-project"
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
    assert not wt_path.exists(), "worktree doesn't exist yet"

    # Create the worktree
    path, branch, created = worktree.ensure_worktree(project, task_id, "Test task", "main")
    assert created, "worktree created"
    assert path == wt_path, "worktree path is correct"
    assert branch.startswith("kuska/"), "branch created"
    assert path.exists(), "worktree directory exists"

    # Verify the worktree is listed
    wts = worktree.list_worktrees(project)
    assert any(Path(wt["path"]).resolve() == path.resolve() for wt in wts), "worktree is listed"

    # Verify the branch exists
    repo = git.Repo(project)
    assert branch in [h.name for h in repo.heads], "branch exists in repo"

    # prepare_workdir records the branch's base commit
    task_id2 = add_ready(conn, "Base sha task", "d", "dev-agent")
    task2 = core.get_task(conn, task_id2)
    mono = core.Monologue(conn, "dev-agent", task_id2, quiet=True)
    loop.prepare_workdir(conn, project, "dev-agent", task2, mono)
    head = git.Repo(project).head.commit.hexsha
    assert core.get_task(conn, task_id2)["worktree_base_sha"] == head, "prepare_workdir records worktree_base_sha"

    # Cleanup
    conn.close()


def test_non_worktree_agent(tmp_path):
    """Test that worktree=false doesn't create a worktree."""
    # non-worktree agent (cwd is project)
    project = tmp_path / "non-worktree-project"
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
    assert not cfg.get("worktree"), "worktree is false by default"

    # workdir should equal project
    if not cfg.get("worktree"):
        workdir = project
    else:
        workdir = None

    assert workdir == project, "workdir equals project"

    # Worktree directory shouldn't be created
    wt_dir = project / ".agents" / "worktrees"
    assert not wt_dir.exists(), "no worktree directory created"

    conn.close()


def test_worktree_ready_to_merge(tmp_path):
    """Test that a worktree agent calling reply(status='done') gets coerced to ready_to_merge."""
    # worktree task coerced from done to ready_to_merge
    project = tmp_path / "worktree-done-project"
    (project / ".agents" / "prompts").mkdir(parents=True)

    # Initialize a git repo
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

    # a worktree task (the daemon records its path before the run) finishing
    # with no prior reply: "done" is held as "ready_to_merge"
    core.update_task_status(conn, task_id, "in_progress")
    core.update_task(conn, task_id, worktree_path=str(project / ".agents" / "worktrees" / f"task-{task_id}"))
    started = time.time()
    runtime.finish_task(conn, "dev-agent", task_id, "Task completed", started)

    # Check that the task status is ready_to_merge
    task = core.get_task(conn, task_id)
    assert task["status"] == "ready_to_merge", "worktree task coerced to ready_to_merge"

    conn.close()


def test_worktree_reply_tool_done(tmp_path):
    """An agent calling the reply tool with status='done' itself must not skip review."""
    # worktree task: agent's own reply(status='done') lands in ready_to_merge
    project = tmp_path / "worktree-reply-tool-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Test task", "test description", "dev-agent")
    dependent = add_ready(conn, "Depends on it", "", "dev-agent")
    core.add_dependency(conn, dependent, task_id)

    started = time.time()
    claimed = core.claim_task(conn, "dev-agent")
    core.update_task(conn, task_id, worktree_path=str(project / ".agents" / "worktrees" / f"task-{task_id}"))

    # mid-run: the agent closes the task through the tool
    core.call_tool(conn, "dev-agent", "reply", {"task_id": task_id, "payload": "done", "status": "done"})
    task = core.get_task(conn, task_id)
    assert claimed["id"] == task_id and task["status"] == "ready_to_merge", "reply tool coerces to ready_to_merge"
    assert core.claim_task(conn, "dev-agent") is None, "dependent not claimable mid-run"

    # end of run: finish_task finds the agent's reply and must not undo the hold
    runtime.finish_task(conn, "dev-agent", task_id, "done", started)
    task = core.get_task(conn, task_id)
    assert task["status"] == "ready_to_merge", "still ready_to_merge after finish_task"
    assert core.claim_task(conn, "dev-agent") is None, "dependent still not claimable"

    conn.close()


def test_run_limits(tmp_path):
    """run limits"""
    limits = core.run_limits({})
    assert limits == {"max_turns": None, "max_budget_usd": None, "timeout_s": core.DEFAULT_TIMEOUT_MINUTES * 60}, "timeout defaults on, turns and budget off"
    limits = core.run_limits({"max_turns": 40.0, "max_budget_usd": "2.5", "timeout_minutes": 0})
    assert limits == {"max_turns": 40, "max_budget_usd": 2.5, "timeout_s": core.DEFAULT_TIMEOUT_MINUTES * 60}, "config values parsed, zero timeout falls back"

    project = tmp_path / "limits-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n'
        "max_turns = 30\nmax_budget_usd = 1.5\ntimeout_minutes = 0.002\n"
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    cfg = core.agent_config(project, "dev-agent")
    options = daemon_claude.build_options(project, project, "dev-agent", cfg, [])
    assert (options.max_turns, options.max_budget_usd) == (30, 1.5), "limits handed to the sdk"
    assert options.sandbox["enabled"] and options.sandbox["allowUnsandboxedCommands"] is False, "bash sandboxed by default, with no way out"
    assert options.sandbox["excludedCommands"] == ["git"], "git runs outside it, for worktree commits"
    assert not daemon_claude.sandbox_settings({"sandbox": "full-access"})["enabled"], "full-access turns it off"

    def run(fake) -> int:
        task_id = add_ready(conn, "Big task", "", "dev-agent")
        real = daemon_claude.run_agent
        daemon_claude.run_agent = fake
        try:
            daemon_claude.run_daemon(project, "dev-agent", poll_interval=0.02, max_tasks=1, quiet=True)
        finally:
            daemon_claude.run_agent = real
        return task_id

    # the sdk stopping a run at its limit: the real run_agent, fed a stubbed stream
    async def stopped_stream(prompt, options):
        yield ResultMessage(
            subtype="error_max_budget_usd", duration_ms=10, duration_api_ms=10, is_error=True,
            num_turns=12, session_id="s", total_cost_usd=1.62,
            usage={"input_tokens": 900, "output_tokens": 300},
        )

    real_query = daemon_claude.query
    daemon_claude.query = stopped_stream
    try:
        t1 = run(daemon_claude.run_agent)
    finally:
        daemon_claude.query = real_query
    assert core.get_task(conn, t1)["status"] == "blocked", "budget stop blocks the task, not done"
    msgs = core.task_messages(conn, t1)
    blocker = [m for m in msgs if m["msg_type"] == "blocker"]
    assert blocker and "max_budget_usd" in blocker[0]["payload"], "blocker says which limit"
    assert blocker and blocker[0]["cost_usd"] == 1.62 and blocker[0]["input_tokens"] == 900, "its spend is on the ledger"
    assert not [m for m in msgs if m["msg_type"] == "result"], "no result logged for a stopped run"

    async def hangs(prompt, options, mono):
        await asyncio.sleep(5)
        return "never", usage(1, 1)

    started = time.time()
    t2 = run(hangs)
    assert time.time() - started < 3, "hung run cut off at its timeout"
    assert core.get_task(conn, t2)["status"] == "blocked", "timed-out task blocked"
    blocker = [m for m in core.task_messages(conn, t2) if m["msg_type"] == "blocker"]
    assert blocker and "timed out" in blocker[0]["payload"], "blocker says it timed out"

    async def works_then_hangs(prompt, options):
        for i, (tok_in, tok_out) in enumerate([(1200, 80), (300, 40)]):
            yield AssistantMessage(
                content=[ToolUseBlock(id=f"t{i}", name="Grep", input={"pattern": "x"})], model="m",
                message_id=f"msg{i}", usage={"input_tokens": tok_in, "output_tokens": tok_out,
                                             "cache_read_input_tokens": 5000},
            )
        await asyncio.sleep(5)

    daemon_claude.query = works_then_hangs
    try:
        t3 = run(daemon_claude.run_agent)
    finally:
        daemon_claude.query = real_query
    blocker = [m for m in core.task_messages(conn, t3) if m["msg_type"] == "blocker"]
    assert (blocker and blocker[0]["input_tokens"] == 1500 and blocker[0]["output_tokens"] == 120
          and blocker[0]["cache_read_tokens"] == 10000 and blocker[0]["tool_rounds"] == 2), "a timed-out run keeps the tokens it had used"
    conn.close()


def test_shared_loop_worktree(tmp_path):
    """The loop every backend shares, driven with a fake backend, in worktree mode."""
    # shared loop: worktree task, any backend
    project = tmp_path / "loop-worktree-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("# Test")
    (project / ".gitignore").write_text(".agents/\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "openai"\nrole = "builder"\nworktree = true\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Write notes", "", "dev-agent")
    seen = {}

    def make_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            seen["workdir"] = workdir
            (workdir / "notes.md").write_text("left uncommitted\n")
            return "  Wrote notes.  ", {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02}
        return run

    loop.run_daemon(project, "dev-agent", "fake", make_runner, poll_interval=0.02, max_tasks=1)

    task = core.get_task(conn, task_id)
    assert seen["workdir"] == Path(task["worktree_path"]) and seen["workdir"] != project, "ran in the task's worktree"
    assert task["status"] == "ready_to_merge", "held for review"
    result = [m for m in core.task_messages(conn, task_id) if m["msg_type"] == "result"]
    assert result and result[0]["payload"] == "Wrote notes." and result[0]["cost_usd"] == 0.02, "result trimmed and costed"
    log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=seen["workdir"], capture_output=True, text=True,
                         check=True).stdout
    assert log.startswith(f"wip: task {task_id}"), "leftover changes committed on the branch"
    assert not (project / "notes.md").exists(), "main checkout untouched"

    # shared loop: failed run, failed worktree setup

    def make_failing_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            seen["workdir"] = workdir
            (workdir / "half.md").write_text("half done\n")
            raise RuntimeError("model unavailable")
        return run

    failing = add_ready(conn, "Half a job", "", "dev-agent")
    loop.run_daemon(project, "dev-agent", "fake", make_failing_runner, poll_interval=0.02, max_tasks=1)
    log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=seen["workdir"], capture_output=True, text=True,
                         check=True).stdout
    assert "partial work from a failed run" in log, "failed run's leftovers labelled as partial"
    assert core.get_task(conn, failing)["status"] == "blocked", "failed run blocks its task"

    def no_worktree(*args, **kwargs):
        raise RuntimeError("disk full")

    real_ensure = loop.worktree.ensure_worktree
    loop.worktree.ensure_worktree = no_worktree
    seen.clear()
    try:
        stuck = add_ready(conn, "Cannot isolate", "", "dev-agent")
        loop.run_daemon(project, "dev-agent", "fake", make_runner, poll_interval=0.02, max_tasks=1)
    finally:
        loop.worktree.ensure_worktree = real_ensure
    assert "workdir" not in seen, "no fallback to the main checkout"
    assert core.get_task(conn, stuck)["status"] == "blocked", "task blocked instead"
    blocker = [m for m in core.task_messages(conn, stuck) if m["msg_type"] == "blocker"]
    assert blocker and "disk full" in blocker[0]["payload"], "blocker explains it"
    stuck_runs = core.task_runs(conn, stuck)
    assert len(stuck_runs) == 1 and stuck_runs[0]["status"] == "failed" and "disk full" in stuck_runs[0]["exit_reason"], "failed worktree setup leaves a failed run"
    conn.close()


def test_review_runs_in_author_worktree(tmp_path):
    """A review task runs in the source task's worktree and never commits there."""
    project = tmp_path / "review-worktree-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("# Test")
    (project / ".gitignore").write_text(".agents/\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "openai"\nrole = "builder"\nworktree = true\n'
        '[agents.review-agent]\nbackend = "openai"\nrole = "reviewer"\nworktree = true\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    dev_id = add_ready(conn, "Write notes", "", "dev-agent")

    def dev_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            (workdir / "notes.md").write_text("done\n")
            subprocess.run(["git", "add", "."], cwd=workdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "notes"], cwd=workdir, check=True, capture_output=True)
            return "Wrote notes.", {"input_tokens": 1, "output_tokens": 1}
        return run

    loop.run_daemon(project, "dev-agent", "fake", dev_runner, poll_interval=0.02, max_tasks=1)
    dev = core.get_task(conn, dev_id)
    assert dev["status"] == "ready_to_merge", "dev task held for review"
    wt = Path(dev["worktree_path"])

    def commits() -> str:
        return subprocess.run(["git", "rev-list", "--count", "--all"], cwd=project, check=True,
                              capture_output=True, text=True).stdout.strip()

    before = commits()
    review_id = core.request_review(conn, dev_id, "review-agent", "main")
    assert review_id, "review task created"
    seen = {}

    def review_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            seen["workdir"] = workdir
            (workdir / "stray.txt").write_text("untracked\n")
            return "Looks fine.", {"input_tokens": 1, "output_tokens": 1}
        return run

    try:
        loop.run_daemon(project, "review-agent", "fake", review_runner, poll_interval=0.02, max_tasks=1)
    finally:
        (wt / "stray.txt").unlink(missing_ok=True)
    assert seen["workdir"] == wt, "review ran in the author's worktree"
    assert commits() == before, "review made no commit"
    assert not core.get_task(conn, review_id).get("worktree_path"), "review has no worktree of its own"

    # source worktree gone: the review is blocked, the reviewer never runs
    shutil.rmtree(wt)
    gone = core.request_review(conn, dev_id, "review-agent", "main", 5)
    assert gone, "second review task created"
    seen.clear()
    loop.run_daemon(project, "review-agent", "fake", review_runner, poll_interval=0.02, max_tasks=1)
    assert "workdir" not in seen, "reviewer not run"
    assert core.get_task(conn, gone)["status"] == "blocked", "review blocked"
    blocker = [m for m in core.task_messages(conn, gone) if m["msg_type"] == "blocker"]
    assert blocker and "cannot review" in blocker[0]["payload"], "blocker explains it"
    conn.close()


def test_run_ledger(tmp_path):
    """Every run leaves a runs row, and its heartbeat moves on its own thread."""
    # run ledger: finished, failed, heartbeat
    project = tmp_path / "run-ledger-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text('[agents.dev-agent]\nbackend = "openai"\nrole = "builder"\n')
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)

    def serve_one(run, **kw):
        loop.run_daemon(project, "dev-agent", "fake", lambda db, project_, agent_name, cfg: run,
                        poll_interval=0.02, max_tasks=1, quiet=True, **kw)

    async def succeeds(prompt, workdir, mono):
        return "Done.", {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25, "tool_rounds": 3}

    ok = add_ready(conn, "Succeeds", "", "dev-agent")
    serve_one(succeeds)
    runs = core.task_runs(conn, ok)
    result = [m for m in core.task_messages(conn, ok) if m["msg_type"] == "result"]
    assert len(runs) == 1, "one run for a finished task"
    assert runs[0]["status"] == "finished" and runs[0]["ended_at"] is not None, "it finished"
    assert runs[0]["cost_usd"] == 0.25 and runs[0]["input_tokens"] == 10 and runs[0]["tool_rounds"] == 3, "it kept the reported usage"
    assert result and runs[0]["result_message_id"] == result[0]["id"], "it points at the result message"

    box = {}

    async def replies(prompt, workdir, mono):
        core.call_tool(conn, "dev-agent", "reply", {"task_id": box["task"], "payload": "Replied itself."},
                       core.toolset({"flavor": "dev"}))
        return "Wrap-up text.", {"cost_usd": 0.75, "tool_rounds": 2}

    box["task"] = add_ready(conn, "Replies itself", "", "dev-agent")
    serve_one(replies)
    results = [m for m in core.task_messages(conn, box["task"]) if m["msg_type"] == "result"]
    runs = core.task_runs(conn, box["task"])
    assert len(results) == 1, "a run that replied itself leaves exactly one result message"
    assert results and results[0]["cost_usd"] == 0.75, "that message carries the run's cost"
    assert results and runs[0]["status"] == "finished" and runs[0]["result_message_id"] == results[0]["id"], "the run points at that message"

    async def aborts(prompt, workdir, mono):
        raise core.RunAborted("x", {"cost_usd": 0.5})

    bad = add_ready(conn, "Aborts", "", "dev-agent")
    serve_one(aborts)
    runs = core.task_runs(conn, bad)
    assert (len(runs) == 1 and runs[0]["status"] == "failed" and "x" in runs[0]["exit_reason"]
          and runs[0]["cost_usd"] == 0.5 and runs[0]["result_message_id"] is None), "an aborted run is failed with its reason and spend"

    seen = {}

    async def sleeps(prompt, workdir, mono):
        seen["run_id"] = mono.run_id
        seen["before"] = core.get_run(conn, mono.run_id)
        await asyncio.sleep(0.3)
        seen["during"] = core.get_run(conn, mono.run_id)
        seen["threads"] = [t.name for t in threading.enumerate()]
        return "Slept.", {}

    add_ready(conn, "Sleeps", "", "dev-agent")
    serve_one(sleeps, heartbeat_interval=0.05)
    assert seen["before"]["status"] == "running" and seen["before"]["heartbeat_at"] == seen["before"]["started_at"], "running run starts with heartbeat at its start"
    assert seen["during"]["heartbeat_at"] > seen["during"]["started_at"], "heartbeat advances while the run is busy"
    assert f"heartbeat-{seen['run_id']}" in seen["threads"], "heartbeat thread is named for its run"
    assert not [t for t in threading.enumerate() if t.name.startswith("heartbeat-")], "heartbeat thread stopped after the run"
    assert core.get_run(conn, seen["run_id"])["status"] == "finished", "slow run still finished"
    conn.close()


def test_question_round_trip(tmp_path):
    """An agent's question to an idle agent gets answered without a human."""
    # question to another agent: answered and resumed, no human
    project = tmp_path / "question-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n'
        '[agents.planning-agent]\nbackend = "claude"\nrole = "planner"\nflavor = "planner"\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    task_id = add_ready(conn, "Add storage", "", "dev-agent")
    prompts: dict = {}
    tools = core.toolset({"flavor": "dev"})

    def runner(agent, act):
        def make_runner(db, project_, agent_name, cfg):
            async def run(prompt, workdir, mono):
                prompts.setdefault(agent, []).append(prompt)
                return act(db, prompt)
            return run
        loop.run_daemon(project, agent, "fake", make_runner, poll_interval=0.02, max_tasks=1)

    def ask(db, prompt):
        out = core.call_tool(db, "dev-agent", "send_message", {
            "recipient": "planning-agent", "payload": "Which database should storage use?",
            "msg_type": "question", "task_id": task_id,
        }, tools)
        prompts["answer_task"] = out.get("answer_task")
        core.call_tool(db, "dev-agent", "reply", {"task_id": task_id, "payload": "Asked the planner.",
                                                  "status": "blocked"}, tools)
        return "Asked the planner.", {}

    runner("dev-agent", ask)
    answer_id = prompts["answer_task"]
    assert (answer_id is not None
          and core.get_task(conn, answer_id)["assigned_to"] == "planning-agent"
          and core.get_task(conn, answer_id)["status"] == "ready"), "the question became a task for the planner"
    assert (core.get_task(conn, task_id)["status"] == "ready"
          and core.claim_task(conn, "dev-agent") is None), "the asking task waits on it instead of on a human"

    def answer(db, prompt):
        # the answering task cannot spawn questions of its own
        out = core.call_tool(db, "planning-agent", "send_message", {
            "recipient": "dev-agent", "payload": "Postgres or SQLite?", "msg_type": "question",
            "task_id": answer_id,
        }, core.toolset({"flavor": "planner"}))
        prompts["counter_question"] = out
        return "Use SQLite.", {}

    runner("planning-agent", answer)
    assert "Which database should storage use?" in prompts["planning-agent"][0], "the planner saw the question"
    assert "answer_task" not in prompts["counter_question"], "an answer task cannot spawn another"

    runner("dev-agent", lambda db, prompt: ("Stored in SQLite.", {}))
    assert "Use SQLite." in prompts["dev-agent"][1], "the asking task resumed with the answer"
    assert core.get_task(conn, task_id)["status"] == "done", "and finished"
    conn.close()


def test_codex_run():
    """codex turn: narration, limits, watchdog"""
    def item(**kw):
        return SimpleNamespace(payload=ItemCompletedNotification.model_construct(item=SimpleNamespace(**kw)))

    def tokens(tok_in, tok_out):
        last = SimpleNamespace(input_tokens=tok_in, output_tokens=tok_out, cached_input_tokens=0)
        return SimpleNamespace(payload=ThreadTokenUsageUpdatedNotification.model_construct(
            token_usage=SimpleNamespace(last=last)))

    def completed(status="completed"):
        turn = SimpleNamespace(status=SimpleNamespace(value=status), error=None)
        return SimpleNamespace(payload=TurnCompletedNotification.model_construct(turn=turn))

    class Handle:
        def __init__(self, events, hang=False):
            self.events, self.hang, self.interrupted = events, hang, threading.Event()

        def interrupt(self):
            self.interrupted.set()

        def stream(self):
            yield from self.events
            if self.hang:  # silent until interrupted, then the turn completes
                self.interrupted.wait(5)
                yield tokens(50, 5)
                yield completed("interrupted")

    def codex_for(handle):
        thread = SimpleNamespace(turn=lambda prompt: handle)
        return SimpleNamespace(thread_start=lambda **kw: thread)

    class Mono:
        def __init__(self):
            self.events = []
            self.spent = {}

        def record(self, kind, body, label=None):
            self.events.append((kind, label, body))

    project = Path("/nonexistent")
    real_read_prompt = core.read_prompt
    core.read_prompt = lambda project_, agent: "prompt"
    cfg = {"price_in_per_mtok": 1.0, "price_out_per_mtok": 10.0}
    try:
        handle = Handle([
            item(type="command_execution", command="pytest -q"),
            tokens(1000, 100),
            item(type="agent_message", text="All green.", phase=SimpleNamespace(value="final_answer")),
            completed(),
        ])
        text, used = daemon_codex.run_agent(codex_for(handle), project, project, "codex-1", cfg, "go", Mono())
        assert text == "All green.", "final answer returned"
        assert (used["input_tokens"] == 1000 and used["tool_rounds"] == 1
              and used["cost_usd"] == (1000 * 1.0 + 100 * 10.0) / 1e6), "usage in ledger terms, rounds counted"

        busy = Handle([item(type="command_execution", command=f"step {i}") for i in range(5)] + [completed()])
        try:
            daemon_codex.run_agent(codex_for(busy), project, project, "codex-1", {**cfg, "max_turns": 2}, "go", Mono())
            assert False, "turn limit interrupts the turn"
        except core.RunAborted as exc:
            assert (busy.interrupted.is_set() and "max_turns" in str(exc)
                  and exc.usage["tool_rounds"] == 2), "turn limit interrupts the turn"

        silent = Handle([item(type="command_execution", command="sleep 9999")], hang=True)
        started = time.time()
        try:
            daemon_codex.run_agent(codex_for(silent), project, project, "codex-1",
                                   {**cfg, "timeout_minutes": 0.002}, "go", Mono())
            assert False, "a silent turn is still timed out"
        except core.RunAborted as exc:
            assert "timed out" in str(exc) and time.time() - started < 3, "a silent turn is still timed out"
            assert exc.usage["input_tokens"] == 50, "and its usage is kept"
    finally:
        core.read_prompt = real_read_prompt


def test_openai_converse():
    """openai tool-calling loop"""
    def call(id_, name, arguments):
        return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))

    def response(content=None, calls=None, tok=(100, 10)):
        message = SimpleNamespace(content=content, tool_calls=calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=tok[0], completion_tokens=tok[1]),
        )

    class Client:
        def __init__(self, replies):
            self.replies, self.requests = list(replies), []
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            self.requests.append(json.loads(json.dumps(kwargs, default=str)))
            return self.replies.pop(0)

    class Session:
        def __init__(self):
            self.calls = []

        async def list_tools(self):
            tool = SimpleNamespace(name="get_inbox", description="inbox", input_schema={"type": "object"})
            return SimpleNamespace(tools=[tool])

        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return SimpleNamespace(content=[SimpleNamespace(text="[]")], is_error=False)

    class Mono:
        def __init__(self):
            self.events = []
            self.spent = {}

        def record(self, kind, body, label=None):
            self.events.append((kind, label, body))

        def tool_call(self, name, args):
            self.record("tool_use", args, name)

        def tool_result(self, name, result, is_error=False):
            self.record("error" if is_error else "tool_result", result, name)

    cfg = {"price_in_per_mtok": 1.0, "price_out_per_mtok": 10.0}
    client = Client([response(calls=[call("c1", "get_inbox", "{}")]), response(content="All done.")])
    session = Session()
    text, used = asyncio.run(daemon_openai.converse(client, session, "m", "sys", "do it", cfg, Mono()))
    assert text == "All done.", "final answer returned"
    assert session.calls == [("get_inbox", {})], "tool ran over mcp"
    second = client.requests[1]["messages"]
    assert (second[2]["role"] == "assistant"
          and second[2]["tool_calls"][0]["id"] == "c1"), "assistant turn carries its tool_calls"
    assert second[3] == {"role": "tool", "tool_call_id": "c1", "content": "[]"}, "tool answer uses role tool and the call id"
    assert (used == {"input_tokens": 200, "output_tokens": 20, "tool_rounds": 1,
                                              "cost_usd": (200 * 1.0 + 20 * 10.0) / 1e6}), "usage summed and priced"

    looping = Client([response(calls=[call(f"c{i}", "get_inbox", "{}")]) for i in range(3)])
    try:
        asyncio.run(daemon_openai.converse(looping, Session(), "m", "sys", "go", {**cfg, "max_turns": 3}, Mono()))
        assert False, "turn limit stops the loop"
    except core.RunAborted as exc:
        assert "max_turns" in str(exc) and exc.usage["tool_rounds"] == 3, "turn limit stops the loop"

    mono = Mono()
    bad = Client([response(calls=[call("c1", "get_inbox", "{not json")]), response(content="ok")])
    text, _ = asyncio.run(daemon_openai.converse(bad, Session(), "m", "sys", "go", cfg, mono))
    answer = bad.requests[1]["messages"][3]
    assert text == "ok" and answer["content"].startswith("error:"), "malformed arguments answered, not raised"


def test_non_worktree_done(tmp_path):
    """Test that a non-worktree agent calling reply(status='done') stays done."""
    # non-worktree task stays done
    project = tmp_path / "non-worktree-done-project"
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

    # no worktree_path on the task: "done" stays "done"
    core.update_task_status(conn, task_id, "in_progress")
    started = time.time()
    runtime.finish_task(conn, "dev-agent", task_id, "Task completed", started)

    # Check that the task status is done
    task = core.get_task(conn, task_id)
    assert task["status"] == "done", "non-worktree task stays done"

    conn.close()


def test_worktree_blocked_stays_blocked(tmp_path):
    """Test that a worktree agent replying 'blocked' stays blocked (not coerced to ready_to_merge)."""
    # worktree blocked task stays blocked
    project = tmp_path / "worktree-blocked-project"
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

    # a worktree task the agent already blocked: the hold is only for "done"
    core.update_task(conn, task_id, worktree_path=str(project / ".agents" / "worktrees" / f"task-{task_id}"))
    started = time.time()
    runtime.finish_task(conn, "dev-agent", task_id, "Task blocked", started)

    # Check that the task status is still blocked
    task = core.get_task(conn, task_id)
    assert task["status"] == "blocked", "worktree blocked task stays blocked"

    conn.close()


def _review_project(tmp_path, reviewer: bool) -> Path:
    project = tmp_path / "review-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    for args in (["init"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nmodel = "claude-opus-5"\nrole = "builder"\nworktree = true\n'
        + ('reviewer = "codex-1"\n' if reviewer else "")
        + '[agents.codex-1]\nbackend = "codex"\nmodel = "gpt-5-codex"\nrole = "reviewer"\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    conn.close()
    return project


@pytest.mark.parametrize("reviewer", [True, False])
def test_review_requested_on_ready_to_merge(tmp_path, reviewer):
    project = _review_project(tmp_path, reviewer)
    conn = core.connect(core.db_path(project))
    tid = add_ready(conn, "Add the parser", "d", "dev-agent")

    async def fake(prompt, options, mono):
        return "done", usage(10, 5)

    run_loop(project, fake, max_tasks=1)
    assert core.get_task(conn, tid)["status"] == "ready_to_merge", "dev task awaits merge"
    reviews = core.task_reviews(conn, tid)
    if reviewer:
        assert len(reviews) == 1, "one review task"
        assert reviews[0]["assigned_to"] == "codex-1" and reviews[0]["status"] == "ready", "ready for reviewer"
    else:
        assert reviews == [], "no reviewer, no review"
    conn.close()


def _stop_project(tmp_path):
    project = tmp_path / "stop-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    core.config_path(project).write_text('[agents.dev-agent]\nbackend = "openai"\nrole = "builder"\n')
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    return project, conn


def _serve(project, run, stop):
    loop.run_daemon(project, "dev-agent", "fake", lambda db, project_, agent_name, cfg: run,
                    poll_interval=0.02, quiet=True, stop=stop)


def test_serve_stop_already_set(tmp_path):
    """A set stop event ends an idle daemon at once, and it ends offline."""
    project, conn = _stop_project(tmp_path)
    stop = threading.Event()
    stop.set()

    async def never(prompt, workdir, mono):
        raise AssertionError("no task should run")

    started = time.time()
    _serve(project, never, stop)
    assert time.time() - started < 1, "returned promptly"
    assert core.get_agent(conn, "dev-agent")["status"] == "offline"
    conn.close()


def test_serve_stop_after_first_task(tmp_path):
    """Setting stop during a run lets that run finish, and no further task starts."""
    project, conn = _stop_project(tmp_path)
    stop = threading.Event()
    first = add_ready(conn, "First", "", "dev-agent")
    second = add_ready(conn, "Second", "", "dev-agent")

    async def run(prompt, workdir, mono):
        stop.set()
        return "Done.", {}

    _serve(project, run, stop)
    assert core.get_task(conn, first)["status"] != "ready"
    assert core.get_task(conn, second)["status"] == "ready"
    conn.close()


def test_serve_interrupt_blocks_task(tmp_path):
    """KeyboardInterrupt mid-run blocks the task, fails the run, and propagates."""
    project, conn = _stop_project(tmp_path)
    tid = add_ready(conn, "Interrupted", "", "dev-agent")

    async def run(prompt, workdir, mono):
        raise KeyboardInterrupt

    raised = False
    try:
        _serve(project, run, None)
    except KeyboardInterrupt:
        raised = True
    assert raised, "serve re-raises the interrupt"
    assert core.get_task(conn, tid)["status"] == "blocked"
    blockers = [m for m in core.task_messages(conn, tid) if m["msg_type"] == "blocker"]
    assert blockers and "interrupted" in blockers[0]["payload"]
    runs = core.task_runs(conn, tid)
    assert runs[0]["status"] == "failed" and runs[0]["exit_reason"] == "interrupted"
    conn.close()


def test_review_changes_requested_loop(tmp_path):
    """needs_approval sends the task back to its author; max_review_rounds ends the loop."""
    project = tmp_path / "review-loop-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("# Test")
    (project / ".gitignore").write_text(".agents/\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "openai"\nrole = "builder"\nworktree = true\n'
        'reviewer = "review-agent"\nmax_review_rounds = 2\n'
        '[agents.review-agent]\nbackend = "openai"\nrole = "reviewer"\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    dev_id = add_ready(conn, "Write notes", "", "dev-agent")
    runs = {"dev": 0}

    def dev_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            runs["dev"] += 1
            (workdir / f"n{runs['dev']}.md").write_text("x\n")
            return "Wrote notes.", {"input_tokens": 1, "output_tokens": 1}
        return run

    def review_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            review = next(t for t in core.list_tasks(db, status="in_progress") if t["kind"] == "review")
            core.call_tool(db, agent_name, "reply",
                           {"task_id": review["id"], "payload": "Needs tests", "status": "needs_approval"})
            return "Needs tests", {"input_tokens": 1, "output_tokens": 1}
        return run

    def serve(agent, make):
        loop.run_daemon(project, agent, "fake", make, poll_interval=0.02, max_tasks=1, quiet=True)

    reviews = []
    for round_no in (1, 2):
        serve("dev-agent", dev_runner)
        assert core.get_task(conn, dev_id)["status"] == "ready_to_merge", "held for review"
        reviews = core.task_reviews(conn, dev_id)
        assert len(reviews) == round_no, "a review is requested"
        serve("review-agent", review_runner)
        assert core.get_task(conn, dev_id)["status"] == "ready", "sent back to the author"
        done = core.get_task(conn, reviews[-1]["id"])
        assert done["status"] == "done" and done["review_outcome"] == "changes_requested", "review closed"
    assert runs["dev"] == 2, "dev ran again"

    serve("dev-agent", dev_runner)
    assert runs["dev"] == 3, "dev ran a third time"
    assert core.get_task(conn, dev_id)["status"] == "ready_to_merge", "ready to merge again"
    assert len(core.task_reviews(conn, dev_id)) == 2, "no third review"
    notes = [m for m in core.task_messages(conn, dev_id) if m["msg_type"] == "note"]
    assert any("over to you" in m["payload"] for m in notes), "human told it is theirs"
    conn.close()


def test_leftovers_committed_before_ready_to_merge(tmp_path, monkeypatch):
    """When the task shows up in the merge queue, its branch already holds the run's changes."""
    project = tmp_path / "leftovers-project"
    (project / ".agents" / "prompts").mkdir(parents=True)
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    (project / "README.md").write_text("# Test")
    (project / ".gitignore").write_text(".agents/\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)
    core.config_path(project).write_text(
        '[agents.dev-agent]\nbackend = "openai"\nrole = "builder"\nworktree = true\n'
    )
    conn = core.connect(core.db_path(project))
    core.init_db(conn)
    core.sync_agents_from_config(conn, project)
    ok_id = add_ready(conn, "Write notes", "", "dev-agent")
    bad_id = add_ready(conn, "Crash midway", "", "dev-agent")
    snapshots = {}

    def snapshot(task_id):
        path = core.get_task(conn, task_id)["worktree_path"]
        status = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout
        files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=path,
                               capture_output=True, text=True).stdout.split()
        snapshots[task_id] = (status, files)

    real_finish, real_fail = core.finish_task, core.fail_task

    def finish_task(db, agent, task_id, *a, **kw):
        snapshot(task_id)
        return real_finish(db, agent, task_id, *a, **kw)

    def fail_task(db, agent, task_id, *a, **kw):
        snapshot(task_id)
        return real_fail(db, agent, task_id, *a, **kw)

    monkeypatch.setattr(loop.core, "finish_task", finish_task)
    monkeypatch.setattr(loop.core, "fail_task", fail_task)

    def make_runner(db, project_, agent_name, cfg):
        async def run(prompt, workdir, mono):
            (workdir / f"out-{workdir.name}.md").write_text("changes\n")
            if "Crash" in prompt:
                raise RuntimeError("boom")
            return "done", {}
        return run

    loop.run_daemon(project, "dev-agent", "fake", make_runner, poll_interval=0.02, max_tasks=2)

    for task_id in (ok_id, bad_id):
        status, files = snapshots[task_id]
        assert status == "", f"task {task_id}: worktree clean when the status changes"
        assert files == [f"out-task-{task_id}.md"], f"task {task_id}: branch tip holds the run's changes"
    assert core.get_task(conn, ok_id)["status"] == "ready_to_merge"
    assert core.get_task(conn, bad_id)["status"] == "blocked"
    conn.close()
