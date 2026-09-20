# Achka MCP Tools API Documentation

A comprehensive guide to the 10 MCP tools that enable agent-to-agent and agent-to-human coordination in the achka multi-agent system.

## Overview

The achka MCP tools are a shared API for agents to:
- **Claim and manage work**: atomically reserve tasks, report results
- **Communicate**: send messages to other agents or the human coordinator
- **Coordinate**: claim files to avoid conflicts, release them when done
- **Share knowledge**: read and write shared project documentation

All tools are:
- **Atomic**: Claims happen all-or-nothing via SQLite transactions
- **Cooperative**: File claims are advisory notifications, not locks
- **Audited**: Every call is logged with sender, timestamp, and cost
- **Available in-process**: Low latency for the Claude daemon and the web app

The tools are implemented in `src/achka/store.py` and exposed via `src/achka/tools.py` for three consumers:
- The Claude daemon (`daemons/claude.py`): in-process via `create_sdk_mcp_server()`
- The CLI MCP server (`mcp` subcommand): stdio protocol
- The web app: direct database access

---

## Task Lifecycle Tools

### claim_task

**Purpose:** Atomically claim the next task assigned to you that is ready to run.

**Parameters:** None

**Returns:** A task object with the following fields:
- `id` (int): Task ID
- `title` (str): Short task description
- `description` (str): Full task text
- `assigned_to` (str): Agent name it's assigned to (your name)
- `status` (str): Will be `"in_progress"`
- `created_at` (float): Unix timestamp
- `updated_at` (float): Unix timestamp
- Or `null` if no task is available right now

**Examples:**

```json
// Call
{}

// Response (when a task is available)
{
  "id": 42,
  "title": "Write documentation for MCP tools",
  "description": "Create docs/MCP_TOOLS.md with comprehensive examples...",
  "assigned_to": "dev-agent",
  "status": "in_progress",
  "created_at": 1726780800.123,
  "updated_at": 1726780800.456
}

// Response (when no task is available)
null
```

**Common patterns:**

- **Task polling loop**: In the daemon, repeatedly call `claim_task()` on a poll interval (2s default) until one is available, then execute it.
- **Dependency awareness**: `claim_task()` automatically skips tasks whose dependencies are not `"done"` yet, so you never run into unmet dependencies.
- **Status transitions**: The tool atomically moves a task from `"todo"` to `"in_progress"` in a single database transaction. If another agent claims it between your check and the update, you get `null` instead.

**Errors:**

- **No task available yet**: Returns `null`. This is not an error—just wait and poll again.
- **Task has unmet dependencies**: The task is skipped; you get the next runnable task. Dependencies in `"needs_approval"` or `"blocked"` states also hold this one back, not just incomplete ones.

**Related:** `reply` (to close the task when done), `send_message` (to ask for clarification), `get_inbox` (to check for notes about the task).

---

### reply

**Purpose:** Close a task by logging your result back to the human coordinator.

**Parameters:**
- `task_id` (int, required): The ID of the task you are closing
- `payload` (str, required): A human-readable summary of what you did or why you stopped
- `status` (str, optional, default="done"): New status for the task. One of:
  - `"done"` – task succeeded
  - `"blocked"` – you could not proceed; human intervention needed
  - `"needs_approval"` – task completed but awaits human sign-off before dependents can run
- `input_tokens` (int, optional): Tokens consumed reading context
- `output_tokens` (int, optional): Tokens generated in your response
- `cost_usd` (float, optional): Cost of this task in USD

**Returns:** A dict with:
- `id` (int): The message ID of the logged result

**Examples:**

```json
// Task succeeded
{
  "task_id": 42,
  "payload": "Documentation written with 9 tools documented, 3 workflow examples, and best practices section.",
  "status": "done",
  "input_tokens": 15000,
  "output_tokens": 8500,
  "cost_usd": 0.142
}

// Response
{
  "id": 1234
}

// Task blocked (ask human to intervene)
{
  "task_id": 43,
  "payload": "build-agent is holding src/components/Card.tsx (task 41: refactoring Button). Need clarification on API compatibility.",
  "status": "blocked"
}

// Task needs approval (wait before dependents run)
{
  "task_id": 44,
  "payload": "Schema migration written. Tested on staging. Awaiting approval before deployment.",
  "status": "needs_approval"
}
```

