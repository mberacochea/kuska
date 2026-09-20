# Multi-Agent Coordination System

2026-09-19 - @Someone

A lightweight, SQLite-backed system for coordinating multiple coding, benchmarking and testing agents across parallel projects, with a human coordinator assigning work and a web UI for planning, status, and markdown export.

## Design principles

- **Less code is better.** One shared core module; every integration is a thin adapter around it, not a reimplementation.
- **Simple over complex.** SQLite instead of a message broker; polling instead of push; plain functions instead of a framework.
- **Visibility by default.** Who's running, what they're doing, and token/cost spend should fall out of data already being logged, not require a separate telemetry system.
- **The web app is where humans write, not just watch.** Tasks and the project plan are authored there; agents read them, they don't originate there.
- **Everything exportable to Markdown.** Plan, tasks, and message threads all export to git-friendly files on demand.
- **Minimal dependencies, managed with `uv`.**
- **Prompts are project artifacts.** Per-agent prompt files live in the repo, versioned like code.

## Architecture decisions

| Question | Decision |
| --- | --- |
| Agent lifecycle | Mix: thin long-running daemons per agent, but each task is handled in a **fresh subprocess/context** so conversation state never accumulates or goes stale |
| Project scope | **One SQLite DB per project** (no `project_id` column) — running several projects in parallel is just several DB files with independent daemons |
| Coordinator | **The human**, via the web UI or CLI — no LLM-based delegation logic to build |
| Task claiming | Coordinator assigns a task to a named agent explicitly; the agent's daemon picks it up on its next poll |
| Status/cost visibility | Derived from the `messages` table (already logs tokens/cost per turn) — no separate telemetry system |
| Docs (shared project knowledge) | SQLite is the source of truth; auto-exported to Markdown for humans and git |
| Status viewer | Local web app (not TUI or desktop) — needs to work when deployed on a VM |
| Access control on viewer | None needed — VM is assumed network-isolated (VPN/firewall) |
| Inter-agent "skill" | Delivered as **MCP tools** backed by the shared core module, not a CLI or hand-written instructions file |

## Project layout

```
myproject/
  .agents/
    project.db          # SQLite - source of truth for this project
    prompts/
      dev-agent.md
      codex-1.md
      bench-agent.md
    config.toml          # agent registry: name, backend, model, role
  .agents-export/        # generated on demand, git-friendly
    plan.md
    tasks.md
    messages/
      task-42.md
```

A `myproject/` with a `.agents/` directory is a project. Running several projects in parallel is just running the daemons and web app against several such directories.

## Database schema

```sql
CREATE TABLE agents (
    name TEXT PRIMARY KEY,
    backend TEXT,                    -- 'claude' | 'codex' | 'openai' | 'local'
    role TEXT,
    status TEXT DEFAULT 'offline',   -- 'idle' | 'working' | 'offline'
    current_task_id INTEGER,
    last_heartbeat REAL
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT,
    assigned_to TEXT REFERENCES agents(name),
    status TEXT DEFAULT 'todo',      -- 'todo' | 'in_progress' | 'blocked' | 'done'
    created_at REAL,
    updated_at REAL
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL DEFAULT (unixepoch('subsec')),
    sender TEXT,
    recipient TEXT,
    task_id INTEGER,
    msg_type TEXT,                   -- 'result' | 'question' | 'blocker'
    payload TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL
);

CREATE TABLE docs (
    key TEXT PRIMARY KEY,            -- 'plan', 'architecture', 'decisions'
    content TEXT,
    updated_by TEXT,
    updated_at REAL
);

CREATE INDEX idx_recipient_status ON messages(recipient, task_id);
```

`messages` doubles as the audit log and the cost ledger - no separate usage table. Status and cost views are plain `SELECT`/`GROUP BY` queries against it (see Web app section).

## Core: one file, Fossil-style (`agentctl.py`)

Everything that isn't "an agent talking to a model" lives in a single file - schema, task/message/doc functions, the web UI, and the MCP tool exposure - selected by subcommand, not by which file you're editing:

```
python agentctl.py init      # create project.db, run schema
python agentctl.py serve     # Flask + HTMX web UI: project and agents pages
python agentctl.py mcp       # MCP stdio server, for Codex / other external clients
python agentctl.py export    # one-off markdown export
```

The function-level shape:

