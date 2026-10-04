"""Usage figures for the stats page, and cost anomaly detection.

Anomalies are judged on cost, not tokens. Comparing token totals misled: cache
reads count as input tokens at around a tenth of the price of fresh input, so
a turn could triple its token count while costing less. Cost is the only
figure that compares like with like, and the backend reports it directly.
"""

from __future__ import annotations

from peewee import JOIN, SQL, SqliteDatabase, fn

from ..db import HUMAN
from ..models import Run, Task, rows
from .common import bound
from .events import log_event
from .tasks import get_task


@bound
def token_usage_by_agent(db: SqliteDatabase) -> list[dict]:
    """Aggregate token usage and cost by agent, highest cost first.

    Sums the `runs` ledger (finished and failed runs, not ones still running)
    per agent. `turns` counts runs. Useful for billing and performance analysis.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        list[dict]: Aggregated stats per agent, ordered by cost descending.
                    Each record has: agent, turns, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, tool_rounds, cost_usd.
                    Rows predating migration 003 report 0 cache tokens and carry
                    cache reads inside input_tokens.

    Examples:
        >>> usage = token_usage_by_agent(db)
        >>> for stat in usage:
        ...     print(f"{stat['agent']}: {stat['cost_usd']:.4f} USD "
        ...           f"({stat['input_tokens']} in, {stat['output_tokens']} out)")
    """
    query = (
        Run.select(
            Run.agent.alias("agent"),
            fn.COUNT(Run.id).alias("turns"),
            fn.COALESCE(fn.SUM(Run.input_tokens), 0).alias("input_tokens"),
            fn.COALESCE(fn.SUM(Run.output_tokens), 0).alias("output_tokens"),
            fn.COALESCE(fn.SUM(Run.cache_read_tokens), 0).alias("cache_read_tokens"),
            fn.COALESCE(fn.SUM(Run.cache_write_tokens), 0).alias("cache_write_tokens"),
            fn.COALESCE(fn.SUM(Run.tool_rounds), 0).alias("tool_rounds"),
            fn.COALESCE(fn.SUM(Run.cost_usd), 0.0).alias("cost_usd"),
        )
        .where(Run.agent.is_null(False), Run.agent != HUMAN, Run.status != "running")
        .group_by(Run.agent)
        .order_by(SQL("cost_usd DESC"))
    )
    return rows(query)


@bound
def task_status_counts(db: SqliteDatabase) -> list[dict]:
    """Task counts grouped by status, for the stats dashboard."""
    query = Task.select(Task.status, fn.COUNT(Task.id).alias("count")).group_by(Task.status)
    return rows(query)


@bound
def task_counts_by_agent(db: SqliteDatabase) -> list[dict]:
    """Per-agent total and completed task counts, for the stats dashboard."""
    query = (
        Task.select(
            Task.assigned_to,
            fn.COUNT(Task.id).alias("total_tasks"),
            fn.SUM(Task.status == "done").alias("completed_tasks"),
        )
        .where(Task.assigned_to.is_null(False))
        .group_by(Task.assigned_to)
    )
    return rows(query)


@bound
def longest_tasks(db: SqliteDatabase, limit: int = 10) -> list[dict]:
    """The `limit` longest-running done tasks, by wall-clock duration in seconds."""
    duration = Task.updated_at - Task.created_at
    query = (
        Task.select(Task.id, Task.title, duration.alias("duration"))
        .where(Task.status == "done")
        .order_by(SQL("duration DESC"))
        .limit(limit)
    )
    return rows(query)


@bound
def avg_task_duration(db: SqliteDatabase) -> float:
    """Average wall-clock duration of done tasks, in seconds."""
    duration = Task.updated_at - Task.created_at
    query = Task.select(fn.COALESCE(fn.AVG(duration), 0.0).alias("avg")).where(Task.status == "done")
    return query.dicts().get()["avg"]


