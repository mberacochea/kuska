"""Docs: the shared project knowledge agents and humans read and write."""

from __future__ import annotations

from peewee import SqliteDatabase

from ..db import HUMAN, now
from ..models import Doc, DocTask, row, rows
from .common import bound

_UNSET = object()  # docs_set's task_id sentinel: "leave the link as it is"


@bound
def docs_get(db: SqliteDatabase, key: str, task_id: int | None = None) -> str | None:
    """Fetch a shared project knowledge document.

    Agents and humans write documentation here that persists across invocations.
    Used for storing project context, architecture, decisions, etc.

    Args:
        db: SqliteDatabase instance for this project.
        key: Document identifier (e.g., "architecture", "conventions").
        task_id: If given, the doc must be linked to this task - a doc with
            no link, or linked to a different task, returns None instead of
            its content. Omit for project-wide docs (e.g. "architecture"),
            or when the key alone is enough to identify the doc.

    Returns:
        str: Document content, or None if not found or not linked to task_id.

    Examples:
        >>> arch = docs_get(db, "architecture")
        >>> if arch:
        ...     print("Architecture notes:", arch)
        >>> plan = docs_get(db, "task_42_planning-agent_context", task_id=42)
    """
    doc = row(Doc.select(Doc.content).where(Doc.key == key))
    if not doc:
        return None
    if task_id is not None and not DocTask.select().where(
        DocTask.doc == key, DocTask.task == task_id
    ).exists():
        return None
    return doc["content"]


@bound
def docs_set(
    db: SqliteDatabase,
    key: str,
    content: str,
    updated_by: str = HUMAN,
    task_id: int | None = _UNSET,
    task_ids: list[int] | None = None,
) -> None:
    """Write or update a shared project knowledge document.

    Creates a new document or replaces an existing one. Tracks who updated
    the document and when.

    Args:
        db: SqliteDatabase instance for this project.
        key: Document identifier.
        content: Document text (markdown, JSON, or any format).
        updated_by: Who is updating this (default "human").
        task_id: Task this doc belongs to (e.g. a plan or handover report);
            the doc is deleted when that task is. Omit to leave an existing
            doc's link untouched, or pass None to explicitly clear it - a
            bare positional call never touches the link. Links are
            additive: linking a second task keeps the first.
        task_ids: More tasks to link this doc to (many-to-many).

    Examples:
        >>> docs_set(db, "conventions", "# Code Conventions\\n\\n- Use snake_case...",
        ...          updated_by="claude-reviewer")
        >>> docs_set(db, "task_42_context", "# Plan for task 42...", "planning-agent", task_id=42)
    """
    # Use insert().on_conflict() instead of .replace() to ensure the UPDATE trigger
    # fires on the FTS5 index. INSERT OR REPLACE only fires the DELETE trigger if
    # PRAGMA recursive_triggers is ON (it defaults OFF), leaving orphaned index entries.
    # See migration 005's docs_fts_update for the trigger that this must invoke.
    now_val = now()
    fields = {"key": key, "content": content, "updated_by": updated_by, "updated_at": now_val}
    update = {Doc.content: content, Doc.updated_by: updated_by, Doc.updated_at: now_val}
    if task_id is not _UNSET:
        fields["task_id"] = task_id
        update[Doc.task_id] = task_id
    Doc.insert(**fields).on_conflict(conflict_target=[Doc.key], update=update).execute()
    if task_id is None:
        DocTask.delete().where(DocTask.doc == key).execute()
    wanted = ([task_id] if task_id not in (None, _UNSET) else []) + list(task_ids or [])
    for tid in wanted:
        docs_link(db, key, tid)


@bound
def docs_link(db: SqliteDatabase, key: str, task_id: int) -> None:
    """Link an existing doc to a task (idempotent)."""
    DocTask.insert(doc=key, task=task_id).on_conflict_ignore().execute()


@bound
def docs_unlink(db: SqliteDatabase, key: str, task_id: int) -> None:
    """Remove the link between a doc and a task, if any."""
    DocTask.delete().where(DocTask.doc == key, DocTask.task == task_id).execute()


@bound
def docs_list(db: SqliteDatabase, task_id: int | None = None) -> list[dict]:
    """List shared project documents, ordered by key.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: If given, only docs linked to this task.

    Returns:
        list[dict]: Document records with keys: key, content, updated_by,
                    updated_at, task_id, task_ids (every linked task).
    """
    query = Doc.select().order_by(Doc.key)
    if task_id is not None:
        query = query.where(
            Doc.key.in_(DocTask.select(DocTask.doc).where(DocTask.task == task_id))
        )
    result = rows(query)
    links: dict[str, list[int]] = {}
    for link in rows(DocTask.select(DocTask.doc, DocTask.task).order_by(DocTask.task)):
        links.setdefault(link["doc"], []).append(link["task"])
    for d in result:
        d["task_ids"] = links.get(d["key"], [])
    return result