**Common patterns:**

- **Always reply when done**: Do not just return from your task handler. Call `reply()` to close the task and let the coordinator know the result.
- **Blocking pattern**: If you cannot proceed (file is claimed by another agent, need clarification), set `status="blocked"` with a message explaining why. The human can then decide whether to wait or intervene.
- **Approval gates**: Use `status="needs_approval"` to hold dependents until the human reviews your work.
- **Cost tracking**: Pass `input_tokens` and `output_tokens` so the coordinator can track spending per agent. The daemon adds these automatically if using the Claude SDK.

**Errors:**

- **Task not found**: Will raise an error if the task ID does not exist. Make sure you are replying to the task you actually claimed.
- **Task already closed**: If someone else closes the task before you reply, the update still succeeds (idempotent).

**Related:** `claim_task` (to pick up work), `send_message` (to ask before replying blocked).

---

## Messaging Tools

### send_message

**Purpose:** Send a message to another agent or to the human coordinator. Use this instead of waiting inline.

**Parameters:**
- `recipient` (str, required): Agent name (e.g., `"build-agent"`) or `"human"` for the coordinator
- `payload` (str, required): The message body (question, note, blocker summary, etc.)
- `msg_type` (str, optional, default="question"): Semantic type of the message:
  - `"question"` – asking for information
  - `"blocker"` – blocked, need help
  - `"result"` – reporting work done (used by `reply()`)
  - `"note"` – informational message
- `task_id` (int, optional): Task ID this message concerns, if any
- `input_tokens` (int, optional): Tokens used to compose the message
- `output_tokens` (int, optional): Tokens in the response
- `cost_usd` (float, optional): Cost of this interaction

**Returns:** A dict with:
- `id` (int): The message ID assigned to this message

**Examples:**

```json
// Ask another agent a question
{
  "recipient": "build-agent",
  "payload": "I need to know: does the Button API accept both 'primary' and 'secondary' variants? Trying to refactor CardHeader and don't want to break anything.",
  "msg_type": "question",
  "task_id": 42
}

// Response
{
  "id": 1001
}

// Report a blocker to the human
{
  "recipient": "human",
  "payload": "Task 43 blocked: src/store.ts is held by db-migration-agent (task 40, refactoring ORM). I cannot proceed until that finishes or is released.",
  "msg_type": "blocker",
  "task_id": 43
}

// Send a note
{
  "recipient": "integration-tester",
  "payload": "Task 45 done: I've updated the API responses for v2 endpoints. Ready for you to test integration when you get to task 46.",
  "msg_type": "note",
  "task_id": 45
}
```

**Common patterns:**

- **Block and ask**: When you hit an obstacle (file claimed, unclear spec), do not speculate. Send a message and then call `reply()` with `status="blocked"`. Stop.
- **Thread awareness**: Messages are threaded by `task_id`. If you reply to a task-related message, include the same `task_id` so the human can follow the conversation.
- **Check inbox after**: After sending a message, call `get_inbox()` to see if there's already an answer waiting.
- **Non-blocking questions**: You do not have to block your task to ask a question. Message someone, but keep working on what you can. If you hit a true obstacle, then block.

**Errors:**

- **Invalid recipient**: If you name an agent that does not exist, the message is still logged (the coordinator can see it), but the agent will not be notified until they register. Use `get_inbox()` first if unsure.
- **Message too large**: No practical size limit; messages are stored as text in the database.

**Related:** `get_inbox` (to check for replies), `reply` (task closing message), `claim_files` (often combined: "I need to edit X but it's claimed by Y").

---

### get_inbox

**Purpose:** Retrieve all new messages addressed to you. Marks them as read.

**Parameters:** None

**Returns:** A list of message objects (oldest first):
- `id` (int): Message ID
- `sender` (str): Agent name or `"human"`
- `recipient` (str): You (your agent name)
- `task_id` (int or null): Task this is about, if any
- `msg_type` (str): `"question"`, `"blocker"`, `"result"`, or `"note"`
- `payload` (str): The message body
- `ts` (float): Unix timestamp
- `read_at` (float or null): When you read it (set by this call)
- `input_tokens` (int): Tokens used
- `output_tokens` (int): Tokens in response
- `cost_usd` (float): Cost of this interaction

**Examples:**

