"""One Claude-backed agent's daemon.

    kuska daemon <agent-name>

Thin by design: poll for a task, run one fresh `query()` for it, log the
result and the turn's cost, go back to polling. It never holds a running
conversation, so context can neither accumulate nor go stale. The kuska
tools are exposed to Claude in-process through create_sdk_mcp_server(), so
there is no second process and no duplicated logic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from peewee import SqliteDatabase

import kuska as core


def log(line: str, error: bool = False) -> None:
    """Daemons usually run under nohup or systemd, so never buffer their log."""
    print(line, file=sys.stderr if error else sys.stdout, flush=True)

FILE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite", "WebFetch"]

# tools whose input names a file the agent is about to change; the daemon
# claims it on the agent's behalf rather than trusting it to remember
WRITING_TOOLS = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}


def claim_guard(db: SqliteDatabase, project: Path, agent_name: str, mono_ref: dict, reads: dict):
    """Permission callback: take a claim before an edit, point the agent at
    whoever is already holding the file, and refuse a file it has already read.

    Claims are cooperative - this does not protect the filesystem, it makes
    the overlap visible while there is still time to talk about it.

    The read check is about money rather than safety. Every tool result stays
    in the conversation and is re-sent on each of the turn's remaining
    round-trips, so re-reading a thousand-line file to "check" an edit is paid
    for dozens of times over. `reads` maps a path to the ranges already
    fetched this run; a write to that path clears it, because then the file
    really has changed.
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(tool_name: str, tool_input: dict, context):
        mono = mono_ref.get("mono")

        if tool_name == "Read":
            raw = tool_input.get("file_path")
            if not raw:
                return PermissionResultAllow()
            path = core.normalize_path(raw, project)
            # None stands for "the whole file", which subsumes every range
            span = (
                None if tool_input.get("offset") is None and tool_input.get("limit") is None
                else (tool_input.get("offset"), tool_input.get("limit"))
            )
            seen = reads.get(path)
            if seen is not None and (None in seen or span in seen or span is None):
                if mono:
                    mono.record("system", f"{path} ({span or 'whole file'})", label="redundant read")
                return PermissionResultDeny(
                    message=(
                        f"You already read {path} earlier in this turn and its contents "
                        "are still in your context - scroll back rather than fetching it "
                        "again. Nothing has changed it since. If you need a part you have "
                        "not seen yet, Read it with offset/limit for that range, or use "
                        "Grep to find what you are looking for."
                    )
                )
            reads.setdefault(path, set()).add(span)
            return PermissionResultAllow()

        keys = WRITING_TOOLS.get(tool_name)
        if not keys:
            return PermissionResultAllow()
        paths = [tool_input[k] for k in keys if tool_input.get(k)]
        if not paths:
            return PermissionResultAllow()

        # Pre-fetch active claims once for all paths (optimization)
        all_claims = core.active_claims(db)
        for raw in paths:
            path = core.normalize_path(raw, project)
            holders = core.claim_holders(db, path, agent=agent_name, cached_claims=all_claims)
            if holders:
                holder = holders[0]
                if mono:
                    mono.record(
                        "system", f"{path} is held by {holder['agent']}", label="claim conflict"
                    )
                return PermissionResultDeny(
                    message=(
                        f"{path} is being changed right now by {holder['agent']}"
                        f" (task {holder['task_id']}"
                        + (f": {holder['note']}" if holder.get("note") else "")
                        + "). Do not edit it on top of their work. Use send_message to ask "
                        "them about it, check get_inbox for anything they have already told "
                        "you, and if you cannot make progress without this file, reply with "
                        "status 'blocked' and stop."
                    )
                )

        run_id = getattr(mono, "run_id", None)
        task_id = getattr(mono, "task_id", None)
        normalized = [core.normalize_path(p, project) for p in paths]
        for path in normalized:
            reads.pop(path, None)  # it is about to change; a re-read is fair
        result = core.claim_files(
            db, agent_name, normalized,
            task_id=task_id, run_id=run_id, note=f"editing via {tool_name}",
        )
        if mono:
            mono.record("system", ", ".join(result["claimed"]), label="claimed")
        return PermissionResultAllow()

    return can_use_tool


def build_tools(db: SqliteDatabase, agent_name: str):
    """Wrap kuska's shared tool set as in-process SDK tools."""
    from claude_agent_sdk import tool

    def make(spec: dict):
        async def handler(args):
            try:
                value = core.call_tool(db, agent_name, spec["name"], args)
                text = core.tool_result_text(value)
            except Exception as exc:  # the model gets to see and recover from it
                return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            return {"content": [{"type": "text", "text": text}]}

        handler.__name__ = spec["name"]
        return tool(spec["name"], spec["description"], spec["schema"])(handler)

    return [make(spec) for spec in core.TOOL_SPECS]


