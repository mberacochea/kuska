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

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only
    fcntl = None

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


class Feature(Base):
    """A named group of related tasks, e.g. "run-ledger". A task belongs to at
    most one; the feature outlives its tasks and is the unit later per-feature
    work (agent coordination, summaries) will hang off."""

    id = AutoField()
    name = CharField(unique=True)  # normalised by store.features.norm_feature_name
    description = TextField(null=True)
    created_at = FloatField(default=time.time)
    updated_at = FloatField(default=time.time)

    class Meta:
        table_name = "features"


class Task(Base):
    id = AutoField()
    title = TextField(null=True)
    description = TextField(null=True)
    assigned_to = ForeignKeyField(
        Agent, field="name", column_name="assigned_to", null=True, backref="tasks",
        on_delete="SET NULL", lazy_load=False,
    )
    status = CharField(default="todo")  # see db.TASK_STATUSES
    kind = CharField(default="work")  # see db.TASK_KINDS; fixed at creation
    # the feature this task belongs to; deleting the feature ungroups its tasks.
    # Task dicts from store.tasks also carry the feature's name as "feature".
    feature_id = ForeignKeyField(
        Feature, field="id", column_name="feature_id", null=True, backref="tasks",
        on_delete="SET NULL", lazy_load=False,
    )
    tags = TextField(null=True)  # comma-separated tags for filtering and grouping
    worktree_path = TextField(null=True)  # path to the worktree, if one exists
    worktree_base_sha = TextField(null=True)  # commit the task branch started from
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


class Doc(Base):
    key = CharField(primary_key=True)
    content = TextField(null=True)
    updated_by = CharField(null=True)
    updated_at = FloatField(default=time.time)
    # optional link to the task this doc belongs to (e.g. a plan or a
    # handover report); NULL for project-wide docs like "architecture".
    # dies with its task (ON DELETE CASCADE) rather than becoming orphaned.
    task_id = ForeignKeyField(
        Task, field="id", column_name="task_id", null=True, backref="docs",
        on_delete="CASCADE", lazy_load=False,
    )

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


class Run(Base):
    """One agent invocation on one task: its status, heartbeat and own usage.

    task_id is a plain integer like Event.task_id (no FK), so deleting a task
    keeps its runs for the ledger. `id` is the 12-hex run id that
    runtime.Monologue generates and events.run_id carries."""

    id = CharField(primary_key=True)
    task_id = IntegerField(null=True, index=True)
    agent = CharField(null=True)
    status = CharField(default="running", index=True)  # see db.RUN_STATUSES
    exit_reason = TextField(null=True)
    started_at = FloatField(default=time.time)
    heartbeat_at = FloatField(default=time.time)  # a running run that stops touching this has crashed
    ended_at = FloatField(null=True)
    input_tokens = IntegerField(default=0)
    output_tokens = IntegerField(default=0)
    cache_read_tokens = IntegerField(default=0)
    cache_write_tokens = IntegerField(default=0)
    tool_rounds = IntegerField(default=0)
    cost_usd = FloatField(default=0.0)
    result_message_id = IntegerField(null=True)

    class Meta:
        table_name = "runs"


MODELS = [Agent, Feature, Task, TaskDep, Message, Doc, Event, Run]

# per connection. WAL is not among them: it is a property of the file, set
# once by connect() - see _ensure_wal
PRAGMAS = {
    "busy_timeout": 10000,
    "foreign_keys": 1,
}


class FileLock:
    """An exclusive cross-process lock on a sidecar file (POSIX flock).

    For the few moments that must not overlap between processes opening the
    same database - switching it to WAL, running migrations - and nothing
    else: ordinary writes are serialised by SQLite itself."""

    def __init__(self, path: str):
        self._path = path if path and ":memory:" not in path else ""
        self._fd = None

    def __enter__(self):
        if fcntl is None or not self._path:
            return self
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _ensure_wal(database: SqliteDatabase, db_path: str) -> None:
    """Switch the file to WAL once, under a lock.

    The switch needs an exclusive lock, and SQLite does not wait for that one
    - busy_timeout or not, it answers "database is locked" at once to avoid a
    deadlock. So processes starting together (run-all) take turns; the mode
    sticks to the file, and everyone after the first finds it already set."""
    if database.execute_sql("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    with FileLock(f"{db_path}.init.lock"):
        database.execute_sql("PRAGMA journal_mode=wal")




def connect(db_path: str | os.PathLike) -> SqliteDatabase:
    """Open (or create) one project's database.

    WAL mode (journal_mode) provides concurrent readers alongside one writer.
    busy_timeout makes a blocked writer wait rather than fail immediately.
    These pragmas replace the old cross-process write lock, which was both
    incomplete (only wrapped individual statements, not transactions) and
    harmful (serialized the entire fleet through one mutex).
    """
    database = SqliteDatabase(str(db_path), pragmas=PRAGMAS, check_same_thread=False)
    database.connect(reuse_if_open=True)
    _ensure_wal(database, str(db_path))
    return database


def init_db(database: SqliteDatabase) -> None:
    with database.bind_ctx(MODELS):
        database.create_tables(MODELS, safe=True)


def rows(query) -> list[dict]:
    return list(query.dicts())


def row(query) -> dict | None:
    result = query.dicts().first()
    return dict(result) if result else None