```json
// Call
{}

// Response (you have 2 new messages)
[
  {
    "id": 1001,
    "sender": "build-agent",
    "recipient": "dev-agent",
    "task_id": 42,
    "msg_type": "question",
    "payload": "Can you clarify what format the config should be in? JSON or YAML?",
    "ts": 1726780900.123,
    "read_at": null,
    "input_tokens": 2500,
    "output_tokens": 150,
    "cost_usd": 0.042
  },
  {
    "id": 1002,
    "sender": "human",
    "recipient": "dev-agent",
    "task_id": 42,
    "msg_type": "result",
    "payload": "Task 41 done: Button refactoring complete. All tests pass.",
    "ts": 1726780950.456,
    "read_at": null,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": 0.0
  }
]

// Response (no new messages)
[]
```

**Common patterns:**

- **Check first thing**: Call `get_inbox()` at the start of every task. You might have an answer from an earlier question or a note about a dependency.
- **Thread by task_id**: Messages with the same `task_id` are a conversation thread. You can follow context by reading all messages for that task.
- **Automatic read**: This call marks all returned messages as read, so you will not see them again. If you need to refer back, save them yourself.
- **Polling loop**: In the daemon, you can call this between task attempts to stay responsive to blockers or clarifications.

**Errors:**

- **No new messages**: Returns an empty list. This is normal.
- **Old messages are not returned**: Once marked read, you must ask the human or grep the logs to see them again.

**Related:** `send_message` (to start a conversation), `reply` (to log results, which generates a message).

---

## File Coordination Tools

### claim_files

**Purpose:** Tell other agents which files you are about to edit. This is advisory, not a lock—it prevents accidental conflicts by making overlap visible.

**Parameters:**
- `paths` (list of str, required): Project-relative file paths or directories to claim. Multiple files or dirs in one call.
  - Directories count as everything under them (recursive).
  - Paths are normalized (`.` and `..` collapsed), so two agents naming the same file differently still collide.
- `note` (str, optional): What you are doing to these files (e.g., "refactoring Button component", "adding tests"). Shows up in other agents' error messages.

**Returns:** A dict with:
- `claimed` (list of str): The normalized paths you now hold
- `held_by_others` (list of dicts): Anyone else currently holding any of these paths. Each dict has:
  - `path` (str): Normalized path
  - `agent` (str): Who is holding it
  - `task_id` (int or null): Task they are working on
  - `note` (str or null): What they said they are doing
  - `mode` (str): `"write"` or `"read"`

**Examples:**

```json
// Claim one file
{
  "paths": ["src/components/Button.tsx"],
  "note": "Refactoring props interface"
}

// Response (no conflicts)
{
  "claimed": ["src/components/Button.tsx"],
  "held_by_others": []
}

// Claim a directory (recursive)
{
  "paths": ["src/api/"],
  "note": "Adding v3 endpoints"
}

// Response (someone is in there)
{
  "claimed": ["src/api"],
  "held_by_others": [
    {
      "path": "src/api/rest.ts",
      "agent": "api-agent",
      "task_id": 50,
      "note": "Fixing rate limiting",
      "mode": "write"
    }
  ]
}

// Claim multiple paths
{
  "paths": ["src/components/Button.tsx", "src/styles/Button.css", "tests/Button.test.tsx"],
  "note": "Styling refactor"
}

// Response (partial conflict)
{
  "claimed": ["src/components/Button.tsx", "src/styles/Button.css", "tests/Button.test.tsx"],
  "held_by_others": [
    {
      "path": "tests/Button.test.tsx",
      "agent": "test-agent",
      "task_id": 51,
      "note": "Adding snapshot tests",
      "mode": "write"
    }
  ]
}
```

**Common patterns:**

- **Always claim before editing**: Call `claim_files()` before you call `Write`, `Edit`, or similar tools. The Claude daemon does this automatically via a permission hook.
- **Check conflicts immediately**: If `held_by_others` is not empty, message that agent and ask what they are doing. Consider whether you can proceed (e.g., you are editing different sections).
- **Claim directories for scope**: If you are refactoring a whole component directory, claim the directory, not each file. It is clearer and prevents micro-conflicts.
- **Release early**: Call `release_files()` as soon as you are done with a file, so other agents are not blocked waiting for you to finish the whole task.
- **Daemon claims for you**: The Claude daemon intercepts `Write`, `Edit`, etc. and claims files on your behalf. You still see conflicts in the permission error message.

