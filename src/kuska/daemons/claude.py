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

# tools whose input names a file the agent is about to change; the daemon
# claims it on the agent's behalf rather than trusting it to remember
WRITING_TOOLS = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}


def tool_guard(db: SqliteDatabase, project: Path, workdir: Path, agent_name: str, mono_ref: dict, reads: dict):
    """Permission callback: refuse a file it has already read, and check command safety.

    The read check is about money rather than safety. Every tool result stays
    in the conversation and is re-sent on each of the turn's remaining
    round-trips, so re-reading a thousand-line file to "check" an edit is paid
    for dozens of times over. `reads` maps a path to the ranges already
    fetched this run; a write to that path clears it, because then the file
    really has changed.

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
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

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

        # Clear reads for paths that are about to change, since the file will really have changed
        normalized = [core.normalize_path(p, workdir) for p in paths]
        for path in normalized:
            reads.pop(path, None)
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

    `serve_agent` wires this hook but does NOT also pass `guard` as
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
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result.message,
            }
        }

    return hook


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


def build_options(project: Path, workdir: Path, agent_name: str, cfg: dict, tools, can_use_tool=None, pretooluse_hook=None):
    """Wire up one agent's SDK options.

    `allowed_tools` only lists `mcp__kuska__*` - kuska's own tools, which the
    guard allows unconditionally, so pre-approving them is a pure saving. It
    used to also list bare "Read"/"Write"/"Edit"/"Bash": an `allowed_tools`
    entry with no `(...)` specifier auto-approves the *whole* tool before
    `can_use_tool` is ever consulted, so that was silently short-circuiting
    every claim-conflict and redundant-read check `claim_guard` makes - they
    were passing tests that call `claim_guard` directly and doing nothing in
    a real run. `allowedTools` is a permission allowlist, not a
    tool-enablement list, so removing those entries does not remove the
    tools - it just means Read/Write/Edit/Bash now go through permission
    evaluation like everything else, which is what makes `pretooluse_hook`
    (see `as_pretooluse_hook()`) the thing actually guarding them.

    `can_use_tool` stays as a parameter for callers and tests that want it,
    but `serve_agent` does not pass one: setting it alongside the bare
    `mcp__kuska__*` entries above makes the SDK emit `CanUseToolShadowedWarning`
    regardless of whether a hook also covers those calls.

    `project` and `workdir` are separate: `project` is the database location,
    `workdir` is where the agent runs (the worktree for worktree agents, the
    project root otherwise).
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, create_sdk_mcp_server

    hooks = {"PreToolUse": [HookMatcher(hooks=[pretooluse_hook])]} if pretooluse_hook else None

    return ClaudeAgentOptions(
        can_use_tool=can_use_tool,
        hooks=hooks,
        mcp_servers={"kuska": create_sdk_mcp_server(name="kuska", tools=tools)},
        allowed_tools=[f"mcp__kuska__{s['name']}" for s in core.TOOL_SPECS],
        system_prompt={"type": "file", "path": str(core.prompt_path(project, agent_name))},
        cwd=str(workdir),
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
            # Skip noise subtypes (e.g., thinking_tokens heartbeat)
            if message.subtype not in core.eventfmt.NOISE_SUBTYPES:
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

    import kuska.worktree as worktree

    # Git preflight for worktree mode
    if cfg.get("worktree"):
        if not worktree.is_git_repo(project):
            raise SystemExit(
                f"[{agent_name}] worktree=true but {project} is not inside a git work tree. "
                f"Run `git init && git commit` or turn worktree off in .agents/config.toml"
            )
        if not worktree.has_commits(project):
            raise SystemExit(
                f"[{agent_name}] worktree=true but {project} has no commits. "
                f"Run `git init && git commit` or turn worktree off in .agents/config.toml"
            )

    # the guard needs the monologue of whichever run is in flight, and a
    # scratch record of what this run has already read
    current: dict = {}
    reads: dict = {}

    mode = cfg.get("permission_mode", "acceptEdits")
    if mode == "bypassPermissions":
        log(
            f"[{agent_name}] permission_mode=bypassPermissions: the SDK disables "
            "can_use_tool entirely under this mode, so claim conflicts, redundant "
            "reads and the command guardrails are enforced ONLY by the PreToolUse "
            "hook from here on",
            error=True,
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

            # Create monologue early so worktree rebase failures can be narrated
            mono = core.Monologue(db, agent_name, task["id"], quiet=quiet)
            current["mono"] = mono
            reads.clear()  # read tracking is per run, not per daemon

            # Worktree setup
            workdir_branch = None
            if cfg.get("worktree"):
                base = worktree.base_branch(project)
                try:
                    path, branch, created = worktree.ensure_worktree(project, task["id"], task["title"], base)
                except RuntimeError as exc:
                    mono.record("warning", str(exc), label="worktree setup failed - working directly in project")
                    core.send_message(
                        db, agent_name, core.HUMAN, task["id"], "note",
                        f"could not set up a worktree for this task: {exc}. Working directly in the project checkout."
                    )
                    workdir = project
                else:
                    if not created:  # re-queued task: its base may be stale
                        ok, detail = worktree.rebase_onto(path, base)
                        if not ok:
                            mono.record("warning", detail, label=f"rebase onto {base} failed - continuing on the old base")
                            core.send_message(
                                db, agent_name, core.HUMAN, task["id"], "note",
                                f"branch {branch} could not be rebased onto {base}: {detail}. "
                                f"Working from the old base; resolve by hand before merging."
                            )
                    core.update_task(db, task["id"], worktree_path=str(path))
                    workdir = path
                    workdir_branch = branch
            else:
                workdir = project

            prompt, inbox_message_ids = core.compose_task_prompt(db, agent_name, task)
            mono.record("prompt", prompt)

            # Build options and guard per task with workdir
            guard = tool_guard(db, project, workdir, agent_name, current, reads)
            options = build_options(
                project, workdir, agent_name, cfg, build_tools(db, agent_name),
                # not can_use_tool=guard: allowed_tools still bare-lists mcp__kuska__*
                # (see build_options), which would make the SDK warn that can_use_tool
                # is shadowed - a UserWarning, confirmed live to be fatal under
                # -W error::UserWarning. The hook below makes every decision
                # can_use_tool would have, so there is nothing can_use_tool would add.
                pretooluse_hook=as_pretooluse_hook(guard),
            )

            try:
                text, usage = await run_agent(prompt, options, mono)
            except Exception as exc:
                mono.record("error", f"run failed: {exc}")
                core.send_message(db, agent_name, core.HUMAN, task["id"], "blocker", f"run failed: {exc}")
                core.update_task_status(db, task["id"], "blocked")
                log(f"[{agent_name}] task {task['id']} failed: {exc}", error=True)
            else:
                core.finish_task(db, agent_name, task["id"], text, started, worktree_branch=workdir_branch, **usage)
                # Mark inbox messages as read only after successful run
                core.mark_messages_read(db, inbox_message_ids)
                final = (core.get_task(db, task["id"]) or task)["status"]
                # Record if status was coerced from done to ready_to_merge for worktree tasks
                if final == "ready_to_merge" and workdir_branch is not None:
                    mono.record("system", workdir_branch, label="ready to merge")
                # cost and round count are the honest summary; token volume is
                # dominated by cache reads at a tenth the price
                summary = (
                    f"${usage['cost_usd']:.4f}, {usage['tool_rounds']} rounds, "
                    f"{usage['input_tokens']}+{usage['cache_read_tokens']}c/"
                    f"{usage['output_tokens']} tok"
                )
                mono.record("result", text, label=f"{final} - {summary}")
                log(f"[{agent_name}] task {task['id']} {final} ({summary})")
            finally:
                # Commit any uncommitted changes before finishing
                if cfg.get("worktree") and workdir != project:
                    worktree.commit_all(workdir, f"wip: task {task['id']} uncommitted changes")

            current.pop("mono", None)
            core.heartbeat(db, agent_name, "idle")
    finally:
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