def build_options(project: Path, agent_name: str, cfg: dict, tools, can_use_tool=None):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

    return ClaudeAgentOptions(
        can_use_tool=can_use_tool,
        mcp_servers={"kuska": create_sdk_mcp_server(name="kuska", tools=tools)},
        allowed_tools=[f"mcp__kuska__{s['name']}" for s in core.TOOL_SPECS] + FILE_TOOLS,
        system_prompt={"type": "file", "path": str(core.prompt_path(project, agent_name))},
        cwd=str(project),
        model=cfg.get("model"),
        permission_mode=cfg.get("permission_mode", "acceptEdits"),
    )


async def run_agent(prompt: str, options, mono) -> tuple[str, dict]:
    """One fresh invocation, narrated as it goes.

    Returns (text, usage) where usage carries the turn's token counts kept
    apart by price - fresh input, cache reads and cache writes cost 1x, 0.1x
    and 1.25x respectively, so summing them tells you nothing - plus the
    number of tool round-trips, which is what actually drives the bill: every
    round re-sends the whole conversation.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    chunks: list[str] = []
    tool_names: dict[str, str] = {}
    rounds = 0
    result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    mono.record("text", block.text)
                elif isinstance(block, ThinkingBlock):
                    mono.record("thinking", block.thinking)
                elif isinstance(block, ToolUseBlock):
                    tool_names[block.id] = block.name
                    rounds += 1
                    mono.tool_call(block.name, block.input)
        elif isinstance(message, UserMessage):
            # tool results come back addressed to the agent
            for block in message.content if isinstance(message.content, list) else []:
                if isinstance(block, ToolResultBlock):
                    mono.tool_result(
                        tool_names.get(block.tool_use_id, "tool"),
                        block.content,
                        is_error=bool(block.is_error),
                    )
        elif isinstance(message, SystemMessage):
            mono.record("system", json.dumps(message.data, default=str), label=message.subtype)
        elif isinstance(message, ResultMessage):
            result = message

    text = (result.result if result and result.result else "\n\n".join(chunks)).strip()
    usage = (result.usage if result else None) or {}

    def count(key: str) -> int:
        return int(usage.get(key, 0) or 0)

    return text or "(no output)", {
        "input_tokens": count("input_tokens"),
        "output_tokens": count("output_tokens"),
        "cache_read_tokens": count("cache_read_input_tokens"),
        "cache_write_tokens": count("cache_creation_input_tokens"),
        "tool_rounds": rounds,
        "cost_usd": float(result.total_cost_usd or 0.0) if result else 0.0,
    }


async def serve_agent(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
) -> None:
    db = core.connect(core.db_path(project))
    core.init_db(db)
    core.sync_agents_from_config(db, project)
    cfg = core.agent_config(project, agent_name)
    # the guard needs the monologue of whichever run is in flight, and a
    # scratch record of what this run has already read
    current: dict = {}
    reads: dict = {}
    options = build_options(
        project, agent_name, cfg, build_tools(db, agent_name),
        can_use_tool=claim_guard(db, project, agent_name, current, reads),
    )

    log(f"[{agent_name}] claude daemon up on {project} (model={cfg.get('model') or 'default'})")
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
            current["mono"] = mono
            reads.clear()  # read tracking is per run, not per daemon
            mono.record("prompt", prompt)

            try:
                text, usage = await run_agent(prompt, options, mono)
            except Exception as exc:
                mono.record("error", f"run failed: {exc}")
                core.send_message(db, agent_name, core.HUMAN, task["id"], "blocker", f"run failed: {exc}")
                core.update_task_status(db, task["id"], "blocked")
                log(f"[{agent_name}] task {task['id']} failed: {exc}", error=True)
            else:
                core.finish_task(db, agent_name, task["id"], text, started, **usage)
                final = (core.get_task(db, task["id"]) or task)["status"]
                # cost and round count are the honest summary; token volume is
                # dominated by cache reads at a tenth the price
                summary = (
                    f"${usage['cost_usd']:.4f}, {usage['tool_rounds']} rounds, "
                    f"{usage['input_tokens']}+{usage['cache_read_tokens']}c/"
                    f"{usage['output_tokens']} tok"
                )
                mono.record("result", text, label=f"{final} - {summary}")
                log(f"[{agent_name}] task {task['id']} {final} ({summary})")
            # a claim never outlives the run that took it
            core.release_run(db, mono.run_id)
            current.pop("mono", None)
            core.heartbeat(db, agent_name, "idle")
    finally:
        core.release_files(db, agent_name)
        core.heartbeat(db, agent_name, "offline")
        db.close()


def run_daemon(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
) -> None:
    asyncio.run(serve_agent(project, agent_name, poll_interval, max_tasks, quiet))
