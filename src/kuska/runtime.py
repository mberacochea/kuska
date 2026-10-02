"""Helpers shared by the per-backend daemons.

The state-tracking half of running an agent: what goes into a fresh
invocation's prompt, and what comes back out of it into the ledger."""

from __future__ import annotations

import json
import sys
import uuid

from peewee import SqliteDatabase

from .eventfmt import GLYPHS, TERMINAL_WIDTH, glyph, one_line, summarize  # noqa: F401 - GLYPHS/TERMINAL_WIDTH/one_line re-exported for callers that reach them via runtime
from .markdown import as_markdown
from .models import MODELS, Message
from .store import (
    check_cost_anomaly,
    docs_get,
    docs_set,
    get_inbox,
    get_task,
    log_event,
    mark_messages_read,
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

    When an agent finishes with status="needs_approval", it stores a
    Markdown report via docs_set for the next agent to retrieve.
    This reduces token usage by 20-30% by allowing the next agent to
    skip re-parsing message history.

    Context keys follow the pattern: task_{task_id}_{source_agent}_context
    where source_agent is the agent that produced the context. When
    source_agent is not specified, we look for context from upstream
    task dependencies in reverse order (most recent first).

    Args:
        db: SqliteDatabase instance for this project.
        task: Task dict (must include 'id').
        source_agent: Name of agent to retrieve context from. If None,
                     tries to detect the most recent context available
                     by searching upstream task dependencies in reverse order.

    Returns:
        str: Formatted context section or empty string if none found.

    Examples:
        >>> context = get_workflow_context(db, task, "planning-agent")
        >>> if context:
        ...     # Include in prompt for dev-agent
    """
    if not source_agent:
        # Get upstream tasks and extract their agents in reverse order (most recent first)
        deps = task_dependencies(db, task["id"])
        if deps:
            # Check dependencies in reverse order (most recent first)
            # Context is stored on the dependency task with the dependency task's assigned agent
            for dep in reversed(deps):
                if dep.get("assigned_to"):
                    doc_key = f"task_{dep['id']}_{dep['assigned_to']}_context"
                    content = docs_get(db, doc_key)
                    if content:
                        source_agent = dep["assigned_to"]
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
    """Store a Markdown handover report for the next agent in the workflow.

    Called when an agent completes with status="needs_approval" to pass
    context forward to dependent tasks. This avoids token waste by letting
    the next agent skip re-reading message history.

    Context key format: task_{task_id}_{agent_name}_context
    This allows multiple agents to store context for a single task.

    The report is a document: the web UI renders it as Markdown and
    `export_markdown` folds it into plan.md. A model that hands over a JSON
    dump anyway gets it rewritten into sections by `as_markdown`.

    Args:
        db: SqliteDatabase instance for this project.
        agent_name: Name of the agent storing context.
        task_id: Associated task ID.
        context: The handover report, in Markdown.

    Examples:
        >>> report = "## Summary\\n\\nSplit the cache token columns.\\n"
        >>> store_workflow_context(db, "planning-agent", task_id, report)
    """
    doc_key = f"task_{task_id}_{agent_name}_context"
    title = f"Task {task_id}: {agent_name} report"
    docs_set(db, doc_key, as_markdown(context, title=title), updated_by=agent_name, task_id=task_id)


def compose_task_prompt(
    db: SqliteDatabase,
    agent_name: str,
    task: dict,
    limit_history: bool = True,
) -> tuple[str, list[int]]:
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
        tuple[str, list[int]]: A tuple of (prompt_text, inbox_message_ids).
            - prompt_text: Formatted prompt text, ready to prepend to the agent's input.
            - inbox_message_ids: List of message IDs that were fetched from the inbox
                                (should be marked as read after a successful run).

    Examples:
        >>> prompt, msg_ids = compose_task_prompt(db, "claude-worker", task)
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

    # Include workflow context from previous agent if available
    workflow_context = get_workflow_context(db, task)
    if workflow_context:
        parts += [workflow_context, ""]

    # Fetch inbox messages without marking them as read - mark as read only
    # after a successful run (see task R4: unread messages consumed before the
    # run that needs them)
    inbox = get_inbox(db, agent_name, mark_read=False)
    inbox_message_ids = [m["id"] for m in inbox]
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

    return "\n".join(parts), inbox_message_ids


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
    worktree_branch: str | None = None,
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
        worktree_branch: The git branch the task ran on (if using worktrees).

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
            terminal = ("blocked", "done", "needs_approval", "ready_to_merge")
            status = task["status"] if task and task["status"] in terminal else "done"
            # if a worktree task would result in "done", coerce to "ready_to_merge"
            # so the branch requires human review before merging
            if worktree_branch is not None and status == "done":
                status = "ready_to_merge"
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

# GLYPHS, TERMINAL_WIDTH and one_line() now live in eventfmt.py - that module
# owns "how an event reads" for all three surfaces (terminal, web, export),
# and this one needs eventfmt.summarize() below, so the shared primitives had
# to move rather than the two modules importing each other. Re-exported here
# (imported above) since callers - web.py, tests, __init__.py - reach them as
# `runtime.GLYPHS` / `runtime.one_line`.


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
        event = {"agent": self.agent, "task_id": self.task_id, "run_id": self.run_id,
                  "kind": kind, "label": label, "body": body}
        head = f"[{self.agent}] {glyph(kind)} {label or kind}"
        print(f"{head}  {summarize(event)}", file=sys.stderr if kind == "error" else sys.stdout, flush=True)

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
