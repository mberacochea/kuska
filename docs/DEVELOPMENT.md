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

# Run all services together (web + MCP + all agents)
task dev

# Run specific agents with web + MCP
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
- **`task dev`** - Runs web UI + MCP + all configured agents in parallel (recommended!)
- **`task run-all-agents AGENTS=<list>`** - Run web UI + MCP + specific agents
- **`task export`** - Export project as markdown
- **`task test`** - Run all tests
- **`task build`** - Build PyInstaller binary (dist/kuska)
- **`task clean`** - Remove build artifacts and cache

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
- `file_claims` - which files are being edited
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

## Architecture

The system consists of:

1. **Web Server** (Flask) - Project planning, agent configuration, live status
2. **MCP Server** - Coordination tools (claim_files, send_message, etc.) for external clients
3. **Agent Daemons** - Run continuously, poll for tasks, report progress

`run-all` starts all three in separate threads:
- Web server blocks the main thread
- MCP server runs in background
- Each agent daemon runs in its own thread, polling for tasks

Ctrl+C cleanly shuts down all services.

## Multi-Agent Context Passing (Phase 4.1)

When building multi-agent workflows (planning → dev → review), agents can pass structured context forward to reduce token usage by 20-30%.

### How it works

1. **Planning Agent** completes with `status="needs_approval"` and stores context:
```python
import json
context = json.dumps({
    "phase": 1,
    "approach": "Your implementation strategy",
    "key_decisions": ["Decision 1", "Decision 2"],
    "files_to_modify": ["src/core.py"],
    "critical_constraints": "Must maintain backward compatibility"
})
docs_set(db, f"task_{task_id}_planning-agent_context", context)
```

2. **Dev Agent** automatically receives this context in its prompt:
   - Appears as "## Context from planning-agent" section
   - Skips re-parsing message history
   - Uses the context to guide implementation

3. **Dev Agent** stores its own context for review:
```python
dev_context = json.dumps({
    "implementation_summary": "What you built",
    "files_modified": ["src/core.py", "tests/test_core.py"],
    "key_changes": ["Change 1: why it matters"],
    "test_coverage": "Added 10 new unit tests"
})
docs_set(db, f"task_{task_id}_dev-agent_context", dev_context)
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
