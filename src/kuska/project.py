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

# Templates for a fresh project, shipped with the package. They are written
# out by `kuska init` and by sync_agents_from_config; nothing reads them back,
# so a project's .agents/ is entirely its own state and can be deleted.
DEFAULTS_DIR = Path(__file__).parent / "defaults"


def default_config() -> str:
    """The config.toml `kuska init` writes into a new project."""
    return (DEFAULTS_DIR / "config.toml").read_text()


def default_prompt(name: str, role: str) -> str:
    """The system prompt seeded for an agent that has no prompt file yet."""
    # .replace, not .format: the template is markdown documentation, and any
    # brace someone later adds to a code sample or a tool signature would be
    # read by str.format as a field name and blow up seeding a prompt.
    return (
        (DEFAULTS_DIR / "prompt.md")
        .read_text()
        .replace("{name}", name)
        .replace("{role}", role)
    )


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
            seeded = default_prompt(name, cfg.get("role", "a coding agent"))
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
