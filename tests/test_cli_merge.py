"""`kuska merge`: squash a ready_to_merge task into the base branch."""

import subprocess
from pathlib import Path

import pytest

from kuska import cli, worktree
from kuska import connect, db_path, get_task, init_db, transition, update_task
from kuska.store import add_task


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    (root / ".agents").mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agents/\n")
    (root / "README.md").write_text("# T\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    db = connect(db_path(root))
    init_db(db)
    task_id = add_task(db, "Add a feature", "desc")
    base = worktree.base_branch(root)
    path, branch, _ = worktree.ensure_worktree(root, task_id, "Add a feature", base)
    (path / "f.txt").write_text("1\n")
    worktree.commit_all(path, "wip: task 1 one")
    (path / "g.txt").write_text("2\n")
    worktree.commit_all(path, "wip: task 1 two")
    update_task(db, task_id, worktree_path=str(path), worktree_base_sha=worktree.merge_base(root, branch, base),
                status="ready_to_merge")
    return root, db, task_id, path, branch


def merge(root, task_id):
    return _run(["--project", str(root), "merge", str(task_id)])


def _run(argv):
    try:
        cli.main(argv)
    except SystemExit as e:
        return e.code
    return 0


def test_merge_squashes_one_commit(repo, capsys):
    root, db, task_id, path, branch = repo
    before = git(root, "rev-list", "--count", "HEAD")
    assert merge(root, task_id) == 0
    assert int(git(root, "rev-list", "--count", "HEAD")) == int(before) + 1
    message = git(root, "log", "-1", "--format=%B")
    assert message.startswith("Add a feature") and message.endswith(f"Kuska-Task: {task_id}")
    assert (root / "f.txt").exists() and (root / "g.txt").exists()
    assert get_task(db, task_id)["status"] == "done"
    assert not path.exists()
    assert branch not in git(root, "branch", "--format=%(refname:short)").split()


@pytest.mark.parametrize("how", ["dirty", "branch", "status"])
def test_merge_refuses(repo, how, capsys):
    root, db, task_id, path, branch = repo
    if how == "dirty":
        (root / "README.md").write_text("changed\n")
    elif how == "branch":
        git(root, "checkout", "--detach")
    else:
        transition(db, task_id, "requeue")
    head = git(root, "rev-parse", "HEAD")
    assert merge(root, task_id) == 1
    assert "error" in capsys.readouterr().out
    assert git(root, "rev-parse", "HEAD") == head
    assert path.exists()
