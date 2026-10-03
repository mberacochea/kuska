# Kuska agent tools

The tools agents use to coordinate with each other and with the human:
reporting results, asking questions, and sharing project knowledge. They are
defined once, in `src/kuska/tools.py` (`TOOL_SPECS`), and served three ways:

- **Claude daemon** (`daemons/claude.py`): in-process, via `create_sdk_mcp_server()`.
- **`kuska mcp --agent <name>`**: a stdio MCP server, used by the codex and
  openai daemons and by any MCP client.
- Handlers call `src/kuska/store/`, so every path writes the same records.

Claiming tasks, heartbeats and cost accounting are not tools: the daemon
owns them (see `daemons/loop.py`).

## Who gets which tools

`toolset(cfg)` decides, from the caller's entry in `.agents/config.toml`:

| Caller | Tools |
|---|---|
| `dev` or `reviewer` flavor (or no flavor) | the base set: `get_inbox`, `send_message`, `reply`, `docs_get`, `docs_set`, `docs_list`, `create_task`, `list_tasks`, `search`, `list_tags` |
| `planner` flavor | the base set, plus `add_tag` and `remove_tag` |
| not in config.toml (an **operator**) | every tool, with a human's authority - see below |

Calling a tool outside your set fails exactly like calling one that does not
exist.

An **operator** is a person driving an MCP client, like the repo's
`.mcp.json` (`kuska mcp --agent claude`). An operator can `reply` on any task
in any state, and can overwrite a human's docs. Its `get_inbox` also marks
messages read, because no daemon will do that for it.

## Tools

### `reply(task_id, payload, status="done", handover=None)`

Close the task you were given and report back to the human.

- `task_id` must be assigned to you and `in_progress`, and you can reply once
  per run. The first reply moves the task on, so a second one is refused.
- `status` is one of:
  - `done`: finished. A task that ran in its own worktree lands in
    `ready_to_merge` instead, and its dependents wait until a human merges it.
  - `blocked`: you cannot go on. If you asked another agent a question
    (see `send_message`), the task goes back to `ready` and runs again once
    the answer is in.
  - `needs_approval`: done, but a human should sign off before anything
    that depends on it runs.
- `handover` is a Markdown report for whoever works on the tasks that depend
  on this one: what you did, the files you changed, your decisions and why,
  and anything left open. Without one, they get your `payload` instead.

Token and cost figures are not parameters. The daemon records the backend's
own numbers on this message when the run ends.

You don't have to call `reply`: if a run ends without one, the daemon logs
your final message as the result, with status `done`.

### `send_message(recipient, payload, msg_type="question", task_id=None)`

Send a message to another agent or to `human`. `msg_type` is one of
`question`, `blocker`, `result` or `note`.

**Asking another agent.** A `question` or `blocker` sent to another agent,
with your own task's `task_id`:

1. creates an **answer task** for them (tagged `answer`, status `ready`),
   which their daemon picks up;
2. makes your task depend on it.

Then `reply(status="blocked")` and stop. Your task runs again once the answer
task is done, and the answer is in your prompt as context from that task.
The result tells you the answer task's id.

To stop agents from bouncing questions back and forth unattended, an answer
task cannot spawn another one: a question sent from an answer task stays a
plain message.

**Asking the human.** Message `human` and reply `blocked`. When the human
replies on the task in the web UI, it goes back to `ready`.

### `get_inbox()`

Your unread messages. Messages that were already waiting when your run
started are in your prompt under "New messages for you", so call this only
to see what has arrived since. It does not mark messages read: the daemon
does that once a run succeeds, so a failed run can't swallow a message.

### `docs_get(key, task_id=None)`, `docs_set(key, content, task_id=None)`, `docs_list(task_id=None)`

Shared project knowledge, such as `architecture`, `conventions` or `plan`.
Docs are Markdown reports: content sent as JSON is rewritten into Markdown
sections.

- `task_id` on `docs_set` links the doc to a task: the doc is deleted with
  that task, and `docs_get` and `docs_list` can be scoped to it.
- **A doc a human wrote is read-only to agents.** Write under a new key, or
  message the human with the change you propose.
- `docs_set` overwrites the whole value. To add to a doc, read it, edit it,
  then write it back.

### `create_task(title, description="", assigned_to=None, tags=None, depends_on=[])`

File new work. It lands in `todo`, a waiting list, and a human moves it to
`ready` before any agent picks it up.

### `list_tasks(status=None)`, `list_tags()`

Read the task list (optionally filtered by status) and the tags in use.

### `search(query, tables=None, limit=20)`

Full-text search (SQLite FTS5) over `tasks`, `docs`, `messages` and
`events`. It supports `a b` (AND), `a OR b`, `"exact phrase"` and `-word`.
`limit` is 1-100.

### `add_tag(task_id, tags)`, `remove_tag(task_id, tags)` (planners)

Add or remove comma-separated tags on a task.

## Workflows

**Ask, block, resume.** dev-agent needs a decision from planning-agent while
working on task 12:

```
send_message("planning-agent", "Postgres or SQLite for storage?", msg_type="question", task_id=12)
  -> {"id": 301, "answer_task": 13, "next": "planning-agent will answer in task 13 ..."}
reply(12, "Asked planning-agent which database to use.", status="blocked")
```

planning-agent's daemon runs task 13, and its reply is the answer. Task 12
then runs again, with `## Context from planning-agent (task 13: ...)` in its
prompt.

**Hand off through dependencies.** planning-agent breaks the work into tasks
with `create_task(..., depends_on=[...])`. Each one finishes with
`reply(..., handover="## Summary ...")`, and every task that depends on it
starts with that handover in its prompt.

**Gate on a human.** `reply(task_id, "...", status="needs_approval")` holds
everything that depends on the task until a human approves it or sends it
back. Unrelated tasks keep running.
