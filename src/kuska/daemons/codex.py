"""One Codex-backed agent's daemon.

    kuska daemon <agent-name>

Same shape as the Claude daemon, with the SDK call swapped. Codex has no
in-process Python tool registration, so it reaches kuska the other way its
CLI supports: an external stdio MCP server, which is `kuska mcp` serving
the same TOOL_SPECS the Claude daemon registers in-process.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import kuska as core


def log(line: str, error: bool = False) -> None:
    """Daemons usually run under nohup or systemd, so never buffer their log."""
    print(line, file=sys.stderr if error else sys.stdout, flush=True)


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
    from openai_codex import Sandbox

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

    Returns (text, usage). Nothing carries over between invocations.

    `project` is the database location; `workdir` is where the agent runs
    (the worktree for worktree agents, the project root otherwise).
    """
    from openai_codex.models import (
        ItemCompletedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )

    thread = codex.thread_start(
        cwd=str(workdir),
        model=cfg.get("model"),
        config=mcp_config(project, agent_name),
        developer_instructions=core.read_prompt(project, agent_name),
        sandbox=sandbox_preset(cfg.get("sandbox")),
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

            # Create monologue early so worktree rebase failures can be narrated
            mono = core.Monologue(db, agent_name, task["id"], quiet=quiet)

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

            # Construct Codex per task so cwd is the worktree
            codex = Codex(CodexConfig(cwd=str(workdir), codex_bin=cfg.get("codex_bin")))

            prompt, inbox_message_ids = core.compose_task_prompt(db, agent_name, task)
            mono.record("prompt", prompt)

            try:
                text, usage = run_agent(codex, project, workdir, agent_name, cfg, prompt, mono)
                text = text.strip()
                tok_in, tok_out, cost = usage_of(usage, cfg)
                cache_read, cache_write = cache_of(usage)
            except Exception as exc:
                mono.record("error", f"run failed: {exc}")
                core.send_message(db, agent_name, core.HUMAN, task["id"], "blocker", f"run failed: {exc}")
                core.update_task_status(db, task["id"], "blocked")
                log(f"[{agent_name}] task {task['id']} failed: {exc}", error=True)
            else:
                # keyword args: finish_task also takes a tool-round count,
                # which this backend does not report
                core.finish_task(
                    db, agent_name, task["id"], text, started,
                    input_tokens=tok_in, output_tokens=tok_out, cost_usd=cost,
                    cache_read_tokens=cache_read, cache_write_tokens=cache_write,
                    worktree_branch=workdir_branch,
                )
                # Mark inbox messages as read only after successful run
                core.mark_messages_read(db, inbox_message_ids)
                final = (core.get_task(db, task["id"]) or task)["status"]
                # Record if status was coerced from done to ready_to_merge for worktree tasks
                if final == "ready_to_merge" and workdir_branch is not None:
                    mono.record("system", workdir_branch, label="ready to merge")
                mono.record("result", text, label=f"{final} - ${cost:.4f}, {tok_in}/{tok_out} tok")
                log(f"[{agent_name}] task {task['id']} {final} (${cost:.4f}, {tok_in}/{tok_out} tok)")
            finally:
                # Commit any uncommitted changes before finishing
                if cfg.get("worktree") and workdir != project:
                    worktree.commit_all(workdir, f"wip: task {task['id']} uncommitted changes")
                # Close the codex instance for this task
                codex.close()

            core.heartbeat(db, agent_name, "idle")
    finally:
        core.heartbeat(db, agent_name, "offline")
        db.close()
