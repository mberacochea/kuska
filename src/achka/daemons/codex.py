"""One Codex-backed agent's daemon.

    achka daemon <agent-name>

Same shape as the Claude daemon, with the SDK call swapped. Codex has no
in-process Python tool registration, so it reaches achka the other way its
CLI supports: an external stdio MCP server, which is `achka mcp` serving
the same TOOL_SPECS the Claude daemon registers in-process.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import achka as core


def log(line: str, error: bool = False) -> None:
    """Daemons usually run under nohup or systemd, so never buffer their log."""
    print(line, file=sys.stderr if error else sys.stdout, flush=True)


def mcp_command() -> list[str]:
    """How to start this project's MCP server: the frozen binary, or python -m."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable]
    return [sys.executable, "-m", "achka"]


def mcp_config(project: Path, agent_name: str) -> dict:
    """Point Codex at this project's achka MCP server, acting as this agent."""
    command, *head = mcp_command()
    return {
        "mcp_servers": {
            "achka": {
                "command": command,
                "args": [*head, "--project", str(project), "mcp", "--agent", agent_name],
            }
        }
    }


# how a codex thread item maps onto the monologue's vocabulary; anything not
# listed is some flavour of tool call, which is what makes this beta-proof
ITEM_KINDS = {"agent_message": "text", "reasoning": "thinking", "error": "error"}
ITEM_BODY_FIELDS = ("text", "command", "summary", "content", "arguments", "changes", "message")


def describe_item(item) -> tuple[str, str, str]:
    """(kind, label, body) for one thread item, whatever shape the beta gives."""
    it = getattr(item, "root", item)
    itype = getattr(getattr(it, "type", None), "value", None) or str(getattr(it, "type", "item"))
    kind = ITEM_KINDS.get(itype, "tool_use")
    label = itype
    if itype == "mcp_tool_call":
        label = f"{getattr(it, 'server', 'mcp')}.{getattr(it, 'tool', '?')}"
    elif itype == "command_execution":
        label = "shell"
    for attr in ITEM_BODY_FIELDS:
        value = getattr(it, attr, None)
        if value:
            return kind, label, value if isinstance(value, str) else json.dumps(value, default=str)
    dump = it.model_dump_json() if hasattr(it, "model_dump_json") else str(it)
    return kind, label, dump


def run_agent(codex, project: Path, agent_name: str, cfg: dict, prompt: str, mono):
    """One fresh thread per task, narrated as the turn streams back.

    Returns (text, usage). Nothing carries over between invocations.
    """
    from openai_codex.models import (
        ItemCompletedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )

    thread = codex.thread_start(
        cwd=str(project),
        model=cfg.get("model"),
        config=mcp_config(project, agent_name),
        developer_instructions=core.read_prompt(project, agent_name),
        sandbox=cfg.get("sandbox"),
    )
    handle = thread.turn(prompt)
    usage, turn, final_text, last_text = None, None, None, None

    for event in handle.stream():
        payload = event.payload
        if isinstance(payload, ItemCompletedNotification):
            kind, label, body = describe_item(payload.item)
            mono.record(kind, body, label=None if kind in ("text", "thinking") else label)
            if kind == "text":
                last_text = body
                phase = getattr(getattr(payload.item, "root", payload.item), "phase", None)
                if getattr(phase, "value", phase) == "final_answer":
                    final_text = body
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
            usage = payload.token_usage
        elif isinstance(payload, TurnCompletedNotification):
            turn = payload.turn

    status = getattr(getattr(turn, "status", None), "value", None)
    if status == "failed":
        error = getattr(turn, "error", None)
        raise RuntimeError(getattr(error, "message", None) or "codex turn failed")
    return (final_text or last_text or "(no output)"), usage


def usage_of(usage, cfg: dict) -> tuple[int, int, float]:
    """Codex reports tokens but not dollars; price them from config.toml."""
    last = getattr(usage, "last", None)
    tok_in = int(getattr(last, "input_tokens", 0) or 0)
    tok_out = int(getattr(last, "output_tokens", 0) or 0)
    return tok_in, tok_out, core.estimate_cost(cfg, tok_in, tok_out)


def run_daemon(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
) -> None:
    from openai_codex import Codex, CodexConfig

    db = core.connect(core.db_path(project))
    core.init_db(db)
    core.sync_agents_from_config(db, project)
    cfg = core.agent_config(project, agent_name)
    codex = Codex(CodexConfig(cwd=str(project), codex_bin=cfg.get("codex_bin")))

    log(f"[{agent_name}] codex daemon up on {project} (model={cfg.get('model') or 'default'})")
    core.heartbeat(db, agent_name, "idle")
    handled = 0
    try:
        while max_tasks is None or handled < max_tasks:
            task = core.wait_for_task(db, agent_name, poll_interval)
            handled += 1
            log(f"[{agent_name}] task {task['id']}: {task['title']}")
            core.heartbeat(db, agent_name, "working", task["id"])
            started = core.now()
            prompt = core.compose_task_prompt(db, agent_name, task)
            mono = core.Monologue(db, agent_name, task["id"], quiet=quiet)
            mono.record("prompt", prompt)
            try:
                text, usage = run_agent(codex, project, agent_name, cfg, prompt, mono)
                text = text.strip()
                tok_in, tok_out, cost = usage_of(usage, cfg)
            except Exception as exc:
                mono.record("error", f"run failed: {exc}")
                core.send_message(db, agent_name, core.HUMAN, task["id"], "blocker", f"run failed: {exc}")
                core.update_task_status(db, task["id"], "blocked")
                log(f"[{agent_name}] task {task['id']} failed: {exc}", error=True)
            else:
                core.finish_task(db, agent_name, task["id"], text, started, tok_in, tok_out, cost)
                final = (core.get_task(db, task["id"]) or task)["status"]
                mono.record("result", text, label=f"{final} - ${cost:.4f}, {tok_in}/{tok_out} tok")
                log(f"[{agent_name}] task {task['id']} {final} (${cost:.4f}, {tok_in}/{tok_out} tok)")
            # codex has no per-tool callback to claim through, so its claims
            # are whatever the agent took itself - released the same way
            core.release_run(db, mono.run_id)
            core.heartbeat(db, agent_name, "idle")
    finally:
        core.release_files(db, agent_name)
        core.heartbeat(db, agent_name, "offline")
        codex.close()
        db.close()
