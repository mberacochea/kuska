"""Routes: search, run transcripts and stats."""

from __future__ import annotations

from typing import Any

from flask import render_template, request

from .. import eventfmt
from ..export import _fmt_ts
from ..store import (
    avg_task_duration,
    cost_by_task,
    full_text_search,
    list_agents,
    list_tasks,
    longest_tasks,
    recent_runs,
    run_events,
    task_counts_by_agent,
    task_status_counts,
    token_usage_by_agent,
)
from .helpers import RUN_ID, _ago, _clock, _run_status_cost


def register(app, ctx) -> None:
    """Add this area's routes to `app`; `ctx` is the namespace make_context() built."""
    db = ctx.db

    # ========== ROUTES: Search ==========

    @app.get("/search")
    def search_page() -> str:
        """GET /search - Display search page with results."""
        query = request.args.get("q", "").strip()
        page = request.args.get("page", 1, type=int)
        tables_filter = request.args.getlist("table")

        # Validate page number
        page = max(page, 1)

        # All available tables for filtering
        all_tables = ["docs", "messages", "tasks", "events"]
        tables_selected = [t for t in tables_filter if t in all_tables] or all_tables

        # Initialize results
        results = []
        error_msg = None

        # If query provided, validate and search
        if query:
            query_len = len(query)
            if query_len < 2:
                error_msg = "Search query must be at least 2 characters"
            elif query_len > 1000:
                error_msg = "Search query must be no more than 1000 characters"
            else:
                try:
                    limit = 20
                    offset = (page - 1) * limit
                    results = full_text_search(
                        db(),
                        query,
                        tables=tables_selected if tables_selected != all_tables else None,
                        limit=limit,
                        offset=offset
                    )
                except ValueError as e:
                    error_msg = str(e)
                except Exception as e:
                    error_msg = f"Search failed: {e}"

        return render_template(
            "search.html",
            page="search",
            query=query,
            results=results,
            tables_selected=tables_selected,
            all_tables=all_tables,
            current_page=page,
            limit=20,
            error_msg=error_msg,
        )

    # ========== ROUTES: Run Transcripts ==========

    @app.get("/runs")
    def runs_index() -> str:
        """GET /runs - Display the index of recent agent invocations."""
        runs = recent_runs(db(), limit=50)
        return render_template(
            "runs.html",
            page="runs",
            runs=runs,
            ago=_ago,
            _fmt_ts=_fmt_ts,
        )

    @app.get("/runs/<run_id>")
    def run_transcript(run_id: str) -> str:
        """GET /runs/<run_id> - Display a complete invocation transcript.

        A run_id is a 12-hex id set per Monologue (runtime.py:429).
        """
        # Validate run_id format before querying
        if not RUN_ID.fullmatch(run_id):
            return render_template(
                "run.html",
                page="runs",
                run_id=None,
                run=None,
                events=[],
                error="Invalid run ID format",
                ago=_ago,
                summarize=eventfmt.summarize,
                detail_html=eventfmt.detail_html,
                preview_html=eventfmt.preview_html,
                glyph=eventfmt.glyph,
                _fmt_ts=_fmt_ts,
            )

        # Fetch all events for this run
        events = run_events(db(), run_id)

        if not events:
            return render_template(
                "run.html",
                page="runs",
                run_id=run_id,
                run=None,
                events=[],
                error="No run found with this ID",
                ago=_ago,
                summarize=eventfmt.summarize,
                detail_html=eventfmt.detail_html,
                preview_html=eventfmt.preview_html,
                glyph=eventfmt.glyph,
                _fmt_ts=_fmt_ts,
            )

        # Extract run metadata from events
        first_event = events[0]
        last_event = events[-1]
        result_event = next((e for e in events if e.get("kind") == "result"), None)
        status, cost = _run_status_cost(result_event["label"] if result_event else None)

        run_data = {
            "run_id": run_id,
            "agent": first_event.get("agent"),
            "task_id": first_event.get("task_id"),
            "first_ts": first_event.get("ts"),
            "last_ts": last_event.get("ts"),
            "event_count": len(events),
            "status": status,
            "cost": cost,
        }

        return render_template(
            "run.html",
            page="runs",
            run_id=run_id,
            run=run_data,
            events=events,
            error=None,
            ago=_ago,
            clock=_clock,
            summarize=eventfmt.summarize,
            detail_html=eventfmt.detail_html,
            preview_html=eventfmt.preview_html,
            glyph=eventfmt.glyph,
            _fmt_ts=_fmt_ts,
        )

    # ========== ROUTES: Stats Dashboard ==========

    def compute_stats() -> dict[str, Any]:
        """Compute all metrics for the stats dashboard."""
        agents = list_agents(db())
        usage = token_usage_by_agent(db())
        agent_task_counts = {r["assigned_to"]: r for r in task_counts_by_agent(db())}

        # Project overview stats
        status_counts = {r["status"]: r["count"] for r in task_status_counts(db())}
        total_tasks = sum(status_counts.values())
        completed = status_counts.get("done", 0)
        in_progress = status_counts.get("in_progress", 0)
        blocked = status_counts.get("blocked", 0)
        needs_approval = status_counts.get("needs_approval", 0)
        ready_to_merge = status_counts.get("ready_to_merge", 0)
        todo = status_counts.get("todo", 0)
        ready = status_counts.get("ready", 0)

        completed_pct = int((completed / total_tasks * 100) if total_tasks > 0 else 0)

        # Agent productivity stats
        agent_stats = []
        for agent in agents:
            agent_usage = next((u for u in usage if u["agent"] == agent["name"]), None)
            counts = agent_task_counts.get(agent["name"])

            stats = {
                "name": agent["name"],
                "backend": agent["backend"],
                "status": agent["status"],
                "running": agent["running"],
                "completed_tasks": counts["completed_tasks"] if counts else 0,
                "total_tasks": counts["total_tasks"] if counts else 0,
                "turns": agent_usage["turns"] if agent_usage else 0,
                "input_tokens": agent_usage["input_tokens"] if agent_usage else 0,
                "output_tokens": agent_usage["output_tokens"] if agent_usage else 0,
                "cache_read_tokens": agent_usage["cache_read_tokens"] if agent_usage else 0,
                "cache_write_tokens": agent_usage["cache_write_tokens"] if agent_usage else 0,
                "tool_rounds": agent_usage["tool_rounds"] if agent_usage else 0,
                "cost_usd": agent_usage["cost_usd"] if agent_usage else 0.0,
                "avg_cost": (agent_usage["cost_usd"] / agent_usage["turns"]) if (agent_usage and agent_usage["turns"] > 0) else 0.0,
            }
            agent_stats.append(stats)

        # Sort by cost descending
        agent_stats.sort(key=lambda x: x["cost_usd"], reverse=True)

        # Cost and token analysis. Fresh input and cache reads are kept apart
        # because they are priced about 10x differently - summing them produces
        # a number that looks alarming and means nothing.
        total_cost = sum(u["cost_usd"] for u in usage)
        total_input_tokens = sum(u["input_tokens"] for u in usage)
        total_output_tokens = sum(u["output_tokens"] for u in usage)
        total_cache_read_tokens = sum(u["cache_read_tokens"] for u in usage)
        total_cache_write_tokens = sum(u["cache_write_tokens"] for u in usage)
        total_turns = sum(u["turns"] for u in usage)
        total_tool_rounds = sum(u["tool_rounds"] for u in usage)
        cached_in = total_cache_read_tokens + total_input_tokens
        cache_hit_rate = (total_cache_read_tokens / cached_in * 100) if cached_in else 0.0
        cost_per_run = (total_cost / total_turns) if total_turns else 0.0
        rounds_per_run = (total_tool_rounds / total_turns) if total_turns else 0.0

        # Cost per task (for histogram)
        cost_per_task = cost_by_task(db())
        for item in cost_per_task:
            if not item["title"]:
                item["title"] = f"Task {item['task_id']}"

        # Task duration analysis
        longest = longest_tasks(db())
        for t in longest:
            t["duration_hours"] = t["duration"] / 3600
        avg_duration_hours = avg_task_duration(db()) / 3600

        blocked_tasks = list_tasks(db(), status="blocked")
        approval_tasks = list_tasks(db(), status="needs_approval")
        merge_tasks = list_tasks(db(), status="ready_to_merge")

        return {
            "total_tasks": total_tasks,
            "completed": completed,
            "completed_pct": completed_pct,
            "in_progress": in_progress,
            "blocked": blocked,
            "needs_approval": needs_approval,
            "ready_to_merge": ready_to_merge,
            "todo": todo,
            "ready": ready,
            "agent_stats": agent_stats,
            "total_cost": total_cost,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "total_tool_rounds": total_tool_rounds,
            "cache_hit_rate": cache_hit_rate,
            "cost_per_run": cost_per_run,
            "rounds_per_run": rounds_per_run,
            "cost_per_task": cost_per_task,
            "avg_duration_hours": avg_duration_hours,
            "longest_tasks": longest,
            "blocked_tasks": blocked_tasks,
            "approval_tasks": approval_tasks,
            "merge_tasks": merge_tasks,
        }

    @app.get("/stats")
    def stats_page() -> str:
        """GET /stats - Display the project statistics dashboard."""
        metrics = compute_stats()
        return render_template("stats.html", page="stats", metrics=metrics)
