"""Review tasks: a second agent looks at a dev task's branch before the human merges it."""

from __future__ import annotations

from peewee import SqliteDatabase

from .. import worktree
from ..db import HUMAN
from ..models import Task, rows
from .agents import get_agent
from .common import bound
from .lifecycle import transition
from .messages import is_work_task, send_message, task_messages
from .tasks import add_task, get_task

TITLE_MAX = 120

INSTRUCTIONS = (
    "You run inside the author's worktree. Do not modify files. "
    "Reply `done` if it can merge as is. "
    "Reply `needs_approval` with your findings (blocking issues first) if it needs changes. "
    "Reply `blocked` only if you could not review it."
)


def _description(task: dict, base: str) -> str:
    task_id = task["id"]
    branch = worktree.branch_name(task_id, task["title"] or "")
    lines = [
        f"# Review of task {task_id}",
        "",
        f"## Task {task_id}: {task['title']}",
        "",
        task.get("description") or "(no description)",
        "",
        "## Where the work is",
        "",
        f"- Branch: `{branch}`",
        f"- Base branch: `{base}`",
        f"- Worktree: `{task.get('worktree_path') or '(none recorded)'}`",
        "",
        "See what changed with:",
        "",
        f"    git diff {base}...{branch}",
        f"    git log {base}..{branch}",
        "",
    ]
    if task.get("assigned_to"):
        lines += [
            (
                f"The author's handover is the doc `task_{task_id}_{task['assigned_to']}_context`; "
                "read it with docs_get."
            ),
            "",
        ]
    lines += ["## Instructions", "", INSTRUCTIONS]
    return "\n".join(lines)


@bound
def task_reviews(db: SqliteDatabase, task_id: int) -> list[dict]:
    """Review tasks for task_id, oldest first."""
    return rows(Task.select().where(Task.review_of == task_id).order_by(Task.id))


@bound
def request_review(
    db: SqliteDatabase, task_id: int, reviewer: str, base: str, max_rounds: int = 2
) -> int | None:
    """Create a ready review task for a ready_to_merge work task.

    Returns the new task's id, or None when there is nothing to do: the task is
    missing, not ready_to_merge or not a work task, the reviewer is unknown, a
    review is already open, or max_rounds reviews were already made (then the
    human is told it is theirs now).
    """
    task = get_task(db, task_id)
    if task is None or task["status"] != "ready_to_merge" or not is_work_task(task):
        return None
    if get_agent(db, reviewer) is None:
        return None
    reviews = task_reviews(db, task_id)
    if any(r["status"] != "done" for r in reviews):
        return None
    if len(reviews) >= max_rounds:
        n = len(reviews)
        send_message(db, reviewer, HUMAN, task_id, "note",
                     f"task {task_id} has been reviewed {n} times - over to you")
        return None
    title = f"Review task {task_id}: {task['title']}"[:TITLE_MAX]
    review_id = add_task(
        db, title, _description(task, base),
        assigned_to=reviewer, feature=task.get("feature"), kind="review",
    )
    Task.update(review_of=task_id).where(Task.id == review_id).execute()
    transition(db, review_id, "make_ready", actor=reviewer)
    return review_id


@bound
def apply_review_outcome(db: SqliteDatabase, review_task_id: int) -> str | None:
    """Act on a finished review task, going by the status the reviewer ended in.

    done -> "passed": the human is told the branch can merge.
    needs_approval -> "changes_requested": if the source is still ready_to_merge,
    the findings go to its author and it is requeued; the review is then closed.
    blocked (anything else) -> "inconclusive": the human is told, the source stays.

    Returns the outcome recorded, or None if this is not a review task.
    """
    review = get_task(db, review_task_id)
    if review is None or not review.get("review_of"):
        return None
    source = get_task(db, review["review_of"])
    if source is None:
        return None
    reviewer = review["assigned_to"]
    results = [m for m in task_messages(db, review_task_id) if m["msg_type"] == "result"]
    findings = results[-1]["payload"] if results else "(no findings given)"

    if review["status"] == "done":
        outcome = "passed"
        Task.update(review_outcome=outcome).where(Task.id == review_task_id).execute()
        send_message(db, reviewer, HUMAN, source["id"], "note",
                     f"Review passed (task {review_task_id}):\n\n{findings}")
    elif review["status"] == "needs_approval":
        outcome = "changes_requested"
        Task.update(review_outcome=outcome).where(Task.id == review_task_id).execute()
        if source["status"] == "ready_to_merge":
            send_message(
                db, reviewer, source["assigned_to"], source["id"], "note",
                f"Review by {reviewer} (task {review_task_id}) asks for changes:\n\n{findings}",
            )
            transition(db, source["id"], "requeue", actor=reviewer)
        transition(db, review_task_id, "approve", actor=HUMAN)
    else:
        outcome = "inconclusive"
        Task.update(review_outcome=outcome).where(Task.id == review_task_id).execute()
        send_message(db, reviewer, HUMAN, source["id"], "note",
                     f"Review could not be completed (task {review_task_id}):\n\n{findings}")
    return outcome
