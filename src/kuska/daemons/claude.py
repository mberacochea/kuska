"""The Claude backend: one fresh `query()` per task.

The loop around it - claiming, worktrees, the ledger - is loop.py. It never
holds a running conversation, so context can neither accumulate nor go
stale. The kuska tools are exposed to Claude in-process through
create_sdk_mcp_server(), so there is no second process and no duplicated
logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from peewee import SqliteDatabase

import kuska as core

from . import loop
from .loop import log

# tools whose input names the one file they change: a call clears just that
# file's read record
WRITING_TOOLS = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}

# tools that cannot change a file. Anything else - Bash, a subagent, a tool
# this list has never heard of - may have changed any file, so it clears every
# read record: a refused re-read of a file that did change would leave the
# model reasoning about stale contents.
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "TodoWrite"}


def tool_guard(db: SqliteDatabase, project: Path, workdir: Path, agent_name: str, mono_ref: dict, reads: dict):
    """Permission callback: refuse a file it has already read, and check command safety.

    The read check is about money rather than safety. Every tool result stays
    in the conversation and is re-sent on each of the turn's remaining
    round-trips, so re-reading a thousand-line file to "check" an edit is paid
    for dozens of times over. `reads` maps a path to the ranges already
    fetched this run; a write to that path clears it, and any tool that is
    not known to be read-only (Bash, a subagent) clears them all, because
    then the file may really have changed.

    This is also the one function whose return value `as_pretooluse_hook()`
    below turns into a PreToolUse decision, and a PreToolUse hook is the only
    thing the SDK guarantees to run for *every* tool call - regardless of
    `allowed_tools`, and regardless of `permission_mode` (including
    "acceptEdits", which auto-accepts Write/Edit without ever consulting
    `can_use_tool`, and "bypassPermissions", which disables `can_use_tool`
    outright). So the command check - guardrails.check_tool()/check_command(),
    covering things like `rm -rf`, a bare `git push --force` or a write that
    lands outside the project - has to live in this same function rather than
    a separate callback, or nothing would guarantee it runs. It runs first:
    safety before economy.

    `project` is the database location; `workdir` is the guardrail base (the
    worktree for worktree agents, the project root otherwise).
    """
    async def can_use_tool(tool_name: str, tool_input: dict, context):
        mono = mono_ref.get("mono")

        verdict = core.check_tool(tool_name, tool_input, workdir)
        if not verdict["allowed"]:
            if mono:
                mono.record("system", verdict["command"], label=f"refused: {verdict['rule']}")
            return PermissionResultDeny(message=core.refusal_text(verdict))

        if tool_name == "Read":
            raw = tool_input.get("file_path")
            if not raw:
                return PermissionResultAllow()
            path = core.normalize_path(raw, workdir)
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
                        "again. No edit, shell command or subagent has run since, so it has "
                        "not changed. If you need a part you have not seen yet, Read it with "
                        "offset/limit for that range, or use Grep to find what you are "
                        "looking for."
                    )
                )
            reads.setdefault(path, set()).add(span)
            return PermissionResultAllow()

        keys = WRITING_TOOLS.get(tool_name)
        if keys:
            for key in keys:
                if tool_input.get(key):
                    reads.pop(core.normalize_path(tool_input[key], workdir), None)
        elif tool_name not in READ_ONLY_TOOLS and not tool_name.startswith("mcp__kuska__"):
            reads.clear()
        return PermissionResultAllow()

    return can_use_tool


def as_pretooluse_hook(guard):
    """Adapt a `tool_guard` callable into a PreToolUse `HookCallback`.

    Same decision, different envelope: `guard` speaks `can_use_tool`'s
    `PermissionResultAllow`/`Deny`, a PreToolUse hook speaks
    `permissionDecision: "allow"/"deny"` inside `hookSpecificOutput`. Reusing
    `guard` rather than re-deriving the decision keeps there being exactly
    one place that decides a tool call and exactly one place that mutates
    read state for it.

    `make_runner` wires this hook but does NOT also pass `guard` as
    `can_use_tool`: `allowed_tools` still lists the bare `mcp__kuska__*`
    entries on purpose (see `build_options`), and the SDK emits
    `CanUseToolShadowedWarning` - a `UserWarning` - whenever `can_use_tool`
    is set alongside any bare allowed-tool entry, whether or not a hook
    also covers the same calls. Confirmed live: running under
    `-W error::UserWarning` turned that warning into a crash on startup.
    The hook alone already makes every decision `can_use_tool` would have
    (see above), so leaving `can_use_tool` unset costs nothing and avoids
    the warning entirely.
    """

    async def hook(input_data, tool_use_id, context):
        result = await guard(input_data["tool_name"], input_data["tool_input"], context)
        if result.behavior == "allow":
            # No decision: an explicit allow would skip the SDK's own
            # permission checks and sandbox, so the guard may only narrow.
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result.message,
            }
        }

    return hook


def build_tools(db: SqliteDatabase, agent_name: str, specs: list[dict] | None = None):
    """Wrap this agent's kuska tools (default: all of them) as in-process SDK tools."""
    specs = core.TOOL_SPECS if specs is None else specs

    def make(spec: dict):
        async def handler(args):
            try:
                value = core.call_tool(db, agent_name, spec["name"], args, specs)
                text = core.tool_result_text(value)
            except Exception as exc:  # the model gets to see and recover from it
                return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            return {"content": [{"type": "text", "text": text}]}

        handler.__name__ = spec["name"]
        return tool(spec["name"], spec["description"], spec["schema"])(handler)

    return [make(spec) for spec in specs]


