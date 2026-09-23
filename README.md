# kuska

A lightweight, SQLite-backed system for coordinating multiple coding,
benchmarking and testing agents across parallel projects, with a human
coordinator assigning work and a web UI for planning, status and markdown
export.

*kuska* means "together" in [Quechua](https://en.wikipedia.org/wiki/Quechuan_languages).

Target audience = 1 (me :)).

This has been `vibe-coded`, I've read the code but have not written any of it (maybe a few lines).
It is an experiment, but if someone finds this useful please let me know.

## Quick start

**For development** (using [Taskfile](https://taskfile.dev/)):
```bash
uv sync
task init        # create .agents/, config.toml, database
task dev         # run web UI + MCP + all agents (or see docs/DEVELOPMENT.md for more)
```

**For single-binary / production:**
```bash
uv sync
uv run kuska init            # create .agents/
uv run kuska run-all         # run web server + MCP + agents
```

**Manual / step-by-step:**
```bash
uv run kuska init            # create .agents/ here
uv run kuska serve           # web UI on http://0.0.0.0:5055
uv run kuska daemon dev-agent    # run that agent (backend read from config.toml)
```

Everything an agent does is state in `.agents/project.db`; everything a human
writes is written in the web UI.

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for full development workflows and troubleshooting.

## Layout

```
Taskfile.yaml              # development task runner - see task -l
src/kuska/
  models.py      # peewee models - the schema, and the only place SQL is described
  db.py          # statuses, roles, connections
  store.py       # agents, tasks, dependencies, messages, docs, events
  tables.py      # per-model presentation for the generic row editor
  project.py     # .agents/ layout, prompts, config.toml, project registry
  runtime.py     # prompt composition, turn accounting, the agent monologue
  markdown.py    # rendering agent prose, with embedded HTML escaped
  tools.py       # the seven shared agent tools, defined once
  mcp_server.py  # those tools over stdio, for Codex and other external clients
  web.py         # Flask + HTMX routes
  runner.py      # run-all: web server + MCP + daemons in one command
  templates/     # Jinja templates, layout.html plus one file per page/fragment
  static/app.css # the whole stylesheet
  export.py      # markdown export
  cli.py         # init / serve / daemon / mcp / run-all / export
  daemons/
    claude.py    # Claude Agent SDK, tools registered in-process
    codex.py     # openai-codex SDK, tools over the stdio MCP server
packaging/entry.py + kuska.spec   # PyInstaller build
tests/           # plain scripts, no test framework
docs/DEVELOPMENT.md        # development guide and troubleshooting
```

State lives in SQLite through [peewee](https://github.com/coleifer/peewee):
`models.py` owns the schema, `store.py` wraps it in plain functions that
return plain dicts, and nothing outside those two files knows an ORM is
involved. Models are bound to a database per call, so one process can hold
several projects open - which is what the web app's project switcher needs.

A project is any directory with an `.agents/` subdirectory:

```
myproject/
  .agents/
    project.db        # SQLite - source of truth for this project
    config.toml       # agent registry: name, backend, model, role
    prompts/dev-agent.md
  .agents-export/     # generated on demand, git-friendly
```

Running several projects in parallel is several such directories, each with
its own DB file and its own daemons. `kuska init` records each one in
`~/.kuska/projects.toml`, and the web UI's header switches between them.

## Seeing what an agent is doing

A daemon narrates its agent's work as it happens: every piece of text, every
piece of reasoning, every tool call and its result.

```
[dev-agent] ▸ prompt       Task 12: Add the parser  handle nested quotes
[dev-agent] · thinking     the tokenizer needs a state machine for quotes
[dev-agent] ⚙ Read         {"file_path": "src/parser.py"}
[dev-agent] ← Read         def parse(text): ...
[dev-agent] ▪ text         Added a quote-aware tokenizer.
[dev-agent] ✔ done - $0.0300, 1200/340 tok
```

The terminal gets one truncated line per event; the full text goes to the
`events` table, keyed by a per-invocation `run_id`, so a run can be audited
long after it scrolled past. This is the agent's monologue, kept separate from
`messages`, which stays what agents and humans say to each other.

You can read it back three ways: the **Live activity** tail on the agents
page, the **Activity** section inside a task's detail panel (every event
expandable to its full body), and the markdown export, which writes each run
into the task's file. `kuska daemon <name> --quiet` keeps the logging but
stops the narration on the terminal.

## Waiting for approval

A task can be put on hold with the `needs_approval` status - by a human in the
web UI, or by an agent finishing its turn with `reply(status="needs_approval")`
when the work needs sign-off.

A held task does not run, and neither does anything that depends on it. That
is what task dependencies are for: a task is claimable only once every task it
depends on is `done`, so one task waiting for approval freezes its own branch
of the work while everything unrelated keeps running. Dependencies are edited
in a task's detail panel, refuse cycles and self-loops, and are shown in the
task list as a "waiting on" chip.

Resolving a hold is two buttons: **approve** (mark it done, releasing whatever
waited on it) or **send back** (re-queue it for the agent).

## The loop

1. A human adds a task in the web UI and assigns it to an agent.
2. That agent's daemon claims it on its next poll (`UPDATE ... RETURNING`, so
   two daemons can never take the same task).
3. The daemon runs **one fresh invocation** - no conversation is kept between
   tasks, so context never accumulates or goes stale. What the agent needs to
   know (description, earlier turns, answers to its questions) is composed
   into the prompt by `compose_task_prompt`.
4. The result and the turn's tokens/cost are written to `messages`, which
   doubles as the audit log and the cost ledger. The agents page reads its
   status and spend straight out of it.

An agent that needs something from another agent sends a message, marks its
task `blocked` and exits, rather than waiting inline. When the answer lands,
the human re-queues the task from the web UI and the next run gets the reply
as context.

## Two agents, one repository

Agents working different tasks in the same checkout will eventually reach for
the same file. The answer here is cooperation rather than locking: an agent
says what it is touching, and everyone else can see it.

- `claim_files(paths, note)` records "I am working on these". Directories
  cover everything under them, paths are normalised, and claiming never fails
  - it returns whoever else is already holding the path, which is the point.
- `who_has(path)` asks before starting; `release_files(paths)` lets go early.
- The Claude daemon does not rely on the agent remembering. Its `can_use_tool`
  hook claims a file when the agent edits it, and when somebody else holds it
  the edit comes back refused, naming the holder and the task and telling the
  agent to message them, check `get_inbox`, and `reply` with status `blocked`
  rather than editing on top of their work. Reads are never gated.
- A claim belongs to a run, so it is released when that invocation ends, and
  it stops counting as soon as its agent stops heartbeating - a crashed daemon
  cannot wedge the repository, and there is no TTL to tune.
- The next run starts informed: files other agents hold are written into the
  task prompt, and the agents page shows the same list live.

Codex has no per-tool callback to hook, so its claims are whatever the agent
takes through the tools - cooperative all the way down. Both backends release
everything they hold when a run ends and when the daemon stops.

## Editing the data

Four pages, in increasing order of bluntness:

- **Project** - the description and the task list.
- **Agents** - per-agent settings, prompts, live status and spend.
- **Docs** - the shared knowledge agents read and write through `docs_get` /
  `docs_set`: create a key, edit its content, delete it.
- **Data** - every table in the DB (`tasks`, `task_deps`, `agents`,
  `messages`, `file_claims`, `docs`, `events`), row by row: list with paging, insert, edit the columns that are
  safe to edit, delete. It is driven by one spec per table in `tables.py`, so
  a new table means one entry there rather than a new page.

The Data page is deliberately raw - editing `messages` rewrites the audit log
and the cost ledger, and editing `agents` does not write back to
`config.toml`. Both say so on the page.

## Agent tools

Thirteen tools - `get_inbox`, `send_message`, `claim_task`, `create_task`,
`list_tasks`, `reply`, `docs_get`, `docs_set`, `docs_list`, `claim_files`,
`release_files`, `who_has`, `heartbeat` - defined once in `tools.py` and
consumed three ways:

- **Claude** registers them in-process via `create_sdk_mcp_server()` - no
  extra process.
- **Codex** connects to `kuska mcp` over stdio, which serves the same
  definitions.
- **Anything else** can point a generic MCP client at that same command.

## Configuration

`.agents/config.toml` is the agent registry; `.agents/prompts/<name>.md` is
where each agent's instructions live. Both are editable from the agents page:
click an agent to open its editor, where backend, model, role and the
backend-specific options are saved straight back into `config.toml`, and the
prompt back into its file. The same panel adds and removes agents.

Two things to know: saving from the web UI rewrites `config.toml` and drops
hand-written comments, and a running daemon reads its settings at startup, so
a model change needs that daemon restarted.

```toml
[agents.dev-agent]
backend = "claude"          # 'claude' | 'codex'
model = "claude-opus-5"
role = "Implements features and fixes bugs"

[agents.codex-1]
backend = "codex"
model = "gpt-5-codex"
sandbox = "workspace-write"          # read-only | workspace-write | full-access
price_in_per_mtok = 1.25             # codex reports tokens, not dollars
price_out_per_mtok = 10.0
codex_bin = "/usr/local/bin/codex"   # only needed for the PyInstaller build
```

`permission_mode` (claude) and `sandbox` (codex) are passed through to the
SDKs. Adding another option means one entry in `AGENT_FIELDS` in
`project.py` - the form, validation and the daemon read it from there.

## Building a binary

```bash
uv run pyinstaller kuska.spec --clean --noconfirm   # -> dist/kuska (~38 MB)
```

One binary covers every subcommand, including the daemons. Both agent SDKs
ship their own ~225 MB CLI inside the wheel; those are left out, so the binary
uses the `claude` and `codex` CLIs on `PATH` (or `codex_bin` from
`config.toml`). `AGENTCTL_BUNDLE_CLIS=1` bundles them anyway.

## Tests

```bash
uv run tests/run_all.py
```

No test framework and no API keys: the model call is stubbed, so the daemon
loop, the web UI and the MCP server are all exercised without spending a
token.

## Status against the build plan

Done: core functions, `init`/`export`, both web pages, the Claude daemon, the
`mcp` subcommand and the Codex daemon, and the multi-project registry.

Not built: the OpenAI / open-weight adapter (step 7) - a generic MCP client
wired into a custom tool-calling loop. It needs no new state code, only a new
module under `daemons/` exposing `run_daemon()`.
