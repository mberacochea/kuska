"""The prompt a fresh agent invocation starts from."""

from __future__ import annotations

from peewee import SqliteDatabase

from .store import get_inbox, handover_sections, task_dependencies, task_messages


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
    - What each dependency handed over (see handover_sections) - including
      the answer, when the dependency is an answer task this one asked for
    - Message thread history (earlier attempts, questions, answers)
    - Unread messages for this agent

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
        >>> prompt, msg_ids = compose_task_prompt(db, "dev-agent", task)
        >>> # Returns markdown like:
        >>> # # Task 42: Fix bug in parser
        >>> # Task description here...
        >>> # ## This task depends on...
        >>> # ## Context from planning-agent (task 41: Plan the parser)
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
    workflow_context = handover_sections(db, task)
    if workflow_context:
        parts += [workflow_context, ""]

    # leave them unread for now: the daemon marks them read only once a run has
    # succeeded, so a run that fails cannot swallow a message it never acted on
    inbox = get_inbox(db, agent_name, mark_read=False, for_task=task["id"])
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
            parts += [f"**{m['sender']}** ({m['msg_type']}):", m["payload"] or "", ""]

    return "\n".join(parts), inbox_message_ids
