"""kuska - SQLite-backed coordination for multiple coding agents.

One shared core; every integration is a thin adapter around it. Import
anything from here (``import kuska as core``) rather than reaching into the
submodules - the split is for readability, not to draw API boundaries.
"""

# ruff: noqa: F401  - this module exists to re-export

from __future__ import annotations

from . import eventfmt
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
from .guardrails import RULES, check_command, check_tool, refusal_text
from .markdown import as_markdown
from .models import MODELS, Agent, Doc, Event, Feature, Message, Task, TaskDep
from .project import (
    AGENT_FIELD_KEYS,
    AGENT_FIELDS,
    DEFAULT_FLAVOR,
    DEFAULTS_DIR,
    FLAVORS,
    REGISTRY,
    agent_config,
    config_path,
    db_path,
    default_config,
    default_prompt,
    find_project,
    load_config,
    merge_prompt,
    prompt_path,
    prompt_template_path,
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
    DEFAULT_TIMEOUT_MINUTES,
    Monologue,
    RunAborted,
    compose_task_prompt,
    estimate_cost,
    estimate_token_count,
    fail_task,
    finish_task,
    get_workflow_context,
    one_line,
    run_limits,
    store_workflow_context,
)
from .store import (
    add_dependency,
    add_task,
    ask_agent,
    is_answer_task,
    waiting_on_answer,
    blocking_dependencies,
    blocking_map,
    calculate_rolling_cost_average,
    check_cost_anomaly,
    claim_task,
    delete_feature,
    delete_task,
    docs_get,
    docs_list,
    docs_set,
    ensure_feature,
    full_text_search,
    get_agent,
    get_event,
    get_feature,
    get_feature_by_name,
    get_inbox,
    get_task,
    heartbeat,
    list_agents,
    list_features,
    list_tasks,
    log_event,
    mark_messages_read,
    normalize_path,
    recent_events,
    recent_runs,
    register_agent,
    remove_dependency,
    reply,
    run_events,
    send_message,
    task_dependencies,
    task_dependents,
    task_events,
    task_messages,
    token_usage_by_agent,
    update_feature,
    update_task,
    update_task_status,
    wait_for_task,
)
from .tools import TOOL_SPECS, call_tool, tool_result_text, toolset
from .web import create_app
from .mcp_server import run_mcp

__version__ = "0.1.0"


__all__ = [n for n in dir() if not n.startswith("_")]
