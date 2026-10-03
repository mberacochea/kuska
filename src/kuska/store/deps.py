"""Task dependencies: what waits on what, with cycles refused."""

from __future__ import annotations

from peewee import SqliteDatabase

from ..models import Task, TaskDep, rows
from .common import bound


def _reaches(db: SqliteDatabase, start: int, target: int) -> bool:
    """Does `start` reach `target` by following dependency edges?

    A recursive CTE walks the graph in SQL rather than pulling every edge in
    the project into Python to BFS over.
    """
    sql = """
        WITH RECURSIVE reachable(node) AS (
            SELECT depends_on FROM task_deps WHERE task_id = ?
            UNION ALL
            SELECT d.depends_on FROM task_deps d
            JOIN reachable r ON d.task_id = r.node
        )
        SELECT 1 FROM reachable WHERE node = ? LIMIT 1
    """
    return db.execute_sql(sql, (start, target)).fetchone() is not None


@bound
def add_dependency(db: SqliteDatabase, task_id: int, depends_on: int) -> None:
    """Make `task_id` wait for `depends_on`. Refuses self-loops and cycles.

    Enforces a strict acyclic dependency graph: a task cannot wait on itself,
    and adding a dependency that would create a cycle is rejected. Duplicate
    dependencies are silently ignored (idempotent operation).

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task that will wait.
        depends_on: Task that must finish first.

    Raises:
        ValueError: If task_id == depends_on (self-loop).
        ValueError: If depends_on already transitively depends on task_id (cycle).

    Examples:
        >>> add_dependency(db, 10, 5)  # task 10 waits for task 5
        >>> add_dependency(db, 10, 5)  # idempotent; no error
        >>> add_dependency(db, 5, 10)  # raises ValueError: cycle
    """
    if task_id == depends_on:
        raise ValueError("a task cannot depend on itself")
    if _reaches(db, depends_on, task_id):
        raise ValueError(f"task {depends_on} already depends on task {task_id}")
    TaskDep.insert(task=task_id, depends_on=depends_on).on_conflict_ignore().execute()


@bound
def remove_dependency(db: SqliteDatabase, task_id: int, depends_on: int) -> None:
    """Remove a dependency edge.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task that was waiting.
        depends_on: Task that is no longer a prerequisite.
    """
    TaskDep.delete().where(
        (TaskDep.task == task_id) & (TaskDep.depends_on == depends_on)
    ).execute()


@bound
def task_dependencies(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch the tasks that this one depends on, as full task records.

    These are the prerequisites: all must reach "done" status before this
    task becomes runnable.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query.

    Returns:
        list[dict]: Task records this task depends on, in insertion order.
    """
    return rows(
        Task.select()
        .join(TaskDep, on=(TaskDep.depends_on == Task.id))
        .where(TaskDep.task == task_id)
        .order_by(Task.id)
    )


@bound
def task_dependents(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch the tasks that depend on this one (the reverse direction).

    These tasks are blocked until this one reaches "done" status.

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query.

    Returns:
        list[dict]: Task records that depend on this task, in insertion order.
    """
    return rows(
        Task.select()
        .join(TaskDep, on=(TaskDep.task == Task.id))
        .where(TaskDep.depends_on == task_id)
        .order_by(Task.id)
    )


@bound
def blocking_dependencies(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Fetch the dependencies that are blocking this task from running.

    Returns only the dependencies that have not yet reached "done" status.
    An empty list means the task is runnable (all prerequisites met).

    Args:
        db: SqliteDatabase instance for this project.
        task_id: Task to query.

    Returns:
        list[dict]: Task records that are dependencies and not yet done.

    Examples:
        >>> blocking = blocking_dependencies(db, 42)
        >>> if blocking:
        ...     print(f"Task 42 is blocked by {len(blocking)} task(s)")
        ...     for dep in blocking:
        ...         print(f"  - {dep['id']}: {dep['title']} ({dep['status']})")
    """
    return [d for d in task_dependencies(db, task_id) if d["status"] != "done"]


@bound
def blocking_map(db: SqliteDatabase) -> dict[int, list[dict]]:
    """Compute all tasks' blocking dependencies in one efficient query.

    For UI rendering and status summaries, this is more efficient than
    calling blocking_dependencies() for each task separately. Returns a
    mapping from task ID to its list of unfinished dependencies.

    Args:
        db: SqliteDatabase instance for this project.

    Returns:
        dict[int, list[dict]]: Maps task_id -> list of unfinished dependency
                               records (id, title, status). Tasks with no
                               blocking dependencies are omitted.

    Examples:
        >>> blocking_map = blocking_map(db)
        >>> if 42 in blocking_map:
        ...     print(f"Task 42 blocked by: {blocking_map[42]}")
    """
    query = (
        TaskDep.select(
            TaskDep.task.alias("task_id"), Task.id, Task.title, Task.status
        )
        .join(Task, on=(TaskDep.depends_on == Task.id))
        .where(Task.status != "done")
        .order_by(Task.id)
    )
    blocking: dict[int, list[dict]] = {}
    for record in rows(query):
        blocking.setdefault(record["task_id"], []).append(record)
    return blocking
