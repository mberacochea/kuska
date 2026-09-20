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

    check("both backends registered", set(BACKENDS) == {"claude", "codex"}, BACKENDS)
    try:
        run("llama-cpp", project, "codex-1")
        check("unknown backend refused", False)
    except SystemExit as exc:
        check("unknown backend refused", "no daemon for backend" in str(exc))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="achka-daemon-"))
    try:
        project = make_project(tmp)
        check_tools_in_process(project)
        check_loop(project)
        check_claim_guard(project)
        check_codex_wiring(project)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} checks passed")


if __name__ == "__main__":
    main()
