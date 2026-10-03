# Development Workflow

This project provides two ways to run the system: a Taskfile-based workflow for development, and a single `run-all` command for production deployments.

## Quick Start

### Option 1: Using Taskfile (Recommended for Development)

[Taskfile](https://taskfile.dev/) is a simple task runner that makes it easy to run common development commands.

Install Taskfile:
```bash
# macOS
brew install go-task/tap/go-task

# Linux (see https://taskfile.dev/installation/ for other methods)
sudo snap install task --classic
```

Then run tasks:
```bash
# Initialize the project
task init

# Run the web UI only
task serve

# Run a specific agent daemon
task daemon AGENT=dev-agent

# Run the MCP server
task mcp

# Run all services together (web + all agents)
task dev

# Run specific agents with web
task run-all-agents AGENTS=dev-agent,codex-1

# Show all available tasks
task -l
```

### Option 2: Using the Single `run-all` Command

For production deployments or running without Taskfile:

```bash
# Initialize the project first
uv run kuska init

# Run all services together
uv run kuska run-all

# Run specific agents
uv run kuska run-all --agents dev-agent,codex-1

# Run all configured agents
uv run kuska run-all --agents "*"

# Custom host/port
uv run kuska run-all --host 127.0.0.1 --port 8080
```

## What Each Command Does

- **`task init`** - Creates `.agents/` directory, SQLite database, and default config
- **`task serve`** - Starts Flask web UI on http://0.0.0.0:5055 with debug mode enabled
- **`task daemon <agent>`** - Runs a single agent daemon (reads backend from config.toml)
- **`task mcp`** - Starts MCP server for external clients (Codex, etc.)
- **`task dev`** - Runs web UI + all configured agents in parallel (recommended!)
- **`task run-all-agents AGENTS=<list>`** - Run web UI + specific agents
- **`task export`** - Export project as markdown
- **`task test`** - Run all tests (`uv run pytest`)
- **`task build`** - Build PyInstaller binary (dist/kuska)
- **`task clean`** - Remove build artifacts and cache

## Tests

```bash
uv run pytest              # everything
uv run pytest -k <name>    # one suite or test, e.g. -k test_web or -k token
```

New tests are plain pytest functions using the `project` and `conn` fixtures in
`tests/conftest.py`. The big legacy suites (`test_core.py`, `test_daemon.py`,
`test_web.py`, `test_worktree.py`, `test_search.py`, `test_guardrails.py`,
`test_concurrent_init.py`, `eventfmt_test.py`) are scripts that stop at their
first failing check; `tests/test_legacy_suites.py` runs each as a subprocess
until it is converted. To convert one, rewrite it as pytest functions and remove
it from `collect_ignore` in `conftest.py` and from the list in
`test_legacy_suites.py`. `uv run tests/run_all.py` forwards its arguments to
pytest.

## Monitoring

### Live Activity

The web UI shows live activity for all agents:
1. Start: `task dev`
2. Open: http://0.0.0.0:5055
3. Click: "Agents" page → "Live Activity" section

Each event is expandable to see full details, and the database stores the complete run history.

### Database Access

Use the "Data" page in the web UI to inspect:
- `tasks` - all tasks and their status
- `messages` - inter-agent communication and cost tracking
- `events` - full narration of agent runs
- `docs` - shared knowledge base

## Single Binary (Production)

Build a standalone binary:

```bash
task build
./dist/kuska run-all --agents "*"
```

To bundle agent SDKs (makes binary larger but self-contained):
```bash
task build-with-clis
```

## Troubleshooting

### Port 5055 already in use
```bash
uv run kuska run-all --port 8080
```

### Specific agent not starting
1. Check configuration: Look at `.agents/config.toml`
2. Verify backend is installed: `which claude` or `which codex`
3. Check logs in web UI → Data page → events table

### Agent crashes on startup
```bash
# Run with verbose output
task daemon AGENT=dev-agent
```

Or in the web UI, check the "Agents" page for error messages and spend.

## Working with Worktrees

When an agent is configured with `worktree = true`, each task runs in its own
git worktree on a dedicated branch. Worktrees live under `.agents/worktrees/`.

### Where they are

Each worktree is named `task-<id>` and lives at:
```
.agents/worktrees/task-42/          # the worktree directory
# checked out on branch:
kuska/42-short-description          # auto-generated from task title
```

### Reviewing a worktree

1. **From the web UI**, find the task in the task list and mark it as "ready to review" or find its "ready_to_merge" status.

2. **In a terminal**, list available worktrees:
```bash
git worktree list
```

3. **Check out the worktree** to review:
```bash
cd .agents/worktrees/task-42
git log --oneline                   # see agent's commits
git diff main                       # see what changed vs main
```

4. **Merge when ready**:
```bash
# From the main checkout, merge the branch
git merge kuska/42-short-description

# Then clean up the worktree
git worktree remove .agents/worktrees/task-42
```

5. **Mark it merged** in the web UI so other tasks that depend on it can proceed.

### Pruning worktrees

Remove all pruned (already-merged) worktrees and clean up `.git/worktrees` metadata:
```bash
kuska worktree prune --all
```

This is safe to run anytime; it only removes worktrees whose branches are
already merged into main.

## Architecture

The system consists of:

1. **Web Server** (Flask) - Project planning, agent configuration, live status
2. **Agent Daemons** - Run continuously, poll for tasks, report progress

The MCP server (`kuska mcp`) is separate: clients start their own, so `run-all` does not.

`run-all` starts both in separate threads:
- Web server blocks the main thread
- Each agent daemon runs in its own thread, polling for tasks

Ctrl+C cleanly shuts down all services.

### Single-Threaded Web Server

The web server runs with `threaded=False` (serial request handling, not concurrent). This is intentional: the Flask app's `state` dict holds one process-wide open project and its SQLite database handle. When a request calls `open_project()` to switch projects, it closes the previous handle. With concurrent requests, another thread could be mid-query on that closed handle, causing an unhandled exception.

Kuska is a single-user tool, so serving one request at a time is the correct design:
- One project open per process
- One database handle per process
- No need for concurrent request handling

Do not add a global lock around request handling or rework `state` into per-session storage — serving one project at a time is accepted behaviour here.

## Multi-Agent Context Passing (Phase 4.1)

When building multi-agent workflows (planning → dev → review), agents pass a handover report forward to reduce token usage by 20-30%.

**Reports are Markdown, not JSON.** A doc is a document: the web UI renders it
as Markdown and `export_markdown` folds it into `plan.md`, where a JSON dump
reads as a wall of escaped quotes. `docs_set` (the agent tool) and
`store_workflow_context` run their content through `markdown.as_markdown`,
which rewrites a body that parses whole as JSON into headings and bullets and
leaves prose untouched — a backstop, not a licence to emit JSON.

### How it works

1. **Planning Agent** completes with `status="needs_approval"` and stores a report:
```python
report = """# Task 42: add the parser

## Approach
What we are going to do, and why this way.

## Files to modify
- `src/core.py` — where the new branch goes

## Constraints
Must stay backward compatible.
"""
docs_set(db, f"task_{task_id}_planning-agent_context", report)
```

2. **Dev Agent** automatically receives this context in its prompt:
   - Appears as "## Context from planning-agent" section
   - Skips re-parsing message history
   - Uses the context to guide implementation

3. **Dev Agent** stores its own report for review:
```python
dev_report = """# Task 42: add the parser

## Summary
What you built, and why.

## Files changed
- `src/core.py` — the new branch, and what it assumes
- `tests/test_core.py` — ten checks covering it

## Known issues
None.
"""
docs_set(db, f"task_{task_id}_dev-agent_context", dev_report)
```

4. **Review Agent** gets dev context for focused code review:
   - Knows what changed without re-reading commits
   - Can focus on high-impact areas
   - Skips unnecessary re-reading

### API Functions

- `get_workflow_context(db, task, source_agent=None)` - Retrieve context from previous agent
- `store_workflow_context(db, agent_name, task_id, context)` - Store context for next agent

Context is automatically included in agent prompts via `compose_task_prompt()`.

### Expected Token Savings

- **Planning → Dev**: 25-30% (skips re-reading planning discussion)
- **Dev → Review**: 15-20% (skips re-reading implementation details)
- **Total for workflow**: 20-30% across the chain

See test `check_workflow_context` in `tests/test_daemon.py` for verification.
