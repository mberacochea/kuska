"""Project layout on disk: .agents/, prompts, config.toml and the registry.

A directory with an .agents/ subdirectory is a project. Running several
projects in parallel is just several of these, each with its own DB file and
its own daemons."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from peewee import SqliteDatabase

from .store import register_agent

DEFAULT_CONFIG = """# agent registry for this project
# one [agents.<name>] table per agent; the prompt lives in .agents/prompts/<name>.md

[agents.dev-agent]
backend = "claude"
model = "claude-opus-5"
role = "Implements features and fixes bugs"

# [agents.codex-1]
# backend = "codex"
# model = "gpt-5-codex"
# role = "Second opinion / parallel implementation"
# codex_bin = "/usr/local/bin/codex"   # only needed for the PyInstaller build
# price_in_per_mtok = 1.25             # codex reports tokens, not dollars
# price_out_per_mtok = 10.0

# [agents.openai-1]
# backend = "openai"
# model = "gpt-4"
# role = "OpenAI-backed agent"
# api_key = "sk-..."                   # or set via OPENAI_API_KEY env var
# # base_url = "http://localhost:8000/v1"  # for local models or proxies
# price_in_per_mtok = 0.03             # openai reports cost, but you can override
# price_out_per_mtok = 0.06

[mcp_servers.kuska]
command = "uv"
args = ["run", "kuska", "mcp"]
"""

DEFAULT_PROMPT = """# {name}

You are `{name}`, {role}, working inside a multi-agent project.

## MCP Tools (Critical)

You have access to these MCP tools to coordinate with other agents and manage shared project knowledge. **Use these tools actively** — they are your primary interface for inter-agent communication and shared state:

### Shared Project Knowledge
- **`docs_get(key)`** - Read shared project docs by key (e.g., 'plan', 'architecture', 'task_N_planning-agent_context'). Always read relevant docs first before assuming or designing — the planning-agent may have already created a strategy.
- **`docs_set(key, content)`** - Write shared docs that other agents will read. Use this to pass context forward (e.g., `task_N_dev-agent_context`). **DO NOT** store plans, decisions, or shared knowledge in local folders or home directories — always use `docs_set`.

### Task & Message Management
- **`send_message(recipient, payload, msg_type='question'|'blocker'|'note')`** - Send a message to another agent (e.g., 'planning-agent', 'review-agent') or 'human'. Use msg_type='blocker' when you're stuck and need input before proceeding.
- **`get_inbox()`** - Check for new messages from other agents or the human. This returns only unread messages, so it's cheap to call early in your task to see if there's new context you need.
- **`reply(task_id, payload, status='done'|'blocked'|'needs_approval')`** - Log the result of your task. Status 'blocked' means you're waiting on someone else; the coordinator will re-queue after they reply. 'needs_approval' means the human should review before the next agent starts work.

### File Coordination
- **`claim_files(paths, note)`** - Before editing files, claim them. This tells you if another agent is already working on them. If someone holds a file you need, send them a message instead of editing around them.
- **`release_files(paths)`** - Let go of files once you're done. Everything you hold releases anyway when your task ends, but don't make colleagues wait longer than necessary.
- **`who_has(path)`** - Quick check: is anyone touching this file right now?

### Task Creation & Claiming
- **`create_task(title, description, assigned_to=None)`** - Create a new task. Rarely used by dev-agent (that's planning-agent's job), but available if you discover critical work that blocks you.
- **`claim_task()`** - Claim the next task assigned to you (your daemon does this, but available if needed).

### Heartbeat
- **`heartbeat(status='working'|'idle'|'offline', task_id=None)`** - Report your status. Your daemon manages this, but useful for long-running tasks to show you're still alive.

## Workflow context passing

When you get context from planning-agent in your prompt:
- It appears as "## Context from planning-agent" section
- This replaces the need to re-read message history
- Use it as your implementation guide, then pass your own context forward to review-agent:

```python
# At the end of your task (before calling reply with "done" status):
import json
context = json.dumps({
    "implementation_summary": "What you built and why",
    "files_modified": ["src/file1.py", "src/file2.py"],
    "key_changes": ["Change 1: why it matters", "Change 2: testing notes"],
    "breaking_changes": [],
    "test_coverage": "Which tests you added or modified",
    "known_issues": "Any technical debt or future improvements"
})
docs_set(db, f"task_{task_id}_dev-agent_context", context)
```

This context will automatically appear in review-agent's prompt, saving them token budget for deeper code analysis.

## How you work

- You are handed exactly one task per invocation, in a fresh context. Nothing
  carries over between invocations, so write down anything that matters.