@bound
def cost_by_task(db: SqliteDatabase, limit: int = 10) -> list[dict]:
    """Total cost per task, highest first, with the task title joined in.

    A left join, because a run can outlive the task it belonged to -
    `title` comes back None for those and the caller decides how to label them.
    """
    query = (
        Run.select(
            Run.task_id,
            Task.title,
            fn.COALESCE(fn.SUM(Run.cost_usd), 0.0).alias("cost"),
        )
        .join(Task, JOIN.LEFT_OUTER, on=(Run.task_id == Task.id))
        .where(Run.task_id.is_null(False))
        .group_by(Run.task_id)
        .order_by(SQL("cost DESC"))
        .limit(limit)
    )
    return rows(query)


@bound
def calculate_rolling_cost_average(
    db: SqliteDatabase, window_size: int = 20, exclude_task_id: int | None = None
) -> float:
    """Calculate the rolling average cost per task, in USD.

    Sums each task's ended runs, takes the tasks whose runs started most
    recently, and averages what they actually cost.

    Args:
        db: SqliteDatabase instance for this project.
        window_size: Number of recent tasks to average over (default 20).
        exclude_task_id: Optional task ID to exclude from calculation (typically
                        the current task being evaluated).

    Returns:
        float: Average USD per task. Returns 0 if no data available.

    Examples:
        >>> avg = calculate_rolling_cost_average(db, window_size=20)
        >>> print(f"Average cost per task: ${avg:.4f}")
    """
    query = (
        Run.select(Run.task_id, fn.SUM(Run.cost_usd).alias("total_cost"))
        .where(Run.task_id.is_null(False), Run.status != "running")
        .group_by(Run.task_id)
        .order_by(fn.MAX(Run.started_at).desc(), Run.task_id.desc())
        .limit(window_size)
    )
    if exclude_task_id is not None:
        query = query.where(Run.task_id != exclude_task_id)

    results = rows(query)
    if not results:
        return 0.0

    return sum(r.get("total_cost") or 0.0 for r in results) / len(results)


@bound
def check_cost_anomaly(
    db: SqliteDatabase,
    task_id: int,
    cost_usd: float,
    anomaly_threshold: float = 2.0,
    window_size: int = 20,
) -> dict:
    """Detect and log cost anomalies for a task.

    Compares a task's cost against the rolling average. If it exceeds the
    threshold (default 2x), logs a warning event visible in the activity feed.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: ID of the task to check.
        cost_usd: What this task's run cost, as reported by the backend.
        anomaly_threshold: Multiple of rolling average to trigger alert (default 2.0).
        window_size: Number of recent tasks for rolling average (default 20).

    Returns:
        dict: Detection result with keys:
              - is_anomaly (bool): Whether an anomaly was detected.
              - cost_usd (float): Cost for this task.
              - rolling_average (float): Average cost per recent task.
              - multiplier (float): How many times the average this task cost.
              - event_id (int | None): ID of logged warning event, or None if no anomaly.

    Examples:
        >>> result = check_cost_anomaly(db, task_id=42, cost_usd=1.20)
        >>> if result["is_anomaly"]:
        ...     print(f"Anomaly: ${result['cost_usd']:.4f} ({result['multiplier']:.1f}x average)")
    """
    rolling_average = calculate_rolling_cost_average(
        db, window_size=window_size, exclude_task_id=task_id
    )

    result = {
        "is_anomaly": False,
        "cost_usd": cost_usd,
        "rolling_average": rolling_average,
        "multiplier": 0.0,
        "event_id": None,
    }

    # No anomaly if rolling average is 0 (not enough history)
    if rolling_average == 0:
        return result

    multiplier = cost_usd / rolling_average
    result["multiplier"] = multiplier

    if multiplier >= anomaly_threshold:
        result["is_anomaly"] = True
        task = get_task(db, task_id)
        task_title = task["title"] if task else f"Task #{task_id}"

        warning_message = (
            f"Task #{task_id} ({task_title}) cost ${cost_usd:.4f} "
            f"({multiplier:.1f}x average of ${rolling_average:.4f}) - "
            f"check its tool round-trip count and how much it read"
        )

        event_id = log_event(
            db,
            agent="system",
            task_id=task_id,
            run_id=None,
            kind="warning",
            body=warning_message,
            label="cost_anomaly",
        )
        result["event_id"] = event_id

    return result