def sandbox_settings(cfg: dict) -> dict:
    """Bash sandboxing (bubblewrap on Linux, Seatbelt on macOS) from the
    agent's `sandbox` setting, shared with codex: blank or anything but
    "full-access" confines shell commands' writes to the run's workdir.

    This, not guardrails.py, is the boundary: the regex rules catch mistakes,
    and `sh -c` walks around them. allowUnsandboxedCommands is off because
    the model could otherwise ask to leave the sandbox, and the PreToolUse
    hook would wave that through like any other command. git runs outside:
    a worktree commits into the main checkout's .git, which a workdir-only
    sandbox cannot write - the guardrails still cover destructive git."""
    if (cfg.get("sandbox") or "").replace("_", "-") in ("full-access", "danger-full-access"):
        return {"enabled": False}
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": ["git"],
    }


def build_options(project: Path, workdir: Path, agent_name: str, cfg: dict, tools, can_use_tool=None, pretooluse_hook=None):
    """Wire up one agent's SDK options.

    `allowed_tools` only lists `mcp__kuska__*` - kuska's own tools, which the
    guard allows unconditionally, so pre-approving them is a pure saving. It
    used to also list bare "Read"/"Write"/"Edit"/"Bash": an `allowed_tools`
    entry with no `(...)` specifier auto-approves the *whole* tool before
    `can_use_tool` is ever consulted, so that was silently short-circuiting
    every check `tool_guard` makes - they passed tests that call `tool_guard`
    directly and did nothing in a real run. `allowedTools` is a permission allowlist, not a
    tool-enablement list, so removing those entries does not remove the
    tools - it just means Read/Write/Edit/Bash now go through permission
    evaluation like everything else, which is what makes `pretooluse_hook`
    (see `as_pretooluse_hook()`) the thing actually guarding them.

    `can_use_tool` stays as a parameter for callers and tests that want it,
    but `make_runner` does not pass one: setting it alongside the bare
    `mcp__kuska__*` entries above makes the SDK emit `CanUseToolShadowedWarning`
    regardless of whether a hook also covers those calls.

    `project` and `workdir` are separate: `project` is the database location,
    `workdir` is where the agent runs (the worktree for worktree agents, the
    project root otherwise).
    """
    hooks = {"PreToolUse": [HookMatcher(hooks=[pretooluse_hook])]} if pretooluse_hook else None
    limits = core.run_limits(cfg)

    return ClaudeAgentOptions(
        can_use_tool=can_use_tool,
        hooks=hooks,
        mcp_servers={"kuska": create_sdk_mcp_server(name="kuska", tools=tools)},
        allowed_tools=[f"mcp__kuska__{t.name}" for t in tools],
        system_prompt={"type": "file", "path": str(core.prompt_path(project, agent_name))},
        cwd=str(workdir),
        model=cfg.get("model"),
        permission_mode=cfg.get("permission_mode", "acceptEdits"),
        sandbox=sandbox_settings(cfg),
        max_turns=limits["max_turns"],
        max_budget_usd=limits["max_budget_usd"],
    )


def running_usage(calls, rounds: int) -> dict:
    """Token totals over the API calls seen so far - a cut-off run's lower
    bound. No cost: the SDK reports dollars only in the final result."""
    def total(key: str) -> int:
        return sum(int(u.get(key, 0) or 0) for u in calls)

    return {
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cache_read_tokens": total("cache_read_input_tokens"),
        "cache_write_tokens": total("cache_creation_input_tokens"),
        "tool_rounds": rounds,
    }


