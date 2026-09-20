"""Initial schema - mirrors current Peewee models.

The models below are copies, so they can drift from the real ones in models.py.
They did: assigned_to, task_id and depends_on were declared as plain columns
with a comment saying "FK to ...", which is not a foreign key. Every project
created after the migration runner landed therefore had no referential
integrity on tasks or task_deps at all. Migration 004 repairs databases that
were built from the broken version. Keep these declarations in step with
models.py - a comment is not a constraint.

This migration creates the complete schema that the application currently
expects: agents, tasks, task_deps, messages, file_claims, docs, and events tables.

Uses peewee's ORM-based model definitions for schema management - the proper
peewee approach instead of raw SQL strings.
"""
from peewee import *


# Define models for schema creation - these mirror the production models
# but are isolated to this migration
class Agent(Model):
    name = CharField(primary_key=True)
    backend = CharField(null=True)
    role = TextField(null=True)
    status = CharField(default='offline')
    current_task_id = IntegerField(null=True)
    last_heartbeat = FloatField(null=True)

    class Meta:
        table_name = 'agents'


class Task(Model):
    id = AutoField()
    title = TextField(null=True)
    description = TextField(null=True)
    assigned_to = ForeignKeyField(
        Agent, field='name', column_name='assigned_to', null=True,
        backref='tasks', on_delete='SET NULL', lazy_load=False,
    )
    status = CharField(default='todo')
    created_at = FloatField()
    updated_at = FloatField()

    class Meta:
        table_name = 'tasks'


class TaskDep(Model):
    id = AutoField()
    task = ForeignKeyField(
        Task, column_name='task_id', on_delete='CASCADE', backref='deps', lazy_load=False
    )
    depends_on = ForeignKeyField(
        Task, column_name='depends_on', on_delete='CASCADE', backref='dependents', lazy_load=False
    )

    class Meta:
        table_name = 'task_deps'
        indexes = ((('task', 'depends_on'), True),)  # unique constraint


class Message(Model):
    id = AutoField()
    ts = FloatField()
    sender = CharField(null=True)
    recipient = CharField(null=True)
    task_id = IntegerField(null=True)
    msg_type = CharField(null=True)
    payload = TextField(null=True)
    input_tokens = IntegerField(default=0, null=True)
    output_tokens = IntegerField(default=0, null=True)
    cost_usd = FloatField(default=0.0, null=True)
    read_at = FloatField(null=True)

    class Meta:
        table_name = 'messages'
        indexes = ((('recipient', 'task_id'), False),)


class FileClaim(Model):
    id = AutoField()
    path = CharField()
    agent = CharField()
    task_id = IntegerField(null=True)
    run_id = CharField(null=True)
    mode = CharField(default='write')
    note = TextField(null=True)
    claimed_at = FloatField()

    class Meta:
        table_name = 'file_claims'
        indexes = ((('path',), False), (('agent',), False))


class Doc(Model):
    key = CharField(primary_key=True)
    content = TextField(null=True)
    updated_by = CharField(null=True)
    updated_at = FloatField()

    class Meta:
        table_name = 'docs'


class Event(Model):
    id = AutoField()
    ts = FloatField()
    agent = CharField(null=True)
    task_id = IntegerField(null=True)
    run_id = CharField(null=True)
    kind = CharField(null=True)
    label = CharField(null=True)
    body = TextField(null=True)

    class Meta:
        table_name = 'events'
        indexes = ((('task_id', 'id'), False), (('run_id', 'id'), False))


def up(migrator, db):
    """Create the initial schema tables using peewee's ORM model definitions."""
    # Bind all models to this database
    models = [Agent, Task, TaskDep, Message, FileClaim, Doc, Event]
    for model in models:
        model._meta.database = db

    # Create all tables using peewee's ORM
    db.create_tables(models)


def down(migrator, db):
    """Drop all schema tables in reverse dependency order.

    Note: This is destructive and removes all data. In production, down()
    migrations are often skipped or documented as destructive.
    """
    # Drop tables in reverse order of dependencies
    models = [Event, Doc, FileClaim, Message, TaskDep, Task, Agent]
    for model in models:
        model._meta.database = db

    db.drop_tables(models)
