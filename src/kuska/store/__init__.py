"""Agents, tasks, dependencies, messages, docs and events - every read and
write of project state.

Plain functions over a peewee database handle, returning plain dicts: the ORM
stays inside this package, so the daemons, the web app and the MCP server keep
working against the same small vocabulary they always did.

One module per kind of record; this package re-exports them all, so callers
import from `kuska.store` and never need to know which file a function is in.
"""

from __future__ import annotations

from .agents import (
    delete_agent,
    get_agent,
    heartbeat,
    list_agents,
    register_agent,
)
from .common import (
    bound,
    normalize_path,
)
from .deps import (
    add_dependency,
    blocking_dependencies,
    blocking_map,
    remove_dependency,
    task_dependencies,
    task_dependents,
)
from .docs import (
    docs_get,
    docs_list,
    docs_set,
)
from .events import (
    get_event,
    log_event,
    recent_events,
    recent_runs,
    run_events,
    task_events,
)
from .lifecycle import (
    TRANSITIONS,
    InvalidTransition,
    transition,
)
from .messages import (
    ANSWER_TAG,
    ask_agent,
    get_inbox,
    is_answer_task,
    latest_result_since,
    mark_messages_read,
    record_usage,
    reply,
    reply_to_task,
    send_message,
    task_messages,
    waiting_on_answer,
)
from .features import (
    delete_feature,
    ensure_feature,
    get_feature,
    get_feature_by_name,
    list_features,
    norm_feature_name,
    update_feature,
)
from .search import (
    full_text_search,
)
from .stats import (
    avg_task_duration,
    calculate_rolling_cost_average,
    check_cost_anomaly,
    cost_by_task,
    longest_tasks,
    task_counts_by_agent,
    task_status_counts,
    token_usage_by_agent,
)
from .tasks import (
    add_task,
    bulk_update_status,
    claim_task,
    delete_task,
    filter_tasks,
    get_task,
    list_tags,
    list_tasks,
    update_task,
    update_task_status,
    wait_for_task,
)

__all__ = [
    "ANSWER_TAG",
    "TRANSITIONS",
    "InvalidTransition",
    "add_dependency",
    "add_task",
    "bulk_update_status",
    "annotations",
    "ask_agent",
    "avg_task_duration",
    "blocking_dependencies",
    "blocking_map",
    "bound",
    "calculate_rolling_cost_average",
    "check_cost_anomaly",
    "claim_task",
    "cost_by_task",
    "delete_agent",
    "delete_feature",
    "delete_task",
    "docs_get",
    "docs_list",
    "docs_set",
    "ensure_feature",
    "filter_tasks",
    "full_text_search",
    "get_agent",
    "get_event",
    "get_feature",
    "get_feature_by_name",
    "get_inbox",
    "get_task",
    "heartbeat",
    "is_answer_task",
    "latest_result_since",
    "list_agents",
    "list_features",
    "list_tags",
    "list_tasks",
    "log_event",
    "longest_tasks",
    "mark_messages_read",
    "norm_feature_name",
    "normalize_path",
    "recent_events",
    "recent_runs",
    "register_agent",
    "remove_dependency",
    "record_usage",
    "reply",
    "reply_to_task",
    "run_events",
    "send_message",
    "task_counts_by_agent",
    "task_dependencies",
    "task_dependents",
    "task_events",
    "task_messages",
    "task_status_counts",
    "token_usage_by_agent",
    "transition",
    "update_feature",
    "update_task",
    "update_task_status",
    "wait_for_task",
    "waiting_on_answer",
]
