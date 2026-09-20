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
    """The text handed to a fresh agent invocation: the task plus its thread.

    Each invocation starts empty, so anything the agent needs to know - earlier
    attempts, answers to questions it asked, notes from the human - has to be
    written into the prompt here.
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
    """Cost from per-million-token prices in config.toml, for backends that
    report tokens but not dollars. Returns 0.0 when no prices are configured."""
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
    """Close out one invocation, recording its cost exactly once.

    If the agent already logged its own result through the `reply` tool, that
    message gets this turn's token/cost numbers instead of a second result
    being inserted. Otherwise the daemon's summary becomes the result.
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
    """Collapse a block of output to one readable terminal line."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "\u2026"


class Monologue:
    """One agent invocation's narration.

    `record` writes the full text to the events table - that is the audit
    trail - and prints a one-line summary so somebody watching the terminal
    can see what the agent is doing while it does it.
    """

    def __init__(self, db: SqliteDatabase, agent: str, task_id: int | None, quiet: bool = False):
        self.db = db
        self.agent = agent
        self.task_id = task_id
        self.quiet = quiet
        self.run_id = uuid.uuid4().hex[:12]

    def record(self, kind: str, body: str, label: str | None = None) -> None:
        body = body if isinstance(body, str) else json.dumps(body, default=str)
        log_event(self.db, self.agent, self.task_id, self.run_id, kind, body, label)
        if self.quiet:
            return
        head = f"[{self.agent}] {GLYPHS.get(kind, ' ')} {label or kind}"
        print(f"{head}  {one_line(body)}", file=sys.stderr if kind == "error" else sys.stdout, flush=True)

    def tool_call(self, name: str, args) -> None:
        self.record("tool_use", args, label=name)

    def tool_result(self, name: str, result, is_error: bool = False) -> None:
        self.record("error" if is_error else "tool_result", result, label=name)