- When the task is done, end your turn with a summary of what you changed and
  why. Your daemon logs that summary as the task result, with the turn's token
  and cost numbers - you do not need to call `reply` yourself.
- If you need something from another agent or from the human, call
  `send_message`, then `reply` with status `blocked`, and stop. Never wait
  inline: the human re-queues the task once the answer lands, and you get it
  as context in a fresh run.
- If your work needs a human to sign off before anything built on top of it
  runs, `reply` with status `needs_approval`. Every task that depends on this
  one waits until a human approves it or sends it back; unrelated tasks carry
  on.
- Shared project knowledge lives in `docs_get` / `docs_set` - read before you
  assume, write when you learn something the next agent will need.

## Reading files efficiently

Everything a tool returns stays in your context and is re-sent to the model on
every remaining step of your turn. A 1,200-line file you read once is paid for
dozens of times over. This is the single largest cost in a run, and it is
entirely under your control.

- **Locate, then read.** Use `Grep` to find where something lives, then `Read`
  with `offset` and `limit` to pull just that region. Do not fetch a whole
  module to look at one function.
- **You already have it.** A file you read earlier this turn is still in your
  context - scroll back instead of reading it again. The daemon refuses a
  repeat read of an unchanged file and will tell you so.
- **Do not re-read to verify an edit.** `Edit` fails loudly if its `old_string`
  did not match. Silence means it applied.
- **Re-read only after a change.** Once you `Write` or `Edit` a file, reading it
  again is fair and permitted.

## Working alongside other agents

Other agents are changing this repository at the same time as you.

- Before you edit anything, call `claim_files` with the paths (or directories)
  you are about to change. It tells you if somebody already holds them.
- If a file you need is held, do not edit around it: `send_message` to whoever
  holds it and say what you need. Then either work on something else in your
  task, or `reply` with status `blocked` and stop.
- Call `release_files` as soon as you are done with a path, so nobody waits on
  you longer than necessary. Everything you hold is released when your run
  ends anyway.
- `who_has` answers "is anyone touching this?" before you start reading a file
  you intend to change.
- When something surprises you - an edit refused, a file that does not look
  the way your task described it, work that seems already done - call
  `get_inbox`. It returns only messages you have not seen yet, so it is cheap
  to check and it will not hand you old news twice.
