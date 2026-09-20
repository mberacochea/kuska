"""Every model in the DB, described once, so one set of routes can edit them all.

The web UI has purpose-built pages for the things humans work with daily -
tasks, agents, docs. This is the other half: a plain row editor for every
model, including the ones nothing else exposes (messages, events, the
dependency edges). Field lists come from the peewee models, so a column added
there shows up here without being named twice.
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

from .db import now
from .models import (
    MODELS,
    Agent,
    Doc,
    Event,
    FileClaim,
    Message,
    Task,
    TaskDep,
    row,
    rows,
)

# per-table presentation and policy; the columns themselves come from the model
TABLES: dict[str, dict[str, Any]] = {
    "tasks": {
        "label": "Tasks",
        "model": Task,
        "order": lambda m: m.id.desc(),
        "editable": ["title", "description", "assigned_to", "status"],
        "insertable": ["title", "description", "assigned_to", "status"],
    },
    "task_deps": {
        "label": "Dependencies",
        "model": TaskDep,
        "order": lambda m: m.task,
        "editable": ["task", "depends_on"],
        "insertable": ["task", "depends_on"],
        "note": "task waits for depends_on - a task only runs once every task it depends on is done",
    },
    "agents": {
        "label": "Agents",
        "model": Agent,
        "order": lambda m: m.name,
        "editable": ["backend", "role", "status", "current_task_id"],
        "insertable": ["name", "backend", "role", "status"],
        "note": "config.toml is the registry - edits here are not written back to it",
    },
    "messages": {
        "label": "Messages",
        "model": Message,
        "order": lambda m: m.id.desc(),
        "editable": [
            "sender", "recipient", "task_id", "msg_type", "payload",
            "input_tokens", "output_tokens", "cost_usd", "read_at",
        ],
        "insertable": ["sender", "recipient", "task_id", "msg_type", "payload"],
        "note": "this is the audit log and the cost ledger - edit with care",
    },
    "file_claims": {
        "label": "File claims",
        "model": FileClaim,
        "order": lambda m: m.claimed_at.desc(),
        "editable": ["path", "agent", "task_id", "mode", "note"],
        "insertable": ["path", "agent", "task_id", "run_id", "mode", "note"],
        "note": "advisory: who says they are touching what, released when a run ends",
    },
    "docs": {
        "label": "Docs",
        "model": Doc,
        "order": lambda m: m.key,
        "editable": ["content", "updated_by"],
        "insertable": ["key", "content", "updated_by"],
    },
    "events": {
        "label": "Events",
        "model": Event,
        "order": lambda m: m.id.desc(),
        "editable": ["kind", "label", "body"],
        "insertable": ["agent", "task_id", "run_id", "kind", "label", "body"],
        "note": "the agent monologue - normally append-only, deletable here to prune",
    },
}

# columns whose name says they hold a timestamp, rendered as "3m ago"
TS_COLUMNS = ("ts", "created_at", "updated_at", "last_heartbeat", "read_at", "claimed_at")


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
    if isinstance(field, TextField) and name in ("description", "payload", "body", "content"):
        return "longtext"
    return "text"


def fields(table: str) -> list[tuple[str, str]]:
    """(name, kind) for every column of this table, in declaration order."""
    return [(f.name, _kind(f.name, f)) for f in model_of(table)._meta.sorted_fields]


def field_types(table: str) -> dict[str, str]:
    return dict(fields(table))


def pk_name(table: str) -> str:
    return model_of(table)._meta.primary_key.name


def coerce(table: str, column: str, raw: Any) -> Any:
    """Form values arrive as strings; give the model the type it declares."""
    kind = field_types(table).get(column, "text")
    if isinstance(raw, str):
        raw = raw.strip()
    if raw in ("", None):
        return None
    if kind == "int":
        return int(raw)
    if kind in ("real", "ts"):
        return float(raw)
    return raw


def _pk_expression(table: str, pk_value: Any):
    name = pk_name(table)
    return getattr(model_of(table), name) == coerce(table, name, pk_value)


def _values(table: str, values: dict, allowed: list[str]) -> dict:
    return {k: coerce(table, k, v) for k, v in values.items() if k in allowed}


def count_rows(db: SqliteDatabase, table: str) -> int:
    with db.bind_ctx(MODELS):
        return int(model_of(table).select().count())


def list_rows(db: SqliteDatabase, table: str, limit: int = 50, offset: int = 0) -> list[dict]:
    model = model_of(table)
    with db.bind_ctx(MODELS):
        query = model.select().order_by(spec(table)["order"](model)).limit(limit).offset(offset)
        return rows(query)


def get_row(db: SqliteDatabase, table: str, pk_value: Any) -> dict | None:
    with db.bind_ctx(MODELS):
        return row(model_of(table).select().where(_pk_expression(table, pk_value)))


def insert_row(db: SqliteDatabase, table: str, values: dict) -> Any:
    model = model_of(table)
    columns = {k: v for k, v in _values(table, values, spec(table)["insertable"]).items() if v is not None}
    if not columns:
        raise ValueError("nothing to insert")
    types = field_types(table)
    for stamp in ("created_at", "updated_at", "ts"):
        if stamp in types:
            columns.setdefault(stamp, now())
    with db.bind_ctx(MODELS):
        inserted = model.insert(**columns).execute()
    name = pk_name(table)
    return columns.get(name, inserted)


def update_row(db: SqliteDatabase, table: str, pk_value: Any, values: dict) -> bool:
    model = model_of(table)
    columns = _values(table, values, spec(table)["editable"])
    if not columns:
        return False
    if "updated_at" in field_types(table):
        columns["updated_at"] = now()
    with db.bind_ctx(MODELS):
        return bool(model.update(**columns).where(_pk_expression(table, pk_value)).execute())


def delete_row(db: SqliteDatabase, table: str, pk_value: Any) -> bool:
    model = model_of(table)
    with db.bind_ctx(MODELS):
        return bool(model.delete().where(_pk_expression(table, pk_value)).execute())