**Errors:**

- **Overlapping claims**: This is not an error—the system returns the conflict in `held_by_others`. You decide what to do: wait, ask, work around, or proceed if you are doing different things.
- **Dead claims**: If an agent stops heartbeating (crashes or disappears), its claims are automatically released after 180 seconds, so files are not held forever.
- **Path normalization**: Paths are normalized before checking overlap. `src/components/Button.tsx` and `./src/components/Button.tsx` are the same claim.

**Related:** `release_files` (to let go of claims early), `who_has` (to check a file without claiming it), `send_message` (to ask someone about a conflict).

---

### release_files

**Purpose:** Let go of file claims you made, allowing other agents to claim them. Automatic on task end; use this for early release.

**Parameters:**
- `paths` (list of str or null, optional): Files to release. If absent or `null`, releases everything you hold.

**Returns:** A dict with:
- `released` (int): Number of claims deleted

**Examples:**

```json
// Release one file
{
  "paths": ["src/components/Button.tsx"]
}

// Response
{
  "released": 1
}

// Release multiple files
{
  "paths": ["src/styles/Button.css", "tests/Button.test.tsx"]
}

// Response
{
  "released": 2
}

// Release everything
{}

// Response
{
  "released": 5
}
```

**Common patterns:**

- **Release early if possible**: If you claim a directory but finish with one file, release it. Do not hold it until the task ends.
- **Automatic cleanup**: The daemon releases all claims when your task ends, so you do not have to remember to call this. It is useful if you want to unblock a colleague sooner.
- **No error on missing path**: If you try to release a path you do not hold, it just returns 0. Safe to call multiple times.

**Errors:**

- **Releasing paths you do not hold**: Returns 0. Not an error.
- **Empty release**: Calling with an empty `paths` list (`[]`) releases nothing (different from `null`, which releases everything).

**Related:** `claim_files` (to claim), `who_has` (to check without claiming).

---

### who_has

**Purpose:** Check if anyone is holding a specific file or directory without claiming it yourself.

**Parameters:**
- `path` (str, required): Project-relative file or directory path to check

**Returns:** A list of active claims on that path or anything overlapping it:
- Each item has the same structure as `held_by_others` from `claim_files`:
  - `path` (str): Normalized path being held
  - `agent` (str): Agent holding it
  - `task_id` (int or null): Task ID
  - `note` (str or null): What they said they are doing
  - `mode` (str): `"write"` or `"read"`
- If no one is holding it, returns an empty list

**Examples:**

```json
// Check a file that is free
{
  "path": "src/components/Button.tsx"
}

// Response
[]

// Check a file someone is editing
{
  "path": "src/api/rest.ts"
}

// Response
[
  {
    "path": "src/api/rest.ts",
    "agent": "api-agent",
    "task_id": 50,
    "note": "Fixing rate limiting",
    "mode": "write"
  }
]

// Check a directory (shows all claims under it)
{
  "path": "src/api/"
}

// Response (multiple people in the directory)
[
  {
    "path": "src/api/rest.ts",
    "agent": "api-agent",
    "task_id": 50,
    "note": "Fixing rate limiting",
    "mode": "write"
  },
  {
    "path": "src/api/auth.ts",
    "agent": "auth-agent",
    "task_id": 48,
    "note": "Adding OAuth support",
    "mode": "write"
  }
]
```

**Common patterns:**

- **Check before claiming**: Use `who_has()` to scout a file before you decide to claim it. Gives you time to assess what others are doing.
- **Non-blocking check**: Unlike `claim_files()`, this does not reserve the file for you. It is a read-only query. Use it when you are exploring.
- **Directory check**: Checking a directory shows all overlapping claims under it, helping you understand the scope of other work.

**Errors:**

- **Dead claims excluded**: Claims from agents that have not heartbeat in 180 seconds are not returned.
- **Non-existent path**: Returns `[]`. Checking a path that does not exist yet (you are about to create it) is safe.

**Related:** `claim_files` (to reserve a file), `send_message` (if you want to ask someone about their work).

---

## Shared Knowledge Tools

### docs_get

**Purpose:** Read shared project documentation by key. Use this to retrieve knowledge that other agents or the human have written.

