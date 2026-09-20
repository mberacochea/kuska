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
    check_cost_anomaly,
    docs_get,
    docs_set,
    get_inbox,
    get_task,
    log_event,
    reply,
    task_dependencies,
    task_messages,
)


def estimate_token_count(text: str) -> int:
    """Estimate token count from text using a simple heuristic.

    Uses a rough approximation: ~1 token per 4 characters for English text.
    This is a conservative estimate for most LLMs. For precise counting,
    use the tokenizer of the specific model.

    Args:
        text: Text to estimate token count for.

    Returns:
        int: Estimated number of tokens.

    Examples:
        >>> estimate_token_count("hello world")
        3
        >>> estimate_token_count("a" * 400)
        100
    """
    return max(1, len(text) // 4)


def get_workflow_context(
    db: SqliteDatabase,
    task: dict,
    source_agent: str | None = None,
) -> str:
    """Retrieve shared context from previous agent in workflow chain.

    When an agent finishes with status="needs_approval", it stores
    structured context via docs_set for the next agent to retrieve.
    This reduces token usage by 20-30% by allowing the next agent to
    skip re-parsing message history.

    Context keys follow the pattern: task_{task_id}_{source_agent}_context
    where source_agent is the agent that produced the context.

    Args:
        db: SqliteDatabase instance for this project.
        task: Task dict (must include 'id').
        source_agent: Name of agent to retrieve context from. If None,
                     tries to detect the most recent context available
                     (planning-agent → dev-agent → review-agent order).

    Returns:
        str: Formatted context section or empty string if none found.

    Examples:
        >>> context = get_workflow_context(db, task, "planning-agent")
        >>> if context:
        ...     # Include in prompt for dev-agent
    """
    if not source_agent:
        # Try to find context from expected workflow: planning → dev → review
        # Check in reverse order (most recent first)
        for agent in ["dev-agent", "planning-agent"]:
            doc_key = f"task_{task['id']}_{agent}_context"
            content = docs_get(db, doc_key)
            if content:
                source_agent = agent
                break
        if not source_agent:
            return ""
    else:
        doc_key = f"task_{task['id']}_{source_agent}_context"
        content = docs_get(db, doc_key)
        if not content:
            return ""

    return f"## Context from {source_agent}\n\n{content}\n"


def store_workflow_context(
    db: SqliteDatabase,
    agent_name: str,
    task_id: int,
    context: str,
) -> None:
    """Store structured context for the next agent in the workflow.

    Called when an agent completes with status="needs_approval" to pass
    context forward to dependent tasks. This avoids token waste by letting
    the next agent skip re-reading message history.

    Context key format: task_{task_id}_{agent_name}_context
    This allows multiple agents to store context for a single task.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent storing context.
        task_id: Associated task ID.
        context: Structured context (markdown, JSON, or any format).

    Examples:
        >>> plan_context = json.dumps({"phase": 1, "files_to_modify": [...]})
        >>> store_workflow_context(db, "planning-agent", task_id, plan_context)
    """
    doc_key = f"task_{task_id}_{agent_name}_context"
    docs_set(db, doc_key, context, updated_by=agent_name)


def compose_task_prompt(
    db: SqliteDatabase,
    agent_name: str,
    task: dict,
    limit_history: bool = True,
) -> str:
    """Compose the prompt text for an agent invocation: task, context, and thread.

    Each agent invocation starts with a blank slate, so all context must be
    bundled into the initial prompt:
    - The task title and description
    - Task dependencies (what this task waits for)
    - File claims from other agents (avoid editing those)
    - Workflow context from previous agent (if available)
    - Message thread history (earlier attempts, questions, answers)

    Workflow context passing (Phase 4.1):
    - When an agent completes with status="needs_approval", it can store
      structured context via docs_set(db, f"task_{id}_{agent}_context", ...)
    - The next agent automatically receives this context without re-parsing

    Message history summarization (by default):
    - Keeps the last 5 messages in full detail
    - Summarizes older messages into a "Prior context" section
    - Summary format: [sender]: [msg_type] - [payload_preview]

    This prompt is small - a few hundred tokens - and is not where an
    invocation's cost lives. What costs money is the agentic loop that follows:
    every tool round-trip re-sends the whole conversation, so a large tool
    result is paid for once per remaining round. Optimize there, not here.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent being invoked.
        task: Task dict (must include 'id', 'title', and optional 'description').
        limit_history: If True (default), summarize old messages and keep last 5 in full.
                      Set to False for full history.

    Returns:
        str: Formatted prompt text, ready to prepend to the agent's input.

    Examples:
        >>> prompt = compose_task_prompt(db, "claude-worker", task)
        >>> # Returns markdown like:
        >>> # # Task 42: Fix bug in parser
        >>> # Task description here...
        >>> # ## This task depends on...
        >>> # ## Files other agents are working on...
        >>> # ## Earlier on this task
        >>> # ### Prior context (summarized)
        >>> # - agent-1: result - Successfully implemented feature X...
        >>> # ### Recent messages (last 5)
        >>> # **agent-2 -> recipient** (question): ...
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

    # Include workflow context from previous agent if available
    workflow_context = get_workflow_context(db, task)
    if workflow_context:
        parts += [workflow_context, ""]

    inbox = get_inbox(db, agent_name)
    all_history = [
        m for m in task_messages(db, task["id"]) if m["id"] not in {i["id"] for i in inbox}
    ]

    if all_history:
        parts += ["## Earlier on this task", ""]

        if limit_history and len(all_history) > 5:
            # older messages as one-line summaries, recent ones in full
            parts += ["### Prior context (summarized)", ""]
            for m in all_history[:-5]:
                preview = " ".join(((m["payload"] or "(no content)")[:100]).split())
                parts += [f"- **{m['sender']}**: {m['msg_type']} - {preview}"]
            parts += ["", "### Recent messages (last 5)", ""]
            recent = all_history[-5:]
        else:
            recent = all_history

        for m in recent:
            parts += [f"**{m['sender']} -> {m['recipient']}** ({m['msg_type']}):", m["payload"] or "", ""]

        parts += [""]

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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    tool_rounds: int = 0,
    cost_usd: float = 0.0,
) -> int:
    """Close out one invocation, recording its cost without double-counting.

    If the agent already called the `reply` tool during this run, we update
    that message's token/cost fields instead of creating a duplicate result.
    This ensures token counts are accurate even when the agent logs its own
    completion.

    Also checks for token usage anomalies and logs warnings if a task used
    significantly more tokens than the rolling average.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent that ran.
        task_id: Task being worked on.
        payload: Result summary or daemon message (used if no prior reply).
        since: Timestamp of invocation start (to find recent results).
        input_tokens: Fresh input tokens, charged at full price.
        output_tokens: Total tokens generated.
        cache_read_tokens: Input served from cache, at roughly a tenth the price.
        cache_write_tokens: Input written to cache, at roughly 1.25x the price.
        tool_rounds: API round-trips in this turn - the real cost driver.
        cost_usd: Total cost in USD, as reported by the backend.

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
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
                tool_rounds=tool_rounds, cost_usd=cost_usd,
            ).where(Message.id == existing.id).execute()
            msg_id = int(existing.id)
        else:
            task = get_task(db, task_id)
            # whatever hold the agent put the task under is the agent's call to keep
            terminal = ("blocked", "done", "needs_approval")
            status = task["status"] if task and task["status"] in terminal else "done"
            msg_id = reply(
                db, agent_name, task_id, payload,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens,
                tool_rounds=tool_rounds, cost_usd=cost_usd, status=status,
            )

    # cost is the only comparable figure: token volume is dominated by cache
    # reads, which are priced an order of magnitude below fresh input
    if cost_usd > 0:
        check_cost_anomaly(db, task_id, cost_usd, anomaly_threshold=2.0, window_size=20)

    return msg_id


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
