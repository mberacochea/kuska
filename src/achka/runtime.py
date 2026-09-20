"""Helpers shared by the per-backend daemons.

The state-tracking half of running an agent: what goes into a fresh
invocation's prompt, and what comes back out of it into the ledger."""

from __future__ import annotations

import json
import sys
import uuid

from peewee import SqliteDatabase

from .models import MODELS, Message
from .store import (
    active_claims,
    get_inbox,
    get_task,
    log_event,
    reply,
    task_dependencies,
    task_messages,
)


def compose_task_prompt(db: SqliteDatabase, agent_name: str, task: dict) -> str:
    """Compose the prompt text for an agent invocation: task, context, and thread.

    Each agent invocation starts with a blank slate, so all context must be
    bundled into the initial prompt:
    - The task title and description
    - Task dependencies (what this task waits for)
    - File claims from other agents (avoid editing those)
    - Message thread history (earlier attempts, questions, answers)

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent being invoked.
        task: Task dict (must include 'id', 'title', and optional 'description').

    Returns:
        str: Formatted prompt text, ready to prepend to the agent's input.

    Examples:
        >>> prompt = compose_task_prompt(db, "claude-worker", task)
        >>> # Returns markdown like:
        >>> # # Task 42: Fix bug in parser
        >>> # Task description here...
        >>> # ## This task depends on...
        >>> # ## Files other agents are working on...
    """
    parts = [f"# Task {task['id']}: {task['title']}", ""]
    if task.get("description"):
        parts += [task["description"], ""]

    deps = task_dependencies(db, task["id"])
    if deps:
        parts += ["## This task depends on", ""]
        parts += [f"- task {d['id']} ({d['status']}): {d['title']}" for d in deps]
        parts += [""]

    others = [c for c in active_claims(db) if c["agent"] != agent_name]
    if others:
        parts += ["## Files other agents are working on right now", ""]
        parts += [
            f"- `{c['path']}` - {c['agent']}"
            + (f" ({c['note']})" if c["note"] else "")
            + (f", task {c['task_id']}" if c["task_id"] else "")
            for c in others
        ]
        parts += [
            "",
            "Do not edit those. Claim what you are about to change with "
            "`claim_files` first, and message whoever holds a file you need.",
            "",
        ]

    inbox = get_inbox(db, agent_name)
    history = [m for m in task_messages(db, task["id"]) if m["id"] not in {i["id"] for i in inbox}]
    if history:
        parts += ["## Earlier on this task", ""]
        for m in history:
            parts += [f"**{m['sender']} -> {m['recipient']}** ({m['msg_type']}):", m["payload"] or "", ""]
    if inbox:
        parts += ["## New messages for you", ""]
        for m in inbox:
            scope = f" (task {m['task_id']})" if m["task_id"] and m["task_id"] != task["id"] else ""
            parts += [f"**{m['sender']}**{scope} ({m['msg_type']}):", m["payload"] or "", ""]
    return "\n".join(parts)