**Parameters:**
- `key` (str, required): The documentation key (e.g., `"architecture"`, `"api_overview"`, `"build_system"`). Keys are case-sensitive and conventionally lowercase with underscores.

**Returns:** A dict with:
- `key` (str): The key you requested
- `content` (str or null): The documentation text, or `null` if the key does not exist yet

**Examples:**

```json
// Get architecture docs
{
  "key": "architecture"
}

// Response (docs exist)
{
  "key": "architecture",
  "content": "# System Architecture\n\nThe achka system is organized into:\n\n1. **Store**: SQLite database for tasks, agents, messages, file claims\n2. **Tools**: MCP tool set for agent coordination\n3. **Daemon**: Thin Claude-backed agent runner\n4. **Web**: Task and message UI\n\n..."
}

// Response (key not found)
{
  "key": "unknown_key",
  "content": null
}
```

**Common patterns:**

- **Read before starting**: Call `docs_get()` with keys like `"architecture"`, `"code_overview"`, `"conventions"` to understand the project before making changes.
- **Build on existing docs**: If you update architecture or add new knowledge, call `docs_set()` to share it. Other agents will read it.
- **Naming convention**: Use snake_case for keys: `"database_schema"`, `"api_version_2"`, `"deployment_checklist"`.
- **Fallback to null**: Always check if `content` is `null` before using it. A missing doc is not an error.

**Errors:**

- **Key does not exist**: Returns `null` for `content`. Not an error—it just means the knowledge has not been written yet.
- **Encoding**: Docs are stored as UTF-8 text. Any encoding beyond that is your responsibility.

**Related:** `docs_set` (to write docs), task `description` field (for task-specific context).

---

### docs_set

**Purpose:** Write or update shared project documentation. Use this to record knowledge that other agents or future runs will need.

**Parameters:**
- `key` (str, required): The documentation key to write to
- `content` (str, required): The documentation text (replaces the whole value)

**Returns:** A dict with:
- `ok` (bool): Always `true` (success)

**Examples:**

```json
// Write architecture docs
{
  "key": "architecture",
  "content": "# System Architecture\n\nThe achka system is organized into:\n\n1. **Store**: SQLite database...\n2. **Tools**: MCP tool set...\n..."
}

// Response
{
  "ok": true
}

// Update with new knowledge
{
  "key": "build_system",
  "content": "# Build System\n\nBuilt with:\n- Poetry for dependency management\n- pytest for testing\n- Black for formatting\n\nRun:\n```bash\npoetry install\npoetry run pytest\n```"
}

// Response
{
  "ok": true
}
```

**Common patterns:**

- **Document as you learn**: If you discover something important (API gotchas, dependency constraints, architectural decisions), write it to docs so the next agent does not have to re-learn it.
- **Overwrite, not append**: `docs_set()` replaces the entire value for a key. If you want to add to existing docs, you must read it first with `docs_get()`, append, then write back.
- **Example: Building on existing docs**:
  ```json
  // 1. Read existing docs
  {"key": "api_gotchas"}
  // → returns {"key": "api_gotchas", "content": "...existing text..."}
  
  // 2. Append your finding
  // → content += "\n\n- New finding: ..."
  
  // 3. Write back
  {"key": "api_gotchas", "content": "...existing...\n\n- New finding..."}
  ```

**Errors:**

- **No size limit**: Docs can be as large as the database allows. No practical limit for most use cases.
- **Concurrent writes**: If two agents call `docs_set()` on the same key simultaneously, the last write wins (no merging). Avoid that by using task assignments or messaging.

**Related:** `docs_get` (to read), `send_message` (to tell someone you updated docs if it is important).

---

## Status Reporting Tool

### heartbeat

**Purpose:** Report your current status to the coordinator so it can track agent health and workload.

**Parameters:**
- `status` (str, required): Your current state. One of:
  - `"idle"` – waiting for a task
  - `"working"` – actively processing a task
  - `"offline"` – stopped or unreachable
- `task_id` (int, optional): The ID of the task you are currently working on, if `status="working"`

**Returns:** A dict with:
- `ok` (bool): Always `true`

**Examples:**

```json
// Report that you are idle
{
  "status": "idle"
}

// Response
{
  "ok": true
}

// Report that you are working on a task
{
  "status": "working",
  "task_id": 42
}

// Response
{
  "ok": true
}

// Report offline (maintenance or shutdown)
{
  "status": "offline"
}

// Response
{
  "ok": true
}
```

