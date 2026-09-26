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
uv run kuska serve           # web UI on http://127.0.0.1:5055
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
  tools.py       # the ten shared agent tools, defined once
  guardrails.py  # refuse rm -rf, git reset --hard, sudo, etc. before they run
  worktree.py    # git worktrees: branch per task, merge queue
  mcp_server.py  # those tools over stdio, for Codex and other external clients
  web.py         # Flask + HTMX routes
  runner.py      # run-all: web server + MCP + daemons in one command
  templates/     # Jinja templates, layout.html plus one file per page/fragment
  static/app.css # the whole stylesheet
  defaults/      # config.toml and the prompt `kuska init` seeds a project with
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

Everything under `.agents/` is that project's own state, seeded once from
`src/kuska/defaults/`. Worktrees with unmerged work now live there. To safely
clean up, run `kuska worktree prune --all` first to remove merged worktrees and
resolve stale metadata in `.git/worktrees`, then delete the directory.

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
3. If the agent is configured with `worktree = true`, the daemon creates a
   git worktree at `.agents/worktrees/task-<id>` on branch `kuska/<id>-<slug>`.
   The agent runs **one fresh invocation** there. No worktree: the agent runs in
   the main checkout.
4. The daemon runs **one fresh invocation** - no conversation is kept between
   tasks, so context never accumulates or goes stale. What the agent needs to
   know (description, earlier turns, answers to its questions) is composed
   into the prompt by `compose_task_prompt`.
5. The result and the turn's tokens/cost are written to `messages`, which
   doubles as the audit log and the cost ledger. The agents page reads its
   status and spend straight out of it.
6. If the task ran in a worktree, the agent commits there and the task
   enters `ready_to_merge`. A human reviews the branch, then merges it in a
   terminal and marks it merged in the web UI, which releases anything that
   depended on it.

An agent that needs something from another agent sends a message, marks its
task `blocked` and exits, rather than waiting inline. When the answer lands,
the human re-queues the task from the web UI and the next run gets the reply
as context.

Note: Worktrees resolve conflicts at merge time rather than preventing them.
The claim system they replaced never actually prevented conflicts anyway — it
only counted claims from agents that had heartbeated in the last 180 seconds,
and no daemon heartbeats mid-run.

## Tasks in their own branches

Agents configured with `worktree = true` run each task in an isolated git
worktree on its own branch, created at `.agents/worktrees/task-<id>` on branch
`kuska/<id>-<slug>`. The agent makes commits there; when done, the task enters
`ready_to_merge`. A human reviews the branch in a terminal, merges it, and
marks it merged in the web UI.

This design trades conflict prevention for isolation and explicitness: you now
resolve conflicts at merge time rather than preventing them by bookkeeping.
The claim system it replaced never actually prevented conflicts anyway — it
only saw claims from agents that had heartbeated in the last 180 seconds, and
no daemon heartbeats mid-run. Worktrees give you a clear boundary and a git
audit trail instead.

## Editing the data

Four pages, in increasing order of bluntness:

- **Project** - the description and the task list.
- **Agents** - per-agent settings, prompts, live status and spend.
- **Docs** - the shared knowledge agents read and write through `docs_get` /
  `docs_set`: create a key, edit its content, delete it.
- **Data** - every table in the DB (`tasks`, `task_deps`, `agents`,
  `messages`, `docs`, `events`), row by row: list with paging, insert, edit the columns that are
  safe to edit, delete. It is driven by one spec per table in `tables.py`, so
  a new table means one entry there rather than a new page.

The Data page is deliberately raw - editing `messages` rewrites the audit log
and the cost ledger, and editing `agents` does not write back to
`config.toml`. Both say so on the page.

## Agent tools

Ten tools - `get_inbox`, `send_message`, `reply`, `docs_get`, `docs_set`,
`docs_list`, `heartbeat`, `create_task`, `list_tasks`, `search` - defined once
in `tools.py` and consumed three ways:

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
worktree = true             # run tasks in per-task git worktrees; requires git repo

[agents.codex-1]
backend = "codex"
model = "gpt-5-codex"
role = "Second opinion"
sandbox = "workspace-write"          # read-only | workspace-write | full-access
price_in_per_mtok = 1.25             # codex reports tokens, not dollars
price_out_per_mtok = 10.0
codex_bin = "/usr/local/bin/codex"   # only needed for the PyInstaller build
```

`permission_mode` (claude) and `sandbox` (codex) are passed through to the
SDKs. Adding another option means one entry in `AGENT_FIELDS` in
`project.py` - the form, validation and the daemon read it from there.

Destructive shell commands are refused by `src/kuska/guardrails.py` before they run, regardless of `permission_mode`: `rm -rf`, `git reset --hard`, `git push --force`, `git clean -fd`, `git branch -D`, `sudo`, downloads piped into a shell, `chmod 777`, writes outside the project, anything aimed at `.agents/project.db`, and anything aimed at `.agents/worktrees`. Adding a rule is one dict in the `RULES` table.

This enforcement catches mistakes, not a determined agent — `sh -c "rm -rf build"`, `find -delete`, and `python -c "shutil.rmtree(...)"` all pass through.

The guardrails are wired into the Claude daemon's `PreToolUse` hook. The codex backend has no per-tool callback and the openai backend has no permission concept, so `guardrails.py` is written to be callable from them but does not yet protect them. `dev-agent` is currently a codex agent, so the guardrails do not yet protect its shell commands.

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
