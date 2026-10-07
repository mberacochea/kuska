"""Every model in the DB, described once, so one set of routes can browse them all.

The web UI has purpose-built pages for the things humans work with daily -
tasks, agents, docs, runs. This is the other half: a read-only browser for
every model, including the ones nothing else exposes (messages, events, the
dependency edges). Field lists come from the peewee models, so a column added
there shows up here without being named twice. Nothing here writes.
"""

from __future__ import annotations

from typing import Any

from peewee import (
    AutoField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    SqliteDatabase,
    TextField,
)

from .models import (
    Agent,
    Doc,
    Event,
    Feature,
    Message,
    Run,
    Task,
    TaskDep,
    TaskTag,
    row,
    rows,
    using,
)

# per-table presentation and policy; the columns themselves come from the model
TABLES: dict[str, dict[str, Any]] = {
    "tasks": {
        "label": "Tasks",
        "model": Task,
        "order": lambda m: m.id.desc(),
    },
    "features": {
        "label": "Features",
        "model": Feature,
        "order": lambda m: m.name,
        "note": "names are stored lowercased",
    },
    "task_deps": {
        "label": "Dependencies",
        "model": TaskDep,
        "order": lambda m: m.task,
        "note": "task waits for depends_on - a task only runs once every task it depends on is done",
    },
    "task_tags": {
        "label": "Tags",
        "model": TaskTag,
        "order": lambda m: m.task,
    },
    "agents": {
        "label": "Agents",
        "model": Agent,
        "order": lambda m: m.name,
        "note": "config.toml is the registry; daemons keep only last_heartbeat here - the Agents page derives status from runs",
    },
    "messages": {
        "label": "Messages",
        "model": Message,
        "order": lambda m: m.id.desc(),
        "note": "the audit log and the cost ledger",
    },
    "docs": {
        "label": "Docs",
        "model": Doc,
        "order": lambda m: m.key,
    },
    "events": {
        "label": "Events",
        "model": Event,
        "order": lambda m: m.id.desc(),
        "note": "the agent monologue, append-only",
    },
    "runs": {
        "label": "Runs",
        "model": Run,
        "order": lambda m: m.started_at.desc(),
        "note": "one row per agent invocation, written by the daemon",
    },
}

# text columns holding markdown, rendered as such wherever a row is shown
MARKDOWN_COLUMNS = frozenset({"description", "payload", "body", "content"})

# columns whose name says they hold a timestamp, rendered as "3m ago"
TS_COLUMNS = ("ts", "created_at", "updated_at", "last_heartbeat", "read_at", "claimed_at",
              "started_at", "heartbeat_at", "ended_at")


# columns that name a task / an agent / a run, and so can link to its page
_TASK_COLUMNS = ("task_id", "task", "depends_on")
_AGENT_COLUMNS = ("agent", "assigned_to", "sender", "recipient")

# tables whose rows have a page of their own besides /data/<table>/<pk>
_OWN_PAGE = {"tasks": "/tasks/{}", "agents": "/agents/{}", "docs": "/docs/{}", "runs": "/runs/{}"}


def link_for(column: str, value: Any) -> str | None:
    """The canonical page a foreign-key-like column value points at, if it has one."""
    if value in (None, ""):
        return None
    if column in _TASK_COLUMNS:
        return f"/tasks/{value}"
    if column in _AGENT_COLUMNS and value != "human":
        return f"/agents/{value}"
    if column == "run_id":
        return f"/runs/{value}"
    return None


def own_page(table: str, pk_value: Any) -> str | None:
    """The purpose-built page for a row of tasks, agents, docs or runs."""
    pattern = _OWN_PAGE.get(table)
    return pattern.format(pk_value) if pattern else None


def spec(table: str) -> dict:
    if table not in TABLES:
        raise KeyError(f"unknown table: {table}")
    return TABLES[table]


def model_of(table: str):
    return spec(table)["model"]


def _kind(name: str, field) -> str:
    if name in TS_COLUMNS:
        return "ts"
    if isinstance(field, ForeignKeyField):
        # a foreign key is whatever the column it points at is
        return _kind(name, field.rel_field)
    if isinstance(field, (AutoField, IntegerField)):
        return "int"
    if isinstance(field, FloatField):
        return "real"
    if isinstance(field, TextField) and name in MARKDOWN_COLUMNS:
        return "longtext"
    return "text"


def fields(table: str) -> list[tuple[str, str]]:
    """(name, kind) for every column of this table, in declaration order."""
    return [(f.name, _kind(f.name, f)) for f in model_of(table)._meta.sorted_fields]


def field_types(table: str) -> dict[str, str]:
    return dict(fields(table))


def pk_name(table: str) -> str:
    return model_of(table)._meta.primary_key.name


def _pk_expression(table: str, pk_value: Any):
    """`pk = value`, with the value typed the way the pk column is; None if it can't be."""
    name = pk_name(table)
    kind = field_types(table)[name]
    try:
        value = int(pk_value) if kind == "int" else str(pk_value)
    except (TypeError, ValueError):
        return None
    return getattr(model_of(table), name) == value


def count_rows(db: SqliteDatabase, table: str) -> int:
    with using(db):
        return int(model_of(table).select().count())


def list_rows(db: SqliteDatabase, table: str, limit: int = 50, offset: int = 0) -> list[dict]:
    model = model_of(table)
    with using(db):
        query = model.select().order_by(spec(table)["order"](model)).limit(limit).offset(offset)
        return rows(query)


def get_row(db: SqliteDatabase, table: str, pk_value: Any) -> dict | None:
    with using(db):
        where = _pk_expression(table, pk_value)
        return row(model_of(table).select().where(where)) if where is not None else None