**Common patterns:**

- **Daemon sends automatically**: The Claude daemon sends heartbeats automatically on a regular interval (usually once per task). You rarely need to call this directly.
- **Stale claim cleanup**: If your agent does not heartbeat for 180 seconds, your file claims are automatically released, so other agents are not stuck waiting.
- **Status tracking**: The coordinator uses this to show agent state in the UI and to know which agents are alive for `who_has()` and `claim_files()` queries.

**Errors:**

- **None practical**: This call always succeeds. It is purely informational.

**Related:** No other tools depend on heartbeat; it is a side-channel status update.

---

## Workflow Examples

### Workflow 1: Asking Another Agent (Question Pattern)

You are working on a task but need clarification from a teammate.

```
1. Call send_message(recipient="build-agent", payload="...", msg_type="question", task_id=42)
   → Returns message ID 1001

2. Continue working on what you can.

3. Later, periodically call get_inbox()
   → Returns build-agent's answer when they reply

4. When done with task, call reply(task_id=42, payload="...", status="done")
   → Task closes
```

**Key point**: You do not have to block. Keep working until you hit a real blocker.

---

### Workflow 2: Blocked on a File (Conflict Resolution)

You need to edit a file but another agent is holding it.

```
1. Call claim_files(paths=["src/components/Button.tsx"], note="Refactoring props")
   → Returns claimed=["src/components/Button.tsx"], held_by_others=[{agent: "test-agent", task_id: 51, ...}]

2. Read the conflict: test-agent is working on task 51 in that file.

3. Call send_message(recipient="test-agent", payload="I need to refactor Button props. You are in Button.tsx (task 51). Can we coordinate?", task_id=42)
   → Message ID 1002

4. Call get_inbox() to wait for a reply

5. Either:
   a) Test-agent says "go ahead, I am in a different section" → proceed with edits
   b) Test-agent says "hold on, still editing" → call reply(task_id=42, status="blocked", payload="Waiting for test-agent to finish Button.tsx edits")
   c) Collaborate: test-agent says "edit first, I will rebase" → proceed, release when done

6. When done, call release_files(paths=["src/components/Button.tsx"])
   → Unblocks test-agent if they were waiting
```

**Key point**: Conflict is not failure—it is a communication moment. Ask first, then decide.

---

### Workflow 3: Handing Off Work with Approval Gate

You complete a task but it must be reviewed before dependent tasks run.

```
1. You complete the work (e.g., database migration)

2. Call reply(task_id=42, payload="Migration written and tested on staging. Ready for review.", status="needs_approval")
   → Task moves to "needs_approval" state

3. Any tasks depending on task 42 are now blocked (claim_task skips them)

4. Human reviews the work and either:
   a) Approves: human updates task to "done" in the UI or via the CLI
      → Dependent tasks become runnable
   b) Sends it back: human marks it "todo" and/or messages you with feedback
      → You claim it again and redo

5. If human approves, dependent tasks can now claim
```

**Key point**: Use `"needs_approval"` to hold dependents until the human verifies something critical.

---

## File Claiming Best Practices

### When to claim:
- Before you call `Write`, `Edit`, or any tool that changes a file
- Before you start a refactoring that touches multiple files
- When you know you will iterate (tests, code review cycles)

### When not to claim:
- When you are only reading files (no write claim needed; read claims exist but are not enforced)
- When the file is new and no one else is working on it (though a claim is harmless)
- Temporary files or build artifacts (not worth the overhead)

### Claim scope:
- **Single file**: `claim_files(["src/components/Button.tsx"], note="Refactoring")` when you are focused
- **Directory**: `claim_files(["src/components/"], note="Styling refactor")` when you are touching many files
- **Multiple related files**: `claim_files(["src/api.ts", "tests/api.test.ts", "docs/api.md"])` when they go together

### Release strategy:
- **Release as soon as done**: Do not hold claims longer than necessary
- **Release per-file**: If you claim three files but finish with one, release it: `release_files(["src/api.ts"])`
- **Automatic cleanup**: Everything is released when your task ends anyway

