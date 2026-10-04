"""Helpers the web UI's pages share that need no open project: time
formatting, run grouping, form validation and the htmx handle."""

from __future__ import annotations

import itertools
import re
import time
from typing import Any
from urllib.parse import urlencode

from flask import make_response
from flask_htmx import HTMX
from markupsafe import escape
from peewee import SqliteDatabase

from ..db import now
from ..export import _fmt_ts
from ..store import list_agents, list_tasks

# bound to the app in create_app(); reads the current request's HX-* headers
htmx = HTMX()


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = max(0, int(now() - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return _fmt_ts(ts)


def _clock(ts: float | None) -> str:
    """HH:MM:SS for one event's timestamp.

    Individual event log lines used a humanized "12h ago" - fine for a task's
    created/updated column, but relative time on a per-event feed just makes
    old events unreadable ("3d ago" tells you nothing about when in the day
    it happened). The full date is still one hover away via the same title
    attribute these spans already carried.
    """
    if not ts:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(ts))


# a run's terminal `result` event has a label like "done - $5.1009, 103
# rounds, ..." (claude.py) or "done - $0.0312, 40/1217 tok" (codex.py,
# openai.py) - every backend agrees on "<status> - <summary with a $cost>",
# so pulling the status and the first dollar amount out of it is enough to
# read across all three without parsing the rest of the summary.
_RUN_COST = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")


def _run_status_cost(label: str | None) -> tuple[str | None, float | None]:
    """(status, cost) parsed from a `result` event's label, degrading to (None, None)."""
    if not label:
        return None, None
    status = label.split(" - ", 1)[0].strip() or None
    match = _RUN_COST.search(label)
    cost = float(match.group(1)) if match else None
    return status, cost


def _group_runs(events: list[dict]) -> list[dict]:
    """Group a task's chronological events into per-run blocks.

    One agent works a task's runs one at a time, so a task's events already
    arrive in contiguous per-run blocks in id order - `itertools.groupby`
    over the flat list is enough, no need to bucket by run_id first.

    Returns one dict per run: `run_id`, `agent`, `first_ts`, `count`,
    `status`/`cost` (parsed from the run's terminal `result` event, both None
    while the run is still in flight), `events` (the run's events, newest
    first), and `open` (True only for the newest run - the one a human is
    most likely watching right now).

    Runs and, within each run, events are returned newest-first, since a
    human scanning a task's activity cares about what just happened.
    """
    groups: list[dict] = []
    for run_id, members in itertools.groupby(events, key=lambda e: e.get("run_id")):
        members = list(members)
        result = next((e for e in members if e.get("kind") == "result"), None)
        status, cost = _run_status_cost(result["label"] if result else None)
        groups.append(
            {
                "run_id": run_id,
                "agent": members[0].get("agent"),
                "first_ts": members[0].get("ts"),
                "count": len(members),
                "status": status,
                "cost": cost,
                "events": members,
                "open": False,
            }
        )
    if groups:
        groups[-1]["open"] = True
    groups.reverse()
    for g in groups:
        g["events"] = list(reversed(g["events"]))
    return groups


def _activity_qs(agent: str, kind: str, show_system: bool) -> str:
    """Query string for the current fleet-tail filter state, or "" when unfiltered.

    Rendered onto the tail's own `hx-get` so its 3s self-poll keeps re-asking
    with the same filters instead of silently resetting them - and, when no
    filter is set, this returns "" so the attribute stays the bare
    `/agents/activity` the tests (and a plain first load) expect.
    """
    params = [(k, v) for k, v in (("agent", agent), ("kind", kind)) if v]
    if show_system:
        params.append(("system", "1"))
    return f"?{urlencode(params)}" if params else ""


# agent names become TOML keys and prompt filenames, so keep them plain
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BACKENDS = ("claude", "codex", "openai")
DOC_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# run_id is a 12-hex id set per Monologue (runtime.py:429)
RUN_ID = re.compile(r"[0-9a-f]{12}")

# Validation limits
MAX_TASK_TITLE_LEN = 200
MAX_TASK_DESC_LEN = 10000
MAX_DOC_CONTENT_LEN = 100000
MAX_AGENT_ROLE_LEN = 500


def _error_response(field: str, message: str) -> dict[str, Any]:
    """Return a structured error response for form validation."""
    return {"error": True, "field": field, "message": message}


def _field_error_html(field: str, message: str) -> str:
    """Return HTML for a form field error."""
    return f'<div class="field-error" role="alert" aria-live="polite" data-field="{escape(field)}">{escape(message)}</div>'


def _bad_request(fragment: str, field: str, message: str):
    """422 response: the fragment re-rendered as-is, plus a field error for htmx to display."""
    return make_response(fragment + _field_error_html(field, message), 422)


def wants_fragment() -> bool:
    """True when htmx is asking a page route for a fragment to swap into an element.

    A page route serves two audiences: a browser navigating to the URL, which
    needs the whole document, and htmx swapping part of the page in place,
    which needs only the fragment its hx-target expects. Handing the full page
    to an htmx swap is what nests a second copy of a table inside one of its
    own rows.

    htmx marks its requests with HX-Request, with one exception that matters
    here: on back/forward it may re-request the URL with
    HX-History-Restore-Request set, and it then replaces the entire body with
    whatever comes back. That case wants the full page, so it is excluded.
    """
    return bool(htmx) and not htmx.history_restore_request


def validate_task_title(title: str, db_handle: SqliteDatabase, task_id: int | None = None) -> str | None:
    """Validate task title. Returns error message if invalid, None if valid."""
    title = (title or "").strip()
    if not title:
        return "Title is required"
    if len(title) > MAX_TASK_TITLE_LEN:
        return f"Title is too long (max {MAX_TASK_TITLE_LEN} characters)"
    # Check for duplicates
    existing = [t for t in list_tasks(db_handle) if t["title"] == title]
    if existing and (not task_id or existing[0]["id"] != task_id):
        return "Title already exists. Try adding a suffix like '_v2'"
    return None


def validate_task_description(description: str) -> str | None:
    """Validate task description. Returns error message if invalid, None if valid."""
    if description and len(description) > MAX_TASK_DESC_LEN:
        return f"Description is too long (max {MAX_TASK_DESC_LEN} characters)"
    return None


def validate_task_assigned_to(assigned_to: str | None, db_handle: SqliteDatabase) -> str | None:
    """Validate assigned_to field. Returns error message if invalid, None if valid."""
    if not assigned_to:
        return None
    agent_names = {a["name"] for a in list_agents(db_handle)}
    if assigned_to not in agent_names:
        return f"Agent '{assigned_to}' does not exist"
    return None


def validate_agent_name(name: str, existing_agents: set[str]) -> str | None:
    """Validate agent name format. Returns error message if invalid, None if valid."""
    name = (name or "").strip()
    if not name:
        return "Agent name is required"
    if not AGENT_NAME.fullmatch(name):
        return "Agent name must start with alphanumeric and contain only alphanumeric, dash, underscore, or dot"
    if name in existing_agents:
        return f"Agent '{name}' already exists"
    return None


def validate_agent_backend(backend: str) -> str | None:
    """Validate agent backend. Returns error message if invalid, None if valid."""
    backend = (backend or "").strip()
    if not backend:
        return "Backend is required"
    if backend not in BACKENDS:
        return f"Backend must be one of: {', '.join(BACKENDS)}"
    return None


def validate_agent_model(model: str) -> str | None:
    """Validate agent model. Returns error message if invalid, None if valid."""
    model = (model or "").strip()
    if not model:
        return "Model is required"
    return None


def validate_agent_role(role: str) -> str | None:
    """Validate agent role. Returns error message if invalid, None if valid."""
    role = (role or "").strip()
    if not role:
        return "Role is required"
    if len(role) > MAX_AGENT_ROLE_LEN:
        return f"Role is too long (max {MAX_AGENT_ROLE_LEN} characters)"
    return None


def validate_agent_prices(form_data: dict) -> str | None:
    """Validate agent price values. Returns error message if invalid, None if valid."""
    for key in ("price_in_per_mtok", "price_out_per_mtok"):
        if form_data.get(key):
            try:
                value = float(form_data[key])
                if value < 0:
                    return f"{key} must be non-negative"
            except (ValueError, TypeError):
                return f"{key} must be a valid number"
    return None


def validate_doc_key(key: str, existing_docs: set[str]) -> str | None:
    """Validate doc key. Returns error message if invalid, None if valid."""
    key = (key or "").strip()
    if not key:
        return "Doc key is required"
    if not DOC_KEY.fullmatch(key):
        return "Doc key must start with alphanumeric and contain only alphanumeric, dash, or underscore"
    if key in existing_docs:
        return f"Doc '{key}' already exists"
    return None


def validate_doc_content(content: str) -> str | None:
    """Validate doc content. Returns error message if invalid, None if valid."""
    if len(content) > MAX_DOC_CONTENT_LEN:
        return f"Content is too long (max {MAX_DOC_CONTENT_LEN} characters)"
    return None


def task_filters(args) -> dict[str, Any]:
    """The task filter fields in `args` (a query string or a posted form).

    One parser for everything that filters tasks - the Tasks page, its bulk
    move and the board - so they all take the same parameter names.
    """
    return {
        "search": args.get("search", "").strip(),
        "status": args.getlist("status"),
        "agent": args.getlist("agent"),
        "feature": args.getlist("feature"),
        "tag": args.getlist("tag"),
        "sort": args.get("sort") or None,
        "direction": args.get("direction", "asc"),
    }


def board_cols(args) -> int:
    """The board's sub-columns per column: 1-4, anything else is 2."""
    value = args.get("cols", "")
    return int(value) if value in ("1", "2", "3", "4") else 2
