"""The shared agent tool set.

One definition, several consumers: the `mcp` subcommand (stdio, for Codex and
other external clients), the Claude daemon (in-process, via
create_sdk_mcp_server) and any generic MCP client. Handlers take the
connection and the calling agent's name, so no backend reimplements logic.

Who gets which tools is `toolset()`: an agent in config.toml gets its
flavor's set; any other caller - a human driving an MCP client, as in the
repo's .mcp.json - is an operator and gets every tool, with the authority
that implies (replying on any task, marking its own inbox read)."""

from __future__ import annotations

import json
from typing import Any

from peewee import SqliteDatabase

from .db import HUMAN, TASK_STATUSES
from .markdown import as_markdown
from .runtime import store_workflow_context
from .store import (
    add_dependency,
    add_task,
    ask_agent,
    docs_get,
    docs_list,
    docs_set,
    full_text_search,
    get_inbox,
    get_task,
    list_tags,
    list_tasks,
    reply,
    send_message,
    update_task,
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


def _create_task_handler(db, agent, args):
    """Handler for the create_task tool."""
    title = args["title"]
    description = args.get("description", "")
    assigned_to = args.get("assigned_to")
    tags = args.get("tags")
    depends_on = args.get("depends_on", [])

    # Create the task
    task_id = add_task(db, title, description=description, assigned_to=assigned_to, tags=tags)

    # Add dependencies if provided
    for dep_id in depends_on:
        add_dependency(db, task_id, dep_id)

    # Fetch and return the created task
    task = get_task(db, task_id)
    return task


# what an agent may say about its own task; every other transition (requeue,
# approve, merge) belongs to the human or the daemon
REPLY_STATUSES = ("done", "blocked", "needs_approval")


def _reply_handler(db, agent, args):
    """Handler for the reply tool: only on the caller's own in-progress task.

    Cost is not taken from the model - the daemon records the backend's real
    figures on this message when the run ends (see runtime.finish_task)."""
    task_id = args["task_id"]
    status = args.get("status", "done")
    if status not in REPLY_STATUSES:
        raise ValueError(f"status must be one of {', '.join(REPLY_STATUSES)}, not {status!r}")
    task = get_task(db, task_id)
    if not task or task["assigned_to"] != agent:
        raise ValueError(f"task {task_id} is not assigned to you; reply only on the task you were given")
    if task["status"] != "in_progress":
        raise ValueError(f"task {task_id} is already {task['status']}; you can reply on it only once per run")
    return _reply(db, agent, task_id, args, status)


def _reply(db, agent, task_id, args, status):
    if args.get("handover"):
        store_workflow_context(db, agent, task_id, args["handover"])
    return {"id": reply(db, agent, task_id, args["payload"], status=status)}


def _operator_reply_handler(db, agent, args):
    """An operator closes whichever task it means to, in any state."""
    status = args.get("status", "done")
    if status not in REPLY_STATUSES:
        raise ValueError(f"status must be one of {', '.join(REPLY_STATUSES)}, not {status!r}")
    if not get_task(db, args["task_id"]):
        raise ValueError(f"task {args['task_id']} not found")
    return _reply(db, agent, args["task_id"], args, status)


def _send_message_handler(db, agent, args):
    msg_type = args.get("msg_type", "question")
    out = {"id": send_message(db, agent, args["recipient"], args.get("task_id"), msg_type, args["payload"])}
    if msg_type in ("question", "blocker"):
        answer = ask_agent(db, agent, args["recipient"], args.get("task_id"), args["payload"])
        if answer is not None:
            out["answer_task"] = answer
            out["next"] = (
                f"{args['recipient']} will answer in task {answer}. reply(status='blocked') "
                f"and stop; task {args['task_id']} resumes with the answer."
            )
    return out


def _search_handler(db, agent, args):
    """Handler for the search tool."""
    query = args["query"]
    tables = args.get("tables")
    limit = args.get("limit", 20)

    # Clamp limit to valid range (1-100)
    limit = max(1, min(100, limit))

    # Call full_text_search
    results = full_text_search(db, query, tables=tables, limit=limit)

    return {
        "query": query,
        "count": len(results),
        "results": results,
    }


def _docs_set_handler(db, agent, args):
    """Handler for the docs_set tool. Only touches the task link if given.

    A doc a human last wrote is theirs: an agent overwriting it would replace
    the brief every other agent works from, with no history to recover it."""
    existing = next((d for d in docs_list(db) if d["key"] == args["key"]), None)
    if existing and existing["updated_by"] == HUMAN:
        raise ValueError(
            f"'{args['key']}' was written by the human and agents may not overwrite it. "
            "Write under a new key, or send_message the human with the change you propose."
        )
    return _operator_docs_set_handler(db, agent, args)


def _operator_docs_set_handler(db, agent, args):
    kwargs = {}
    if "task_id" in args:
        kwargs["task_id"] = args["task_id"]
    docs_set(db, args["key"], as_markdown(args["content"]), agent, **kwargs)
    return {"ok": True}


def _add_tag_handler(db, agent, args):
    """Handler for the add_tag tool."""
    task_id = args["task_id"]
    new_tags = args["tags"]

    task = get_task(db, task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}

    # Get existing tags
    existing_tags = set()
    if task.get("tags"):
        existing_tags = set(task["tags"].split(","))

    # Add new tags
    for tag in new_tags.split(","):
        tag = tag.strip().lower()
        if tag:
            existing_tags.add(tag)

    # Update task with combined tags
    combined_tags = ",".join(sorted(existing_tags)) if existing_tags else None
    update_task(db, task_id, tags=combined_tags)

    # Return updated task
    updated_task = get_task(db, task_id)
    return updated_task or {}


def _remove_tag_handler(db, agent, args):
    """Handler for the remove_tag tool."""
    task_id = args["task_id"]
    tags_to_remove = args["tags"]

    task = get_task(db, task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}

    # Get existing tags
    existing_tags = set()
    if task.get("tags"):
        existing_tags = set(task["tags"].split(","))

    # Remove tags
    for tag in tags_to_remove.split(","):
        tag = tag.strip().lower()
        existing_tags.discard(tag)

    # Update task with remaining tags
    combined_tags = ",".join(sorted(existing_tags)) if existing_tags else None
    update_task(db, task_id, tags=combined_tags)

    # Return updated task
    updated_task = get_task(db, task_id)
    return updated_task or {}


TOOL_SPECS: list[dict] = [
    {
        "name": "get_inbox",
        "description": (
            "Check for unread messages addressed to me. The ones that were waiting "
            "when this run started are already in your prompt; this also shows any "
            "that arrived since."
        ),
        "schema": _obj({}),
        # a daemon-run agent only peeks: the daemon marks what it delivered as
        # read once a run succeeds, so a failed run cannot swallow a message
        "handler": lambda db, agent, a: get_inbox(db, agent, mark_read=False),
    },
    {
        "name": "send_message",
        "description": (
            "Send a message to another agent or to 'human'. Never wait inline for "
            "an answer. To ask another agent, send msg_type 'question' with your "
            "task_id, then reply(status='blocked') and stop: they get a task to "
            "answer it, and your task resumes with their answer once they have."
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
        "handler": lambda db, agent, a: _send_message_handler(db, agent, a),
    },
    {
        "name": "reply",
        "description": (
            "Log the result of the task you are working on back to the human "
            "coordinator and close it. Only for your current task, once per run."
        ),
        "schema": _obj(
            {
                "task_id": {**_INT, "description": "the task you were given"},
                "payload": {**_STR, "description": "summary of what was done"},
                "handover": {
                    **_STR,
                    "description": (
                        "Markdown brief for whoever works on the tasks that depend on this one: "
                        "what you did, files changed, decisions and why, anything left open. "
                        "Without it they get your payload instead."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": list(REPLY_STATUSES),
                    "description": "task status to set (default 'done')",
                },
            },
            ["task_id", "payload"],
        ),
        "handler": lambda db, agent, a: _reply_handler(db, agent, a),
    },
    {
        "name": "docs_get",
        "description": (
            "Read a shared project doc by key (e.g. 'description', 'architecture'). "
            "Pass task_id for a doc that belongs to a task (e.g. a plan or handover "
            "report) to make sure you're reading the one linked to that task, not a "
            "same-named doc from somewhere else."
        ),
        "schema": _obj(
            {
                "key": _STR,
                "task_id": {**_INT, "description": "Optional: the doc must be linked to this task"},
            },
            ["key"],
        ),
        "handler": lambda db, agent, a: {
            "key": a["key"], "content": docs_get(db, a["key"], a.get("task_id")),
        },
    },
    {
        "name": "docs_set",
        "description": (
            "Write a shared project doc, as Markdown. Overwrites the whole value for "
            "that key. Docs are reports other agents and humans read: headings, prose "
            "and bullets - not a JSON dump (JSON content is rewritten into Markdown). "
            "Pass task_id to link this doc to a task (e.g. your plan for it) - it is "
            "then deleted along with the task, and visible to task_id-scoped lookups."
        ),
        "schema": _obj(
            {
                "key": _STR,
                "content": _STR,
                "task_id": {**_INT, "description": "Optional: link this doc to a task"},
            },
            ["key", "content"],
        ),
        "handler": lambda db, agent, a: _docs_set_handler(db, agent, a),
    },
    {
        "name": "docs_list",
        "description": "List shared project docs, with their content. Pass task_id to see only docs linked to that task.",
        "schema": _obj({"task_id": {**_INT, "description": "Optional: only docs linked to this task"}}),
        "handler": lambda db, agent, a: docs_list(db, a.get("task_id")),
    },
    {
        "name": "create_task",
        "description": "Create a new task and optionally set up dependencies. Returns the created task record.",
        "schema": _obj(
            {
                "title": {**_STR, "description": "Short task name"},
                "description": {**_STR, "description": "Optional longer explanation of what to do"},
                "assigned_to": {**_STR, "description": "Optional agent name to assign this task to"},
                "tags": {**_STR, "description": "Optional comma-separated tags for filtering (e.g. 'bug,urgent')"},
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
    {
        "name": "search",
        "description": "Search across all project knowledge: tasks, docs, messages, and activity logs. Returns results with table information so you can rank by source.",
        "schema": _obj(
            {
                "query": {
                    **_STR,
                    "description": "Search query. Supports FTS5 syntax: word1 word2 (AND), word1 OR word2, \"phrase search\", -word (NOT). Example: 'architecture' or '\"design pattern\" AND -deprecated'",
                },
                "tables": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["docs", "messages", "tasks", "events"],
                    },
                    "description": "Tables to search (default: all). Example: ['docs', 'tasks']",
                },
                "limit": {
                    **_INT,
                    "description": "Max results to return (default 20, max 100)",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            ["query"],
        ),
        "handler": lambda db, agent, a: _search_handler(db, agent, a),
    },
    {
        "name": "add_tag",
        "description": "Add one or more tags to a task. Tags are comma-separated strings used for filtering and grouping.",
        "schema": _obj(
            {
                "task_id": {**_INT, "description": "ID of the task to tag"},
                "tags": {**_STR, "description": "Comma-separated tags to add (e.g. 'bug,urgent')"},
            },
            ["task_id", "tags"],
        ),
        "handler": lambda db, agent, a: _add_tag_handler(db, agent, a),
    },
    {
        "name": "remove_tag",
        "description": "Remove one or more tags from a task.",
        "schema": _obj(
            {
                "task_id": {**_INT, "description": "ID of the task to remove tags from"},
                "tags": {**_STR, "description": "Comma-separated tags to remove (e.g. 'bug,urgent')"},
            },
            ["task_id", "tags"],
        ),
        "handler": lambda db, agent, a: _remove_tag_handler(db, agent, a),
    },
    {
        "name": "list_tags",
        "description": "List all tags currently used in the project.",
        "schema": _obj({}),
        "handler": lambda db, agent, a: {"tags": list_tags(db)},
    },
]


# every agent's tools; planners also curate tags. create_task is in the base
# set because a new task lands in `todo`, where a human decides whether it runs
BASE_TOOLS = (
    "get_inbox", "send_message", "reply", "docs_get", "docs_set", "docs_list",
    "create_task", "list_tasks", "search", "list_tags",
)
FLAVOR_TOOLS = {
    "dev": BASE_TOOLS,
    "reviewer": BASE_TOOLS,
    "planner": (*BASE_TOOLS, "add_tag", "remove_tag"),
}

# what changes for a caller acting as the human's hands rather than as an agent
OPERATOR_HANDLERS = {
    "reply": _operator_reply_handler,
    "get_inbox": lambda db, agent, a: get_inbox(db, agent),
    "docs_set": _operator_docs_set_handler,
}


def toolset(cfg: dict | None) -> list[dict]:
    """The tools one caller gets: `cfg` is its config.toml entry, or None for
    an operator - a caller that is not a configured agent."""
    if cfg is None:
        return [{**s, "handler": OPERATOR_HANDLERS.get(s["name"], s["handler"])} for s in TOOL_SPECS]
    names = FLAVOR_TOOLS.get(cfg.get("flavor") or "dev", BASE_TOOLS)
    return [s for s in TOOL_SPECS if s["name"] in names]


def call_tool(db: SqliteDatabase, agent_name: str, name: str, args: dict, specs: list[dict] | None = None) -> Any:
    """Run one tool for one caller. `specs` is that caller's toolset(); a
    tool outside it is refused exactly like one that does not exist."""
    for spec in TOOL_SPECS if specs is None else specs:
        if spec["name"] == name:
            return spec["handler"](db, agent_name, args or {})
    raise KeyError(f"unknown tool: {name}")


def tool_result_text(value: Any) -> str:
    return json.dumps(value, default=str)