```python
def connect(db_path) -> sqlite3.Connection: ...
def init_db(conn) -> None: ...

def register_agent(conn, name, backend, role) -> None: ...
def heartbeat(conn, name, status, task_id=None) -> None: ...
def list_agents(conn) -> list[dict]: ...

def add_task(conn, title, description, assigned_to=None) -> int: ...
def update_task_status(conn, task_id, status) -> None: ...
def list_tasks(conn, status=None) -> list[dict]: ...

def wait_for_task(conn, agent_name, poll_interval=2) -> dict: ...
def reply(conn, agent_name, task_id, payload, input_tokens=0, output_tokens=0, cost_usd=0) -> None: ...
def send_message(conn, sender, recipient, task_id, msg_type, payload) -> None: ...
def get_inbox(conn, agent_name) -> list[dict]: ...

def read_prompt(project_dir, agent_name) -> str: ...   # .agents/prompts/<name>.md
def write_prompt(project_dir, agent_name, content) -> None: ...

def docs_get(conn, key) -> str | None: ...
def docs_set(conn, key, content, updated_by) -> None: ...

def token_usage_by_agent(conn) -> list[dict]: ...
def export_markdown(conn, out_dir) -> None: ...
```

Claude's daemon still loads these in-process via `create_sdk_mcp_server()`; the standalone MCP server for Codex and other external clients is now `python agentctl.py mcp` rather than a separate module. The per-backend daemons (`daemon_claude.py`, `daemon_codex.py`) stay outside this file - a different kind of code (async SDK calls, retries, token accounting) than the state-tracking this file owns.

## Wiring agents to the core, via MCP

One shared MCP tool set, wrapping `core.py`: `send_message`, `get_inbox`, `claim_task`, `reply`, `docs_get`, `docs_set`, `heartbeat`. Each backend consumes it differently - no per-backend reimplementation of the logic.

### Claude (Claude Agent SDK)

Define the tools with `@tool` and expose them via `create_sdk_mcp_server()`, passed straight into `ClaudeAgentOptions.mcp_servers`. Runs **in-process** - no extra server, no subprocess.

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions

@tool("get_inbox", "Check for new messages addressed to me", {})
async def get_inbox(args):
    rows = core.get_inbox(conn, AGENT_NAME)
    return {"content": [{"type": "text", "text": json.dumps(rows)}]}

# ... send_message, claim_task, reply, docs_get, docs_set defined the same way

agentctl_server = create_sdk_mcp_server(name="agentctl", tools=[get_inbox, send_message, claim_task, reply, docs_get, docs_set])

options = ClaudeAgentOptions(
    mcp_servers={"agentctl": agentctl_server},
    allowed_tools=["mcp__agentctl__get_inbox", "mcp__agentctl__send_message", ...],
    system_prompt={"type": "file", "path": ".agents/prompts/dev-agent.md"},
)
```

### Codex (`openai-codex` Python SDK + Codex CLI's native MCP support)

Codex reads an `mcp_servers` table from its config and connects to **external** MCP servers over stdio - it has no in-process Python tool registration. Point it at the same `agentctl.py`, run in its `mcp` subcommand (using the `mcp` package for the stdio framing):

```python
# agentctl.py, `mcp` subcommand: exposes the same six tools as the Claude
# in-process server, via mcp.server.stdio, calling the same functions above
```

```toml
# .agents/config.toml (or ~/.codex/config.toml)
[mcp_servers.agentctl]
command = "python"
args = ["agentctl.py", "mcp", "--db", ".agents/project.db"]
```

Drive Codex itself via the SDK, which wraps the CLI:

```python
from codex_sdk import Codex
codex = Codex()
thread = codex.start_thread(mcp_servers=["agentctl"])
turn = await thread.run(task_prompt)
```

Note: `openai-codex` / `codex-sdk-py` is an early beta with no API stability guarantee as of writing - pin the exact version.

### OpenAI (API-driven) and open-weight models

No first-party agent runtime with MCP built in, unlike Claude/Codex. Wire a generic MCP client (the `mcp` package ships one) into whatever tool-calling loop drives the model, pointed at the same `agentctl.py mcp` process. More integration code than the other two backends, but no duplicated business logic - it is the same six tools and the same functions underneath.

## Per-agent prompts

Each agent's role/system prompt lives in `.agents/prompts/<name>.md` - plain text, versioned with the project, the single place agent "personality"/instructions live (not duplicated into `config.toml` or code).

- **Claude**: pass the path directly via `SystemPromptFile` (`{"type": "file", "path": ".agents/prompts/dev-agent.md"}`) - no loading code needed.
- **Codex / others**: the daemon reads the file itself and prepends its content to the task text sent to the agent.

## Daemon loop

One thin daemon process per agent. It never holds a running conversation - each task gets a fresh SDK call / thread, so context never accumulates or goes stale. Both SDKs return usage/cost on their result object, so logging tokens is a couple of extra lines, not new machinery.

```python
while True:
    task = core.wait_for_task(conn, AGENT_NAME)             # blocks, polls
    core.heartbeat(conn, AGENT_NAME, "working", task["id"])
    result = run_agent(AGENT_NAME, task, prompt_file)        # Claude query() or Codex thread.run()
    core.reply(
        conn, AGENT_NAME, task["id"], result.text,
        input_tokens=result.usage.input,
        output_tokens=result.usage.output,
        cost_usd=result.cost_usd,
    )
    core.heartbeat(conn, AGENT_NAME, "idle")