STOP_REASONS = {
    "error_max_turns": "hit its max_turns limit",
    "error_max_budget_usd": "hit its max_budget_usd limit",
    "error_during_execution": "the run errored mid-execution",
}


async def run_agent(prompt: str, options, mono) -> tuple[str, dict]:
    """One fresh invocation, narrated as it goes.

    Returns (text, usage) where usage carries the turn's token counts kept
    apart by price - fresh input, cache reads and cache writes cost 1x, 0.1x
    and 1.25x respectively, so summing them tells you nothing - plus the
    number of tool round-trips, which is what actually drives the bill: every
    round re-sends the whole conversation.
    """
    chunks: list[str] = []
    tool_names: dict[str, str] = {}
    rounds = 0
    result = None
    per_call: dict[str, dict] = {}  # one API call can arrive as several messages
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            if message.usage:
                per_call[message.message_id or str(len(per_call))] = message.usage
                mono.spent = running_usage(per_call.values(), rounds)
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    mono.record("text", block.text)
                elif isinstance(block, ThinkingBlock):
                    mono.record("thinking", block.thinking)
                elif isinstance(block, ToolUseBlock):
                    tool_names[block.id] = block.name
                    rounds += 1
                    mono.spent["tool_rounds"] = rounds
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
            # Skip noise subtypes (e.g., thinking_tokens heartbeat)
            if message.subtype not in core.eventfmt.NOISE_SUBTYPES:
                mono.record("system", json.dumps(message.data, default=str), label=message.subtype)
        elif isinstance(message, ResultMessage):
            result = message

    text = (result.result if result and result.result else "\n\n".join(chunks)).strip()
    usage = (result.usage if result else None) or {}

    def count(key: str) -> int:
        return int(usage.get(key, 0) or 0)

    totals = {
        "input_tokens": count("input_tokens"),
        "output_tokens": count("output_tokens"),
        "cache_read_tokens": count("cache_read_input_tokens"),
        "cache_write_tokens": count("cache_creation_input_tokens"),
        "tool_rounds": rounds,
        "cost_usd": float(result.total_cost_usd or 0.0) if result else 0.0,
    }
    # error_max_turns, error_max_budget_usd, error_during_execution: the run
    # stopped short, so whatever text it left is not a result
    if result is not None and (result.is_error or result.subtype.startswith("error")):
        raise core.RunAborted(STOP_REASONS.get(result.subtype, result.subtype), totals)
    return text or "(no output)", totals


def make_runner(db: SqliteDatabase, project: Path, agent_name: str, cfg: dict):
    """One Claude invocation per task, guarded by `tool_guard`.

    The guard needs the monologue of whichever run is in flight, and a
    scratch record of what that run has already read; both are reset per run.
    """
    if cfg.get("permission_mode") == "bypassPermissions":
        log(
            f"[{agent_name}] permission_mode=bypassPermissions: the SDK disables "
            "can_use_tool entirely under this mode, so redundant reads and the "
            "command guardrails are enforced ONLY by the PreToolUse hook",
            error=True,
        )
    current: dict = {}
    reads: dict = {}

    async def run(prompt: str, workdir: Path, mono) -> tuple[str, dict]:
        current["mono"] = mono
        reads.clear()
        guard = tool_guard(db, project, workdir, agent_name, current, reads)
        options = build_options(
            project, workdir, agent_name, cfg, build_tools(db, agent_name, core.toolset(cfg)),
            # not can_use_tool=guard: allowed_tools still bare-lists mcp__kuska__*
            # (see build_options), which would make the SDK warn that can_use_tool
            # is shadowed - a UserWarning, confirmed live to be fatal under
            # -W error::UserWarning. The hook below makes every decision
            # can_use_tool would have, so there is nothing can_use_tool would add.
            pretooluse_hook=as_pretooluse_hook(guard),
        )
        try:
            return await run_agent(prompt, options, mono)
        finally:
            current.pop("mono", None)

    return run


def run_daemon(
    project: Path,
    agent_name: str,
    poll_interval: float = 2.0,
    max_tasks: int | None = None,
    quiet: bool = False,
    stop=None,
    worker=None,
) -> None:
    loop.run_daemon(project, agent_name, "claude", make_runner, poll_interval, max_tasks, quiet, stop=stop, worker=worker)