### Conflict resolution:
- **Check `held_by_others` first**: If non-empty, message that agent
- **Estimate timing**: Ask them "will you be done in 5 minutes?" to decide whether to wait or pivot
- **Respect read-only claims**: Read claims do not block writes (they are informational). Only `mode="write"` claims matter for conflicts
- **Break ties**: If both agents are stuck waiting for each other, one must back off. Message the human if needed

---

## Shared Docs Conventions

### Key naming:
- Use `snake_case`: `"api_overview"`, `"deployment_checklist"`, `"build_system"`
- Group related docs: `"database_schema"`, `"database_migrations"`
- Use simple names: avoid prefixes unless there is naming ambiguity

### Common keys used in achka:
- `"architecture"` – System design and module structure
- `"code_overview"` – High-level code tour (for new agents)
- `"conventions"` – Code style, naming, patterns
- `"deployment_checklist"` – Steps before shipping
- `"known_issues"` – Gotchas and workarounds
- `"api_gotchas"` – API quirks and surprises
- Custom keys per project: `"build_system"`, `"testing_guide"`, etc.

### Update strategy:
- **Treat docs as evolving**: If you learn something, update the relevant doc
- **Atomic updates**: Read + edit + write in one logical step so updates do not race
- **Document as you go**: Do not defer docs to the end of a task. Write while the insight is fresh.

### Example: Documenting a API change:
```
1. docs_get("api_gotchas")
   → {"key": "api_gotchas", "content": "...existing gotchas..."}

2. Append your finding:
   content += "\n\n### Form field validation\nThe `/api/v2/forms` endpoint rejects fields with > 100 chars, but the error message is cryptic (returns 400 without details). Always validate client-side first."

3. docs_set("api_gotchas", content)
   → {"ok": true}

4. Other agents read this and avoid the trap.
```

---

## Cost and Token Accounting

Every `send_message` and `reply` call can include token and cost information:

```json
{
  "task_id": 42,
  "payload": "...",
  "input_tokens": 15000,
  "output_tokens": 8500,
  "cost_usd": 0.142
}
```

### What to track:
- **input_tokens**: Context you read to understand the task (prior conversations, docs, code files)
- **output_tokens**: Tokens you generated in your response and reasoning
- **cost_usd**: The actual cost to run this agent (e.g., for Claude, use the pricing in your model)

### Where it goes:
- Stored in the `messages` table with each message
- Aggregated in the coordinator UI under "Cost Ledger"
- Useful for understanding which agents are expensive and optimizing

### The daemon calculates it:
- If you use the Claude SDK (the daemon), token counts are added automatically
- If you are calling these tools directly (custom client, tests), you can pass `0` and update later if needed

### Example: Rough calculation for a task:
```
- Input: Read 5 docs (2K tokens each) + code context (5K tokens) = 15K
- Thought + reasoning: 3K tokens
- Output generation: 5.5K tokens
- Total: 23.5K tokens
- Cost (Claude): 23.5K * ($0.008/1M input + $0.024/1M output) ≈ $0.21
```

Log this when you reply to give the coordinator visibility.

---

## Summary

| Tool | Purpose | Blocks? |
|------|---------|---------|
| `claim_task` | Get your next task | No—returns null if none ready |
| `reply` | Close a task with a result | No—but marks task done/blocked/needs_approval |
| `send_message` | Message another agent or human | No—fire-and-forget |
| `get_inbox` | Check for replies to your messages | No—non-blocking read |
| `claim_files` | Claim files you are about to edit | No—advisory; returns conflicts, not lock |
| `release_files` | Let go of claimed files | No—always succeeds |
| `who_has` | Check who is holding a file | No—read-only query |
| `docs_get` | Read shared project documentation | No—returns null if missing |
| `docs_set` | Write shared project documentation | No—overwrites the key |
| `heartbeat` | Report your status | No—informational |

**Recommended task loop:**
```
1. claim_task() → get next task
2. get_inbox() → check for messages about this task
3. claim_files() → declare what you will edit
4. [do your work]
5. release_files() → let go early if possible
6. reply() → close the task
7. Loop back to step 1
```

**Non-blocking pattern for obstacles:**
```
1. Hit a blocker (need info, file is claimed, etc.)
2. send_message() → ask for help
3. get_inbox() → check for reply (optional; you can work on something else)
4. If truly blocked, reply(status="blocked") and stop
5. Otherwise, keep working
```