"""


def prompt_path(project_dir: str | os.PathLike, agent_name: str) -> Path:
    return Path(project_dir) / ".agents" / "prompts" / f"{agent_name}.md"


def read_prompt(project_dir: str | os.PathLike, agent_name: str) -> str:
    path = prompt_path(project_dir, agent_name)
    return path.read_text() if path.exists() else ""


def write_prompt(project_dir: str | os.PathLike, agent_name: str, content: str) -> None:
    path = prompt_path(project_dir, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def find_project(start: str | os.PathLike | None = None) -> Path:
    """Nearest ancestor directory containing .agents/ (defaults to cwd)."""
    path = Path(start or os.getcwd()).resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".agents").is_dir():
            return candidate
    raise SystemExit(
        f"no .agents/ directory found at or above {path} - run `kuska init` first"
    )


def db_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".agents" / "project.db"


def config_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / ".agents" / "config.toml"


# What the web UI offers per agent, and what `config.toml` may hold for one.
# The daemons read these keys straight off the agent's table, so this list is
# the single place a new option has to be named.
AGENT_FIELDS = [
    {
        "key": "backend",
        "label": "Backend",
        "type": "choice",
        "choices": ["claude", "codex", "openai"],
        "help": "which daemon runs this agent",
    },
    {
        "key": "model",
        "label": "Model",
        "type": "text",
        "help": "blank uses the backend's own default",
    },
    {"key": "role", "label": "Role", "type": "text", "help": "shown in the agents table"},
    {
        "key": "permission_mode",
        "label": "Permission mode",
        "type": "choice",
        "choices": ["", "acceptEdits", "default", "plan", "bypassPermissions", "dontAsk", "auto"],
        "help": "claude only - blank means acceptEdits",
    },
    {
        "key": "sandbox",
        "label": "Sandbox",
        "type": "choice",
        "choices": ["", "read-only", "workspace-write", "full-access"],
        "help": "codex only",
    },
    {
        "key": "codex_bin",
        "label": "Codex binary",
        "type": "text",
        "help": "codex only - needed when the CLI is not on PATH",
    },
    {
        "key": "api_key",
        "label": "API Key",
        "type": "text",
        "help": "openai only - OpenAI API key or similar",
    },
    {
        "key": "base_url",
        "label": "Base URL",
        "type": "text",
        "help": "openai only - for local models or proxies (e.g., http://localhost:8000/v1)",
    },
    {
        "key": "price_in_per_mtok",
        "label": "Input $/Mtok",
        "type": "number",
        "help": "codex/openai - price tokens when the model doesn't report cost",
    },
    {"key": "price_out_per_mtok", "label": "Output $/Mtok", "type": "number", "help": ""},
]

AGENT_FIELD_KEYS = [f["key"] for f in AGENT_FIELDS]


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _toml_table(name: str, table: dict) -> list[str]:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    # a table that only groups sub-tables ([agents]) needs no header of its own
    lines = [f"[{name}]"] if scalars or not subtables else []
    lines += [f"{key} = {_toml_value(value)}" for key, value in scalars.items()]
    for key, value in subtables.items():
        if lines:
            lines.append("")
        lines += _toml_table(f"{name}.{key}", value)
    return lines


def write_config(project_dir: str | os.PathLike, config: dict) -> None:
    """Write config.toml back out.

    Only scalars, arrays of scalars and nested tables - which is all this file
    ever holds. Hand-written comments do not survive a save from the web UI,
    so the header says where the file is now edited.
    """
    lines = [
        "# agent registry for this project",
        "# one [agents.<name>] table per agent; the prompt lives in",
        "# .agents/prompts/<name>.md. Editable here or on the web UI's agents",
        "# page - saving there rewrites this file and drops any comments.",
        "",
    ]
    for name, table in config.items():
        if isinstance(table, dict):
            lines += [*_toml_table(name, table), ""]
        else:
            lines.append(f"{name} = {_toml_value(table)}")
    config_path(project_dir).write_text("\n".join(lines).rstrip("\n") + "\n")


def set_agent_config(project_dir: str | os.PathLike, agent_name: str, values: dict) -> dict:
    """Create or update one agent's table. Blank values drop the key entirely."""
    config = load_config(project_dir)
    agents = config.setdefault("agents", {})
    table = dict(agents.get(agent_name, {}))
    for field in AGENT_FIELDS:
        key = field["key"]
        if key not in values:
            continue
        raw = values[key]
        raw = raw.strip() if isinstance(raw, str) else raw
        if raw in ("", None):
            table.pop(key, None)
        elif field["type"] == "number":
            table[key] = float(raw)
        else:
            table[key] = raw
    table.setdefault("backend", "claude")
    agents[agent_name] = table
    write_config(project_dir, config)
    return table


def remove_agent_config(project_dir: str | os.PathLike, agent_name: str) -> bool:
    config = load_config(project_dir)
    if agent_name not in config.get("agents", {}):
        return False
    del config["agents"][agent_name]
    write_config(project_dir, config)
    return True


def load_config(project_dir: str | os.PathLike) -> dict:
    path = config_path(project_dir)
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def agent_config(project_dir: str | os.PathLike, agent_name: str) -> dict:
    cfg = load_config(project_dir).get("agents", {}).get(agent_name)
    if cfg is None:
        raise SystemExit(f"agent '{agent_name}' is not in {config_path(project_dir)}")
    return cfg


def sync_agents_from_config(db: SqliteDatabase, project_dir: str | os.PathLike) -> list[str]:
    """Make the agents table match config.toml, and seed missing prompt files."""
    names = []
    for name, cfg in load_config(project_dir).get("agents", {}).items():
        register_agent(db, name, cfg.get("backend", "claude"), cfg.get("role", ""))
        names.append(name)
        if not prompt_path(project_dir, name).exists():
            # .replace, not .format: the template is markdown documentation
            # carrying code samples - json.dumps({...}), f"task_{task_id}_..." -
            # and str.format reads every one of those braces as a field name.
            seeded = (
                DEFAULT_PROMPT
                .replace("{name}", name)
                .replace("{role}", cfg.get("role", "a coding agent"))
            )
            write_prompt(project_dir, name, seeded)
    return names


# --- multi-project registry (~/.kuska/projects.toml) -------------------

REGISTRY = Path(os.path.expanduser("~/.kuska/projects.toml"))


def registry_load() -> dict[str, str]:
    if not REGISTRY.exists():
        return {}
    with REGISTRY.open("rb") as fh:
        data = tomllib.load(fh)
    return {k: v["path"] for k, v in data.get("projects", {}).items() if "path" in v}


def registry_add(name: str, path: str | os.PathLike) -> None:
    projects = registry_load()
    projects[name] = str(Path(path).resolve())
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# projects known to kuska - written by `kuska init`", ""]
    for key, value in sorted(projects.items()):
        lines += [f"[projects.{key}]", f'path = "{value}"', ""]
    REGISTRY.write_text("\n".join(lines))
