"""The Codex backend: one fresh thread per task, run by loop.py.

Codex has no
in-process Python tool registration, so it reaches kuska the other way its
CLI supports: an external stdio MCP server, which is `kuska mcp` serving
the same TOOL_SPECS the Claude daemon registers in-process.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from openai_codex import Codex, CodexConfig, Sandbox
from openai_codex.models import (
    ItemCompletedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)

import kuska as core

from . import loop


def mcp_command() -> list[str]:
    """How to start this project's MCP server: the frozen binary, or python -m."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable]
    return [sys.executable, "-m", "kuska"]


def mcp_config(project: Path, agent_name: str) -> dict:
    """Point Codex at this project's kuska MCP server, acting as this agent."""
    command, *head = mcp_command()
    return {
        "mcp_servers": {
            "kuska": {
                "command": command,
                "args": [*head, "--project", str(project), "mcp", "--agent", agent_name],
            }
        }
    }


def sandbox_preset(value):
    """Config gives us a string; the SDK insists on its own Sandbox enum.

    `thread_start(sandbox=...)` rejects anything that is not a `Sandbox`
    member, so a plain "workspace-write" out of config.toml would fail every
    turn. Blank (the UI's "leave it to codex" choice) means don't pass one.
    Underscores and the wire spelling "danger-full-access" are accepted too,
    since both show up in hand-written configs.
    """
    if value is None or isinstance(value, Sandbox):
        return value
    name = str(value).strip().replace("_", "-")
    if not name:
        return None
    if name == "danger-full-access":
        name = "full-access"
    try:
        return Sandbox(name)
    except ValueError:
        allowed = ", ".join(preset.value for preset in Sandbox)
        raise ValueError(f"unknown sandbox {value!r}; expected one of: {allowed}") from None


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


def run_agent(codex, project: Path, workdir: Path, agent_name: str, cfg: dict, prompt: str, mono):
    """One fresh thread per task, narrated as the turn streams back.

    Returns (text, usage) with usage in the ledger's terms (see totals_of).
    Nothing carries over between invocations.

    `project` is the database location; `workdir` is where the agent runs
    (the worktree for worktree agents, the project root otherwise).
    """
    thread = codex.thread_start(
        cwd=str(workdir),
        model=cfg.get("model"),
        config=mcp_config(project, agent_name),
        developer_instructions=core.read_prompt(project, agent_name),
        # workspace-write unless config says otherwise: the shell's writes
        # stay inside the workdir, the same default the claude backend uses
        sandbox=sandbox_preset(cfg.get("sandbox") or "workspace-write"),
    )
    handle = thread.turn(prompt)
    limits = core.run_limits(cfg)
    state = {"usage": None, "rounds": 0}

    # the timeout runs on its own thread, so even a turn that has gone silent
    # is stopped. The client serialises its requests, so interrupting from
    # here is safe, and the interrupted turn still completes with its usage.
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        try:
            handle.interrupt()
        except Exception:  # the turn finished just as the timer fired
            pass

    watchdog = threading.Timer(limits["timeout_s"], on_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        turn, text = stream_turn(handle, cfg, limits, mono, state)
    finally:
        watchdog.cancel()

    usage = totals_of(state["usage"], cfg, state["rounds"])
    if timed_out.is_set():
        raise core.RunAborted(f"timed out after {limits['timeout_s'] / 60:g} minutes", usage)
    status = getattr(getattr(turn, "status", None), "value", None)
    if status == "failed":
        error = getattr(turn, "error", None)
        raise core.RunAborted(getattr(error, "message", None) or "codex turn failed", usage)
    return text or "(no output)", usage


def stream_turn(handle, cfg: dict, limits: dict, mono, state: dict):
    """Narrate one turn's events until it completes; (turn, final text).

    Keeps the running usage and tool-call count in `state`, and stops the
    turn - raising RunAborted - once it reaches its turn or budget limit."""
    turn, final_text, last_text = None, None, None
    for event in handle.stream():
        payload = event.payload
        if isinstance(payload, ItemCompletedNotification):
            kind, label, body = describe_item(payload.item)
            mono.record(kind, body, label=None if kind in ("text", "thinking") else label)
            if kind == "tool_use":
                state["rounds"] += 1
            if kind == "text":
                last_text = body
                phase = getattr(getattr(payload.item, "root", payload.item), "phase", None)
                if getattr(phase, "value", phase) == "final_answer":
                    final_text = body
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
            state["usage"] = payload.token_usage
        elif isinstance(payload, TurnCompletedNotification):
            turn = payload.turn
        if turn is not None:  # finished: trailing events only carry usage
            continue
        reason = None
        if limits["max_turns"] and state["rounds"] >= limits["max_turns"]:
            reason = "hit its max_turns limit"
        elif limits["max_budget_usd"] and usage_of(state["usage"], cfg)[2] >= limits["max_budget_usd"]:
            reason = "hit its max_budget_usd limit"
        if reason:
            handle.interrupt()
            raise core.RunAborted(reason, totals_of(state["usage"], cfg, state["rounds"]))
    return turn, final_text or last_text


def usage_of(usage, cfg: dict) -> tuple[int, int, float]:
    """Codex reports tokens but not dollars; price them from config.toml."""
    last = getattr(usage, "last", None)
    tok_in = int(getattr(last, "input_tokens", 0) or 0)
    tok_out = int(getattr(last, "output_tokens", 0) or 0)
    return tok_in, tok_out, core.estimate_cost(cfg, tok_in, tok_out)


def cache_of(usage) -> tuple[int, int]:
    """(read, written) cached prompt tokens, for the run's stats line.

    Kept apart from `usage_of` because these are not priced: codex counts
    cached tokens inside `input_tokens`, so charging them again would
    double-count the turn.
    """
    last = getattr(usage, "last", None)
    read = int(getattr(last, "cached_input_tokens", 0) or 0)
    written = int(getattr(last, "cache_write_input_tokens", 0) or 0)
    return read, written


def totals_of(usage, cfg: dict, rounds: int = 0) -> dict:
    """The turn's usage as the ledger's keyword arguments."""
    tok_in, tok_out, cost = usage_of(usage, cfg)
    cache_read, cache_write = cache_of(usage)
    return {
        "input_tokens": tok_in, "output_tokens": tok_out, "cost_usd": cost,
        "cache_read_tokens": cache_read, "cache_write_tokens": cache_write, "tool_rounds": rounds,
    }


def make_runner(db, project: Path, agent_name: str, cfg: dict):
    """One fresh codex thread per task, in that task's workdir.

    Codex's SDK is synchronous, so the run blocks the daemon's event loop and
    the loop's own timeout cannot preempt it; run_agent enforces the limits
    itself as events stream in."""
    async def run(prompt: str, workdir: Path, mono) -> tuple[str, dict]:
        codex = Codex(CodexConfig(cwd=str(workdir), codex_bin=cfg.get("codex_bin")))
        try:
            return run_agent(codex, project, workdir, agent_name, cfg, prompt, mono)
        finally:
            codex.close()

    return run


def run_daemon(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
    stop=None,
) -> None:
    loop.run_daemon(project, agent_name, "codex", make_runner, poll_interval, max_tasks, quiet, stop=stop)
