"""Markdown export - SQLite is the source of truth, this makes it git-friendly."""

from __future__ import annotations

import os
import time
from pathlib import Path

from peewee import SqliteDatabase

from .store import (
    docs_list,
    list_tasks,
    task_dependencies,
    task_events,
    task_messages,
    token_usage_by_agent,
)


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def export_markdown(db: SqliteDatabase, out_dir: str | os.PathLike) -> list[Path]:
    """Write plan.md, tasks.md and messages/task-<id>.md. Returns written paths."""
    out = Path(out_dir)
    (out / "messages").mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    plan = ["# Project plan", ""]
    for doc in docs_list(db):
        plan += [
            f"## {doc['key']}",
            "",
            doc["content"] or "",
            "",
            f"_updated by {doc['updated_by']} at {_fmt_ts(doc['updated_at'])}_",
            "",
        ]
    plan_file = out / "plan.md"
    plan_file.write_text("\n".join(plan))
    written.append(plan_file)

    tasks = ["# Tasks", "", "| # | Title | Assigned | Status | Updated |", "| --- | --- | --- | --- | --- |"]
    for t in list_tasks(db):
        tasks.append(
            f"| {t['id']} | {t['title']} | {t['assigned_to'] or '-'} | {t['status']} | {_fmt_ts(t['updated_at'])} |"
        )
    tasks.append("")
    for t in list_tasks(db):
        tasks += [f"## {t['id']}. {t['title']}", ""]
        deps = task_dependencies(db, t["id"])
        if deps:
            tasks += [
                "Depends on: " + ", ".join(f"#{d['id']} ({d['status']})" for d in deps),
                "",
            ]
        tasks += [t["description"] or "_no description_", ""]
    tasks_file = out / "tasks.md"
    tasks_file.write_text("\n".join(tasks))
    written.append(tasks_file)

    for t in list_tasks(db):
        msgs = task_messages(db, t["id"])
        events = task_events(db, t["id"])
        if not msgs and not events:
            continue
        lines = [f"# Task {t['id']}: {t['title']}", "", f"Status: {t['status']}", ""]
        for m in msgs:
            lines += [
                f"## {m['sender']} -> {m['recipient']} ({m['msg_type']}) - {_fmt_ts(m['ts'])}",
                "",
                m["payload"] or "",
                "",
            ]
            if m["input_tokens"] or m["output_tokens"] or m["cost_usd"]:
                lines += [
                    f"_tokens in/out: {m['input_tokens'] or 0}/{m['output_tokens'] or 0}"
                    f" - cost: ${m['cost_usd'] or 0:.4f}_",
                    "",
                ]
        if events:
            # the agent monologue: verbose on purpose, this is the audit trail
            lines += ["## Activity", ""]
            run = None
            for e in events:
                if e["run_id"] != run:
                    run = e["run_id"]
                    lines += [f"### run {run} - {e['agent']} - {_fmt_ts(e['ts'])}", ""]
                label = f" {e['label']}" if e["label"] else ""
                lines += [
                    f"**{e['kind']}{label}** - {_fmt_ts(e['ts'])}",
                    "",
                    "```",
                    (e["body"] or "").replace("```", "'''"),
                    "```",
                    "",
                ]

        msg_file = out / "messages" / f"task-{t['id']}.md"
        msg_file.write_text("\n".join(lines))
        written.append(msg_file)

    usage = token_usage_by_agent(db)
    if usage:
        lines = ["# Token usage", "", "| Agent | Turns | In | Out | Cost |", "| --- | --- | --- | --- | --- |"]
        for u in usage:
            lines.append(
                f"| {u['agent']} | {u['turns']} | {u['input_tokens']} | {u['output_tokens']} | ${u['cost_usd']:.4f} |"
            )
        usage_file = out / "usage.md"
        usage_file.write_text("\n".join(lines) + "\n")
        written.append(usage_file)

    return written
