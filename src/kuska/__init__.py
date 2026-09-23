"""kuska - SQLite-backed coordination for multiple coding agents.

One shared core; every integration is a thin adapter around it. Import
anything from here (``import kuska as core``) rather than reaching into the
submodules - the split is for readability, not to draw API boundaries.
"""

# ruff: noqa: F401  - this module exists to re-export

from __future__ import annotations

from .db import (
    AGENT_STATUSES,
    EVENT_KINDS,
    HOLDING_STATUSES,
    HUMAN,
    TASK_STATUSES,
    connect,
    init_db,
    now,
)
from .export import export_markdown
from .markdown import as_markdown
from .models import MODELS, Agent, Doc, Event, FileClaim, Message, Task, TaskDep
from .project import (
    AGENT_FIELD_KEYS,
    AGENT_FIELDS,
    DEFAULTS_DIR,
    REGISTRY,
    agent_config,
    config_path,
    db_path,
    default_config,
    default_prompt,
    find_project,
    load_config,
    prompt_path,
    read_prompt,
    registry_add,
    registry_load,
    remove_agent_config,
    set_agent_config,
    sync_agents_from_config,
    write_config,
    write_prompt,
)
from .runtime import (
    Monologue,
    compose_task_prompt,
    estimate_cost,
    estimate_token_count,
    finish_task,
    get_workflow_context,
    one_line,
    store_workflow_context,
)
from .store import (
    CLAIM_STALE_AFTER,
    active_claims,
    add_dependency,
    add_task,
    blocking_dependencies,
    blocking_map,
    calculate_rolling_cost_average,
    check_cost_anomaly,
    claim_files,
    claim_holders,
    claim_task,
    delete_task,
    docs_get,
    docs_list,
    docs_set,
    get_agent,
    get_inbox,
    get_task,
    heartbeat,
    list_agents,
    list_tasks,
    log_event,
    normalize_path,
    recent_events,
    register_agent,
    release_files,
    release_run,
    remove_dependency,
    reply,
    run_events,
    send_message,
    task_dependencies,
    task_dependents,
    task_events,
    task_messages,
    token_usage_by_agent,
    update_task,
    update_task_status,
    wait_for_task,
)
from .tools import TOOL_SPECS, call_tool, tool_result_text

__version__ = "0.1.0"


def create_app(project_dir):
    """Flask app for one project (imported lazily: the daemons do not need it)."""
    from .web import create_app as _create_app

    return _create_app(project_dir)


def run_mcp(project_dir, agent_name, db=None):
    """Stdio MCP server (imported lazily, same reason)."""
    from .mcp_server import run_mcp as _run_mcp

    return _run_mcp(project_dir, agent_name, db)


__all__ = [n for n in dir() if not n.startswith("_")]