def estimate_cost(cfg: dict, input_tokens: int, output_tokens: int) -> float:
    """Estimate the cost of an LLM call from token counts and configured prices.

    For backends that report token usage but not direct cost, this function
    computes the USD cost from per-million-token rates in the config. Used
    for billing and cost tracking.

    Args:
        cfg: Config dict with keys:
             - price_in_per_mtok (float | None): Cost per million input tokens.
             - price_out_per_mtok (float | None): Cost per million output tokens.
        input_tokens: Number of tokens in the prompt.
        output_tokens: Number of tokens generated.

    Returns:
        float: Estimated cost in USD. Returns 0.0 if prices are not configured.

    Examples:
        >>> cfg = {"price_in_per_mtok": 1.0, "price_out_per_mtok": 3.0}
        >>> cost = estimate_cost(cfg, 1000000, 500000)
        >>> cost
        2.5
    """
    price_in = float(cfg.get("price_in_per_mtok", 0) or 0)
    price_out = float(cfg.get("price_out_per_mtok", 0) or 0)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def finish_task(
    db: SqliteDatabase,
    agent_name: str,
    task_id: int,
    payload: str,
    since: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> int:
    """Close out one invocation, recording its cost without double-counting.

    If the agent already called the `reply` tool during this run, we update
    that message's token/cost fields instead of creating a duplicate result.
    This ensures token counts are accurate even when the agent logs its own
    completion.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent that ran.
        task_id: Task being worked on.
        payload: Result summary or daemon message (used if no prior reply).
        since: Timestamp of invocation start (to find recent results).
        input_tokens: Total tokens consumed by this run.
        output_tokens: Total tokens generated.
        cost_usd: Total cost in USD.

    Returns:
        int: Message ID of the result (newly created or updated).
    """
    with db.bind_ctx(MODELS):
        existing = (
            Message.select(Message.id)
            .where(
                (Message.sender == agent_name)
                & (Message.task_id == task_id)
                & (Message.msg_type == "result")
                & (Message.ts >= since)
            )
            .order_by(Message.ts.desc())
            .first()
        )
        if existing:
            Message.update(
                input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd
            ).where(Message.id == existing.id).execute()
            return int(existing.id)

    task = get_task(db, task_id)
    # whatever hold the agent put the task under is the agent's call to keep
    terminal = ("blocked", "done", "needs_approval")
    status = task["status"] if task and task["status"] in terminal else "done"
    return reply(
        db, agent_name, task_id, payload,
        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd, status=status,
    )


# --------------------------------------------------------------------------
# the monologue: what an agent is doing right now, on the terminal and in the
# DB. Both daemons funnel their backend's stream through this, so the terminal
# format and the audit trail are the same for every backend.
# --------------------------------------------------------------------------

GLYPHS = {
    "prompt": "\u25b8",
    "thinking": "\u00b7",
    "text": "\u25aa",
    "tool_use": "\u2699",
    "tool_result": "\u2190",
    "system": "\u2508",
    "error": "\u2718",
    "result": "\u2714",
}
TERMINAL_WIDTH = 160


def one_line(text: str, width: int = TERMINAL_WIDTH) -> str:
    """Collapse a block of output to one readable terminal line.

    Joins multiple lines into one (collapsing whitespace) and truncates if
    needed, adding an ellipsis to indicate truncation.

    Args:
        text: Text to collapse.
        width: Maximum width before truncation (default TERMINAL_WIDTH).

    Returns:
        str: Single-line summary.

    Examples:
        >>> one_line("this is\\na long\\ntext", width=10)
        "this is a..."
    """
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "\u2026"


class Monologue:
    """One agent invocation's narration - the thinking, tool calls, and results.

    Records the agent's internal monologue to the audit trail (events table)
    and prints a live one-line summary to the terminal so observers can
    monitor progress. The same event stream both logs costs and enables live
    debugging.

    Attributes:
        db: SqliteDatabase instance.
        agent: Name of the agent.
        task_id: Associated task (may be None).
        quiet: If True, suppress terminal output (events still logged).
        run_id: Unique invocation ID (random 12-char hex).
    """

    def __init__(self, db: SqliteDatabase, agent: str, task_id: int | None, quiet: bool = False):
        """Initialize a monologue for one agent run.

        Args:
            db: SqliteDatabase instance for this project.
            agent: Agent name.
            task_id: Associated task ID (may be None).
            quiet: If True, skip terminal output (default False).
        """
        self.db = db
        self.agent = agent
        self.task_id = task_id
        self.quiet = quiet
        self.run_id = uuid.uuid4().hex[:12]

    def record(self, kind: str, body: str, label: str | None = None) -> None:
        """Record an event to the audit trail and print a one-line summary.

        Args:
            kind: Event kind (e.g., "thinking", "tool_use", "error", "result").
            body: Full event body (serialized to JSON if not a string).
            label: Optional annotation (e.g., tool name).
        """
        body = body if isinstance(body, str) else json.dumps(body, default=str)
        log_event(self.db, self.agent, self.task_id, self.run_id, kind, body, label)
        if self.quiet:
            return
        head = f"[{self.agent}] {GLYPHS.get(kind, ' ')} {label or kind}"
        print(f"{head}  {one_line(body)}", file=sys.stderr if kind == "error" else sys.stdout, flush=True)

    def tool_call(self, name: str, args) -> None:
        """Record a tool invocation.

        Args:
            name: Name of the tool being called.
            args: Arguments passed to the tool.
        """
        self.record("tool_use", args, label=name)

    def tool_result(self, name: str, result, is_error: bool = False) -> None:
        """Record a tool result or error.

        Args:
            name: Name of the tool that was called.
            result: Result returned by the tool.
            is_error: If True, record as an error event.
        """
        self.record("error" if is_error else "tool_result", result, label=name)
