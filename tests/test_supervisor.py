"""Supervisor sweep: abandon runs whose daemon died, and notice merged branches."""

import subprocess
import threading
import time

import pytest

import kuska as core
from kuska import supervisor, worktree


def add_in_progress(conn, title):
    tid = core.add_task(conn, title, "", "dev-agent")
    core.update_task_status(conn, tid, "ready")
    core.update_task_status(conn, tid, "in_progress")
    return tid


def start_run(conn, task_id, run_id, age_s=600, status="running"):
    """A run whose last heartbeat was `age_s` seconds ago."""
    core.start_run(conn, run_id, task_id, "dev-agent")
    conn.execute_sql(
        "UPDATE runs SET heartbeat_at = ?, status = ? WHERE id = ?",
        (time.time() - age_s, status, run_id),
    )


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(project):
    """`project` as a git repo on `main` with one commit."""
    git(project, "init", "-b", "main")
    git(project, "config", "user.email", "t@example.com")
    git(project, "config", "user.name", "t")
    git(project, "config", "commit.gpgsign", "false")
    (project / "a.txt").write_text("a")
    git(project, "add", "a.txt")
    git(project, "commit", "-m", "init")
    return project


@pytest.mark.parametrize(
    ("age_s", "run_status", "task_moved_to", "abandoned", "task_after"),
    [
        pytest.param(600, "running", None, True, "blocked", id="dead"),
        pytest.param(1, "running", None, False, "in_progress", id="fresh"),
        pytest.param(600, "finished", None, False, "in_progress", id="finished"),
        pytest.param(600, "running", "todo", True, "todo", id="task-moved-by-human"),
    ],
)
def test_expire_runs(conn, age_s, run_status, task_moved_to, abandoned, task_after):
    tid = add_in_progress(conn, "t")
    start_run(conn, tid, "r1", age_s=age_s, status=run_status)
    if task_moved_to:
        core.update_task_status(conn, tid, task_moved_to)

    assert supervisor.expire_runs(conn) == (["r1"] if abandoned else [])
    expected_run = "abandoned" if abandoned else run_status
    assert core.get_run(conn, "r1")["status"] == expected_run
    assert core.get_task(conn, tid)["status"] == task_after
    blocker = any(
        m["msg_type"] == "blocker" and "abandoned" in m["payload"]
        for m in core.task_messages(conn, tid)
    )
    assert blocker == (task_after == "blocked")


def test_detect_merges(conn, repo):
    tasks = {}
    for name in ("merged", "open"):
        tid = core.add_task(conn, name, "", "dev-agent")
        path, branch, _ = worktree.ensure_worktree(repo, tid, name, "main")
        core.update_task(
            conn, tid, worktree_path=str(path),
            worktree_base_sha=worktree.merge_base(repo, branch, "main"),
        )
        (path / f"{name}.txt").write_text(name)
        git(path, "add", ".")
        git(path, "commit", "-m", name)
        core.update_task_status(conn, tid, "ready_to_merge")
        tasks[name] = (tid, branch)
    git(repo, "merge", "--no-ff", "-m", "merge", tasks["merged"][1])

    assert supervisor.detect_merges(conn, repo) == [tasks["merged"][0]]
    assert core.get_task(conn, tasks["merged"][0])["status"] == "done"
    assert core.get_task(conn, tasks["open"][0])["status"] == "ready_to_merge"


def test_run_supervisor_sweeps_once_then_stops(conn, project):
    tid = add_in_progress(conn, "dead")
    start_run(conn, tid, "r-dead")

    stop = threading.Event()
    stop.set()  # sweep runs before the first wait, so this is exactly one pass
    supervisor.run_supervisor(project, stop, interval_s=60)

    assert core.get_run(conn, "r-dead")["status"] == "abandoned"
    assert core.get_task(conn, tid)["status"] == "blocked"
