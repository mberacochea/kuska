"""The shared agent tool set.

One definition, several consumers: the `mcp` subcommand (stdio, for Codex and
other external clients), the Claude daemon (in-process, via
create_sdk_mcp_server) and any generic MCP client. Handlers take the
connection and the calling agent's name, so no backend reimplements logic."""

from __future__ import annotations

import json
from typing import Any

from peewee import SqliteDatabase

from .db import AGENT_STATUSES, TASK_STATUSES
from .store import (
    add_dependency,
    add_task,
    claim_files,
    claim_holders,
    claim_task,
    docs_get,
    docs_list,
    docs_set,
    get_inbox,
    get_task,
    heartbeat,
    list_tasks,
    release_files,
    reply,
    send_message,
)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}


def _create_task_handler(db, agent, args):
    """Handler for the create_task tool."""
    title = args["title"]
    description = args.get("description", "")
    assigned_to = args.get("assigned_to")
    depends_on = args.get("depends_on", [])

    # Create the task
    task_id = add_task(db, title, description=description, assigned_to=assigned_to)

    # Add dependencies if provided
    for dep_id in depends_on:
        add_dependency(db, task_id, dep_id)

    # Fetch and return the created task
    task = get_task(db, task_id)
    return task


TOOL_SPECS: list[dict] = [
    {
        "name": "get_inbox",
        "description": "Check for new messages addressed to me. Marks them read.",
        "schema": _obj({}),
        "handler": lambda db, agent, a: get_inbox(db, agent),
    },
    {
        "name": "send_message",
        "description": (
            "Send a message to another agent or to 'human'. Use this instead of "
            "waiting inline: send, mark your task blocked, and stop."
        ),
        "schema": _obj(
            {
                "recipient": {**_STR, "description": "agent name, or 'human'"},
                "payload": {**_STR, "description": "the message body"},
                "msg_type": {
                    "type": "string",
                    "enum": ["question", "blocker", "result", "note"],
                    "description": "kind of message",
                },
                "task_id": {**_INT, "description": "task this is about, if any"},
            },
            ["recipient", "payload"],
        ),
        "handler": lambda db, agent, a: {
            "id": send_message(
                db, agent, a["recipient"], a.get("task_id"),
                a.get("msg_type", "question"), a["payload"],
            )
        },
    },
    {
        "name": "claim_task",
        "description": "Claim the next task assigned to me, or null if there is none.",
        "schema": _obj({}),
        "handler": lambda db, agent, a: claim_task(db, agent),
    },
    {
        "name": "reply",
        "description": "Log the result of a task back to the human coordinator and close it.",
        "schema": _obj(
            {
                "task_id": _INT,
                "payload": {**_STR, "description": "summary of what was done"},
                "input_tokens": _INT,
                "output_tokens": _INT,
                "cost_usd": _NUM,
                "status": {
                    "type": "string",
                    "enum": list(TASK_STATUSES),
                    "description": "task status to set (default 'done')",
                },
            },
            ["task_id", "payload"],
        ),
        "handler": lambda db, agent, a: {
            "id": reply(
                db, agent, a["task_id"], a["payload"],
                input_tokens=a.get("input_tokens", 0),
                output_tokens=a.get("output_tokens", 0),
                cost_usd=a.get("cost_usd", 0.0),
                status=a.get("status", "done"),
            )
        },
    },
    {
        "name": "docs_get",
        "description": "Read a shared project doc by key (e.g. 'description', 'architecture').",
        "schema": _obj({"key": _STR}, ["key"]),
        "handler": lambda db, agent, a: {"key": a["key"], "content": docs_get(db, a["key"])},
    },
    {
        "name": "docs_set",
        "description": "Write a shared project doc. Overwrites the whole value for that key.",
        "schema": _obj({"key": _STR, "content": _STR}, ["key", "content"]),
        "handler": lambda db, agent, a: (
            docs_set(db, a["key"], a["content"], agent) or {"ok": True}
        ),
    },
    {
        "name": "docs_list",
        "description": "List all shared project docs, with their content.",
        "schema": _obj({}),
        "handler": lambda db, agent, a: docs_list(db),
    },
    {
        "name": "claim_files",
        "description": (
            "Say which files you are about to change, before you change them. "
            "Directories count as everything under them. Returns who else is "
            "already holding any of them - if somebody is, message them rather "
            "than editing on top of their work."
        ),
        "schema": _obj(
            {
                "paths": {
                    "type": "array",
                    "items": _STR,
                    "description": "project-relative paths or directories",
                },
                "note": {**_STR, "description": "what you are doing to them"},
            },
            ["paths"],
        ),
        "handler": lambda db, agent, a: claim_files(
            db, agent, a["paths"], note=a.get("note")
        ),
    },
    {
        "name": "release_files",
        "description": (
            "Let go of files you claimed, once you are done with them. Leaving "
            "them claimed only blocks your colleagues; everything you hold is "
            "released anyway when this run ends."
        ),
        "schema": _obj({"paths": {"type": "array", "items": _STR}}),
        "handler": lambda db, agent, a: {
            "released": release_files(db, agent, a.get("paths"))
        },
    },
    {
        "name": "who_has",
        "description": "Who is holding a file or directory right now, if anyone.",
        "schema": _obj({"path": _STR}, ["path"]),
        "handler": lambda db, agent, a: claim_holders(db, a["path"], agent=agent),
    },
    {
        "name": "heartbeat",
        "description": "Report my status: 'idle', 'working' or 'offline'.",
        "schema": _obj(
            {
                "status": {"type": "string", "enum": list(AGENT_STATUSES)},
                "task_id": _INT,
            },
            ["status"],
        ),
        "handler": lambda db, agent, a: (
            heartbeat(db, agent, a["status"], a.get("task_id")) or {"ok": True}
        ),
    },
    {
        "name": "create_task",
        "description": "Create a new task and optionally set up dependencies. Returns the created task record.",
        "schema": _obj(
            {
                "title": {**_STR, "description": "Short task name"},
                "description": {**_STR, "description": "Optional longer explanation of what to do"},
                "assigned_to": {**_STR, "description": "Optional agent name to assign this task to"},
                "depends_on": {
                    "type": "array",
                    "items": _INT,
                    "description": "Optional list of task IDs this task depends on"
                },
            },
            ["title"],
        ),
        "handler": lambda db, agent, a: _create_task_handler(db, agent, a),
    },
    {
        "name": "list_tasks",
        "description": "List all tasks, optionally filtered by status.",
        "schema": _obj(
            {
                "status": {
                    "type": "string",
                    "enum": list(TASK_STATUSES),
                    "description": "Optional status filter",
                },
            }
        ),
        "handler": lambda db, agent, a: list_tasks(db, status=a.get("status")),
    },
]


def call_tool(db: SqliteDatabase, agent_name: str, name: str, args: dict) -> Any:
    for spec in TOOL_SPECS:
        if spec["name"] == name:
            return spec["handler"](db, agent_name, args or {})
    raise KeyError(f"unknown tool: {name}")


def tool_result_text(value: Any) -> str:
    return json.dumps(value, default=str)
