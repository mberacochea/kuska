"""Background sweep: end runs whose daemon died, and notice merged branches.

Nothing else in kuska runs on a timer, so a crashed daemon would leave its task
`in_progress` forever, and a merged branch would stay `ready_to_merge` until
someone opened the merge-queue page.
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import worktree
from .db import HUMAN, connect, init_db, now
from .project import db_path
from .store import (
    InvalidTransition,
    end_run,
    get_task,
    list_tasks,
    send_message,
    stale_runs,
    transition,
)

STALE_AFTER_S = 300.0  # 10 missed heartbeats (loop.RUN_HEARTBEAT_S is 30)
SWEEP_EVERY_S = 15.0


def expire_runs(db, stale_after_s: float = STALE_AFTER_S) -> list[str]:
    """End runs whose heartbeat stopped, and block the task they left in_progress.

    Returns:
        list[str]: Ids of the runs that were abandoned.
    """
    expired = []
    for run in stale_runs(db, stale_after_s):
        task_id, agent = run["task_id"], run["agent"]
        age = int(now() - run["heartbeat_at"])
        reason = f"no heartbeat for {age}s - the daemon stopped mid-run"
        end_run(db, run["id"], "abandoned", exit_reason=reason)
        expired.append(run["id"])
        task = get_task(db, task_id)
        if task and task["status"] == "in_progress":
            send_message(db, agent, HUMAN, task_id, "blocker", f"run abandoned: {reason}")
            try:
                transition(db, task_id, "block", actor=agent)
            except InvalidTransition:
                pass
    return expired


def detect_merges(db, project: Path) -> list[int]:
    """Move ready_to_merge tasks whose branch has been merged to done.

    Returns:
        list[int]: Ids of the tasks moved.
    """
    base = worktree.base_branch(project)
    merged = []
    for task in list_tasks(db, status="ready_to_merge"):
        if not task.get("worktree_path"):
            continue
        branch = worktree.branch_for_path(project, task["worktree_path"])
        if not branch or not worktree.is_branch_merged(
            project, branch, base, task.get("worktree_base_sha"), task["id"]
        ):
            continue
        try:
            transition(db, task["id"], "merged", actor="supervisor")
        except InvalidTransition:
            continue
        merged.append(task["id"])
    return merged


def sweep(db, project: Path, stale_after_s: float = STALE_AFTER_S) -> dict:
    """One pass of both checks."""
    return {
        "expired": expire_runs(db, stale_after_s),
        "merged": detect_merges(db, project),
    }


def run_supervisor(
    project: Path,
    stop: threading.Event,
    interval_s: float = SWEEP_EVERY_S,
    stale_after_s: float = STALE_AFTER_S,
) -> None:
    """Sweep until `stop` is set. Opens and closes its own connection."""
    db = connect(db_path(project))
    try:
        init_db(db)
        while True:
            try:
                result = sweep(db, project, stale_after_s)
                if result["expired"] or result["merged"]:
                    print(f"[supervisor] {result}", flush=True)
            except Exception as exc:  # noqa: BLE001 - one bad sweep must not end the thread
                print(f"[supervisor] sweep failed: {exc!r}", flush=True)
            if stop.wait(interval_s):
                break
    finally:
        db.close()
