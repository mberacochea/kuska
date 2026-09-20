"""Peewee models - the schema, and the only place SQL is described.

Models are bound to a database per call (`with database.bind_ctx(MODELS)`),
not at import time, because one process can serve several projects: the web
app switches between them and each has its own file.

Every store function still returns plain dicts. The ORM is an implementation
detail of this package, not something the daemons, the web app or the MCP
server have to know about.
"""

from __future__ import annotations

import os
import time

from peewee import (
    AutoField,
    CharField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)


class Base(Model):
    class Meta:
        database = None  # bound per call; see connect()/bind_ctx below


class Agent(Base):
    name = CharField(primary_key=True)
    backend = CharField(null=True)
    role = TextField(null=True)
    status = CharField(default="offline")  # idle | working | offline
    current_task_id = IntegerField(null=True)
    last_heartbeat = FloatField(null=True)

    class Meta:
        table_name = "agents"


class Task(Base):
    id = AutoField()
    title = TextField(null=True)
    description = TextField(null=True)
    assigned_to = ForeignKeyField(
        Agent, field="name", column_name="assigned_to", null=True, backref="tasks",
        on_delete="SET NULL", lazy_load=False,
    )
    status = CharField(default="todo")  # see db.TASK_STATUSES
    created_at = FloatField(default=time.time)
    updated_at = FloatField(default=time.time)

    class Meta:
        table_name = "tasks"


class TaskDep(Base):
    """task waits for depends_on."""

    id = AutoField()
    task = ForeignKeyField(
        Task, column_name="task_id", on_delete="CASCADE", backref="deps", lazy_load=False
    )
    depends_on = ForeignKeyField(
        Task, column_name="depends_on", on_delete="CASCADE", backref="dependents", lazy_load=False
    )

    class Meta:
        table_name = "task_deps"
        indexes = ((("task", "depends_on"), True),)  # one edge per pair


class Message(Base):
    """The audit log and the cost ledger in one table."""

    id = AutoField()
    ts = FloatField(default=time.time)
    sender = CharField(null=True)
    recipient = CharField(null=True)
    task_id = IntegerField(null=True)
    msg_type = CharField(null=True)  # result | question | blocker | note
    payload = TextField(null=True)
    input_tokens = IntegerField(null=True, default=0)  # fresh input, full price
    output_tokens = IntegerField(null=True, default=0)
    cache_read_tokens = IntegerField(null=True, default=0)  # ~10% of input price
    cache_write_tokens = IntegerField(null=True, default=0)  # ~125% of input price
    tool_rounds = IntegerField(null=True, default=0)  # API round-trips in the turn
    cost_usd = FloatField(null=True, default=0.0)
    read_at = FloatField(null=True)  # NULL until the recipient pulls it from its inbox

    class Meta:
        table_name = "messages"
        indexes = ((("recipient", "task_id"), False),)


class FileClaim(Base):
    """"I am touching this file" - advisory, cooperative, and short-lived.

    A claim belongs to one run, so it dies when that invocation ends. If a
    daemon dies without releasing, the claim is ignored once its agent stops
    heartbeating, which is why there is no TTL to tune here.
    """

    id = AutoField()
    path = CharField()  # relative to the project directory; may be a directory
    agent = CharField()
    task_id = IntegerField(null=True)
    run_id = CharField(null=True)
    mode = CharField(default="write")  # write | read
    note = TextField(null=True)
    claimed_at = FloatField(default=time.time)

    class Meta:
        table_name = "file_claims"
        indexes = ((("path",), False), (("agent",), False))


class Doc(Base):
    key = CharField(primary_key=True)
    content = TextField(null=True)
    updated_by = CharField(null=True)
    updated_at = FloatField(default=time.time)

    class Meta:
        table_name = "docs"


class Event(Base):
    """One agent invocation's monologue, kept for audit."""

    id = AutoField()
    ts = FloatField(default=time.time)
    agent = CharField(null=True)
    task_id = IntegerField(null=True)
    run_id = CharField(null=True)
    kind = CharField(null=True)  # see db.EVENT_KINDS
    label = CharField(null=True)
    body = TextField(null=True)

    class Meta:
        table_name = "events"
        indexes = ((("task_id", "id"), False), (("run_id", "id"), False))


MODELS = [Agent, Task, TaskDep, Message, FileClaim, Doc, Event]

PRAGMAS = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": 10000,
}


def connect(db_path: str | os.PathLike) -> SqliteDatabase:
    """Open (or create) one project's database."""
    database = SqliteDatabase(str(db_path), pragmas=PRAGMAS, check_same_thread=False)
    database.connect(reuse_if_open=True)
    return database


def init_db(database: SqliteDatabase) -> None:
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS, safe=True)


def rows(query) -> list[dict]:
    return list(query.dicts())


def row(query) -> dict | None:
    result = query.dicts().first()
    return dict(result) if result else None