```

**Mid-task communication (asking another agent something):** to keep every invocation short-lived and stateless, an agent that needs input from another agent or from the human does not block and wait inline. It sends a message via the `send_message` MCP tool, marks its own task `blocked`, and exits. The recipient picks up the question on its next poll like any other task. When the reply lands, the coordinator (human, via the web UI) re-queues the original task; the re-invocation gets the original task plus the reply as context, in a fresh process. One extra hop, in exchange for every invocation staying stateless.

## Web UI (Flask + HTMX)

Flask, not FastAPI: the job here is "render an HTML fragment and send it back," not validate JSON against a schema or generate an OpenAPI spec - none of which HTMX needs. Flask is one dependency and bundles Jinja2, which is the right tool for rendering fragments. No npm, no build step, no bundler - HTMX loads from a CDN `<script>` tag.

Two pages, not three - status folds into the agents page, since it's the same `agents` table either way:

- **`/` - Project** - an editable description (`docs` table, key `description`; a textarea with `hx-post="/description" hx-trigger="blur"` saves without a separate save button) and the task list below it, with add/edit/reassign/delete as small `hx-post`/`hx-patch` swaps of just the affected row.
- **`/agents` - Agents** - one row per agent: name, backend, live status, current task, last heartbeat (this *is* the status view). Each row expands (`hx-get="/agents/<name>/context"`) into a textarea holding that agent's `.agents/prompts/<name>.md` content - edited and saved straight back to the file via `read_prompt`/`write_prompt`.
- **Export** - a button on the Project page calling `export_markdown()`.

Templates stay as string constants inside `agentctl.py` (via `render_template_string`) rather than a separate `templates/` folder, keeping the whole system readable top to bottom in one file. Binds to `0.0.0.0` (the VM is network-isolated) using Flask's built-in dev server - no gunicorn/uvicorn needed for an internal tool with a handful of users. Since several projects run in parallel, the app reads a small `~/.agentctl/projects.toml` registry to switch between project DBs rather than needing one server per project.

## CLI (process management only)

Task/plan authoring lives in the web app, not the CLI. The CLI's only job is starting and stopping processes, plus a manual export trigger:

```
python agentctl.py init             # create .agents/, project.db, default config.toml
python agentctl.py serve            # Flask + HTMX web UI (project + agents pages)
python agentctl.py export           # one-off markdown export
python daemon_claude.py <name>      # start one Claude-backed agent's daemon
python daemon_codex.py <name>       # start one Codex-backed agent's daemon
```

## Dependencies

```
uv add claude-agent-sdk codex-sdk-py mcp flask
```

Four packages. Jinja2 and a dev web server come bundled with Flask - no separate templating engine or WSGI server to add. HTMX loads from a CDN `<script>` tag, not a package. `sqlite3` is standard library. Pin `codex-sdk-py` (or `openai-codex`, depending on which package name is current when building) to an exact version - it is an early beta with no API stability guarantee.

## Suggested build order

1. **`agentctl.py` - schema + core functions** - everything else depends on this; test it standalone with a plain Python script before touching Flask or any agent SDK.
2. **`init` and `export` subcommands** - create `.agents/`, run the schema, write a default `config.toml`; get export working early since it exercises the same functions as everything else.
3. **`serve` - Project page** - task and description CRUD by hand, before any agent exists, so every later step is testable in isolation.
4. **`serve` - Agents page** - status view + the prompt editor (`read_prompt`/`write_prompt`).
5. **One Claude daemon** - simplest integration first (in-process MCP, no extra process). Prove the full loop: task added in the web UI -> daemon picks it up -> agent runs -> reply logged -> status page reflects it.
6. **`mcp` subcommand + one Codex daemon** - reuses the Claude daemon's shape, swaps the SDK call.
7. **OpenAI / open-weight adapter** - generic MCP client wired into a custom tool-calling loop, once the pattern is proven on the two backends with native support.
8. **Multi-project support** - `~/.agentctl/projects.toml` registry in the web app, and confirm daemons for different projects don't interfere (they shouldn't, since each owns a separate DB file).

Each step should be independently runnable and testable - nothing here requires the later steps to exist.
