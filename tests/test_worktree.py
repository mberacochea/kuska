"""Tests for kuska.worktree - git layer with no kuska dependencies."""

import shutil
import subprocess
from pathlib import Path

from kuska import worktree


def init_git_repo(path: Path) -> None:
    """Initialize a git repository with a dummy commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )

    # Create an initial commit
    (path / "README.md").write_text("# Test\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_slug():
    """Test slug generation."""
    assert worktree.slug("Fix search ranking") == "fix-search-ranking", "simple title"
    assert worktree.slug("UPPERCASE") == "uppercase", "lowercase"
    assert worktree.slug("café résumé") == "caf-r-sum", "unicode chars"
    assert worktree.slug("fix!!!bug???") == "fix-bug", "punctuation runs"
    assert worktree.slug("---fix-bug---") == "fix-bug", "leading/trailing punct"
    assert worktree.slug("!!!???") == "", "only punctuation"
    assert worktree.slug("") == "", "empty string"

    # Test 200-character title
    long_title = "a" * 50 + "b" * 50 + "c" * 50 + "d" * 50  # 200 chars
    long_slug = worktree.slug(long_title)
    assert len(long_slug) <= 40, "200-char title truncated"
    assert not long_slug.endswith("-"), "200-char title no trailing dash"

    # Test title with many punctuation/spaces at truncation boundary
    boundary_title = "test-" * 30  # Creates a title > 40 chars
    boundary_slug = worktree.slug(boundary_title)
    assert not boundary_slug.endswith("-"), "truncate then strip dashes"


def test_branch_name():
    """Test branch name generation."""
    assert worktree.branch_name(17, "Fix search ranking") == "kuska/17-fix-search-ranking", "with slug"
    assert worktree.branch_name(42, "!!!") == "kuska/42", "empty slug"
    assert worktree.branch_name(1, "test") == "kuska/1-test", "prefix"


def test_worktree_path(tmp_path):
    """Test worktree path generation."""
    project = Path("/tmp_path/myproject")
    path = worktree.worktree_path(project, 17)
    assert path == project / ".agents" / "worktrees" / "task-17", "correct path"
    assert "task-17" in str(path), "task id in path"


def test_is_git_repo(tmp_path):
    """Test git repo detection."""
    # Not a git repo
    assert not worktree.is_git_repo(tmp_path), "not a git repo"

    # Initialize git repo
    init_git_repo(tmp_path)
    assert worktree.is_git_repo(tmp_path), "is a git repo"

    # Non-existent path
    assert not worktree.is_git_repo(tmp_path / "nonexistent"), "non-existent path"


def test_base_branch(tmp_path):
    """Test base branch detection."""
    init_git_repo(tmp_path)

    branch = worktree.base_branch(tmp_path)
    assert branch in ["main", "master"], "detects main or master"

    # Not a git repo
    default_branch = worktree.base_branch(tmp_path / "nonexistent")
    assert default_branch == "main", "defaults to main"


def test_ensure_worktree_create(tmp_path):
    """Test creating a new worktree."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    path, branch, created = worktree.ensure_worktree(tmp_path, 1, "Test task", base)

    assert created, "created is True"
    assert path == tmp_path / ".agents" / "worktrees" / "task-1", "correct path"
    assert path.exists(), "path exists"
    assert branch == "kuska/1-test-task", "correct branch"


def test_ensure_worktree_idempotent(tmp_path):
    """Test that ensure_worktree is idempotent."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # First call
    path1, branch1, created1 = worktree.ensure_worktree(tmp_path, 2, "Test task", base)
    assert created1, "first call creates"

    # Second call
    path2, branch2, created2 = worktree.ensure_worktree(tmp_path, 2, "Test task", base)
    assert not created2, "second call doesn't create"
    assert path1 == path2, "same path"
    assert branch1 == branch2, "same branch"


def test_ensure_worktree_branch_exists(tmp_path):
    """Test ensure_worktree when directory deleted but branch exists."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create worktree
    path, branch, created = worktree.ensure_worktree(tmp_path, 3, "Test task", base)
    assert created, "created"

    # Delete the directory by hand
    shutil.rmtree(path)
    assert not path.exists(), "directory deleted"

    # Call ensure_worktree again - should recreate it
    path2, branch2, created2 = worktree.ensure_worktree(tmp_path, 3, "Test task", base)
    assert created2, "recreated worktree"
    assert path2.exists(), "path restored"


def test_is_dirty(tmp_path):
    """Test dirty tree detection."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create and setup worktree
    path, _, _ = worktree.ensure_worktree(tmp_path, 4, "Test task", base)

    # Clean tree
    assert not worktree.is_dirty(path), "clean tree"

    # Modify a file
    (path / "README.md").write_text("# Modified\n")
    assert worktree.is_dirty(path), "dirty after modification"

    # Create untracked file
    (path / "untracked.txt").write_text("untracked\n")
    assert worktree.is_dirty(path), "dirty with untracked files"


def test_commit_all(tmp_path):
    """Test committing changes."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create worktree
    path, _, _ = worktree.ensure_worktree(tmp_path, 5, "Test task", base)

    # Test commit on clean tree returns None
    sha = worktree.commit_all(path, "Empty commit")
    assert sha is None, "clean tree returns None"

    # Make a change
    (path / "newfile.txt").write_text("content\n")
    assert worktree.is_dirty(path), "dirty before commit"

    # Commit the change
    sha = worktree.commit_all(path, "Add newfile.txt")
    assert sha is not None, "commit returns sha"
    assert isinstance(sha, str) and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), "sha is hex string"
    assert not worktree.is_dirty(path), "clean after commit"


def test_ahead_count(tmp_path):
    """Test counting commits ahead of base."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create worktree
    path, branch, _ = worktree.ensure_worktree(tmp_path, 6, "Test task", base)

    # Initially 0 commits ahead
    count = worktree.ahead_count(path, branch, base)
    assert count == 0, "initial count is 0"

    # Make and commit a change
    (path / "file1.txt").write_text("content1\n")
    worktree.commit_all(path, "Commit 1")

    count = worktree.ahead_count(path, branch, base)
    assert count == 1, "count is 1"

    # Make and commit another change
    (path / "file2.txt").write_text("content2\n")
    worktree.commit_all(path, "Commit 2")

    count = worktree.ahead_count(path, branch, base)
    assert count == 2, "count is 2"


def test_rebase_onto(tmp_path):
    """Test rebasing worktree onto base."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create worktree and make commits
    path, branch, _ = worktree.ensure_worktree(tmp_path, 7, "Test task", base)
    (path / "file1.txt").write_text("content1\n")
    worktree.commit_all(path, "Commit in branch")

    # Make a commit in the main repo
    (tmp_path / "main_file.txt").write_text("main content\n")
    subprocess.run(
        ["git", "add", "main_file.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Main commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Rebase the worktree
    success, detail = worktree.rebase_onto(path, base)
    assert success, "rebase succeeds"
    assert detail == "", "rebase detail empty on success"
    assert not worktree.is_dirty(path), "worktree clean after rebase"


def test_rebase_conflict(tmp_path):
    """Test rebase with conflict."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create worktree and modify the same file
    path, branch, _ = worktree.ensure_worktree(tmp_path, 8, "Test task", base)
    (path / "README.md").write_text("# Branch version\n")
    worktree.commit_all(path, "Modify README in branch")

    # Modify the same file in main
    (tmp_path / "README.md").write_text("# Main version\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Modify README in main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Try to rebase - should fail but be aborted cleanly
    success, detail = worktree.rebase_onto(path, base)
    assert not success, "rebase fails"
    assert len(detail) > 0, "error detail provided"

    # Check that rebase is not in progress
    rebase_dir = path / ".git" / "rebase-merge"
    assert not rebase_dir.exists(), "rebase-merge cleaned up"

    # Tree should be clean (rebase aborted)
    assert not worktree.is_dirty(path), "tree is clean after abort"


def test_diff_stat(tmp_path):
    """Test diff_stat with merge base."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create worktree and make a commit
    path, branch, _ = worktree.ensure_worktree(tmp_path, 9, "Test task", base)
    (path / "branch_file.txt").write_text("branch content\n" * 10)
    worktree.commit_all(path, "Commit in branch")

    # Make commits in main AFTER branching
    (tmp_path / "main_file.txt").write_text("main content\n" * 5)
    subprocess.run(
        ["git", "add", "main_file.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Commit in main after branch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Get diff stats - should only include the branch commit, not the main commit
    stats = worktree.diff_stat(tmp_path, branch, base)
    assert stats["files"] == 1, "files in diff"
    assert stats["insertions"] > 0, "insertions > 0"
    assert stats["deletions"] == 0, "deletions == 0"


def test_merged_branches(tmp_path):
    """Test listing merged branches."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Initially no merged kuska branches
    merged = worktree.merged_branches(tmp_path, base)
    assert len(merged) == 0, "no merged branches initially"

    # Create and merge a branch
    path, branch, _ = worktree.ensure_worktree(tmp_path, 10, "Test task", base)
    (path / "file.txt").write_text("content\n")
    worktree.commit_all(path, "Work on branch")

    # Merge the branch into main
    subprocess.run(
        ["git", "merge", branch],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Now the branch should appear as merged
    merged = worktree.merged_branches(tmp_path, base)
    assert branch in merged, "merged branch appears"
    assert base not in merged, "base not in merged"


def test_list_worktrees(tmp_path):
    """Test listing worktrees."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create multiple worktrees
    path1, branch1, _ = worktree.ensure_worktree(tmp_path, 11, "Task 1", base)
    path2, branch2, _ = worktree.ensure_worktree(tmp_path, 12, "Task 2", base)

    # List worktrees
    worktrees = worktree.list_worktrees(tmp_path)

    # Should have at least 3: main repo + 2 worktrees
    assert len(worktrees) >= 3, "worktrees listed"

    # Check that our worktrees are in the list (use resolved paths for comparison)
    paths = {Path(wt["path"]).resolve() for wt in worktrees}
    assert path1.resolve() in paths, "worktree 1 listed"
    assert path2.resolve() in paths, "worktree 2 listed"


def test_remove_worktree(tmp_path):
    """Test removing a worktree."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create a worktree
    path, branch, _ = worktree.ensure_worktree(tmp_path, 13, "Task to remove", base)
    assert path.exists(), "worktree created"

    # Remove it
    ok, detail = worktree.remove_worktree(tmp_path, path, branch)
    assert ok, "remove succeeds"
    assert not path.exists(), "path removed"

    # Ensure we can create a new one with the same id
    path2, branch2, created = worktree.ensure_worktree(tmp_path, 13, "Task to remove", base)
    assert created, "can recreate worktree"
    assert path2.exists(), "new path exists"


def test_prune(tmp_path):
    """Test pruning deleted worktrees."""
    init_git_repo(tmp_path)
    base = worktree.base_branch(tmp_path)

    # Create a worktree
    path, _, _ = worktree.ensure_worktree(tmp_path, 14, "Task to prune", base)

    # Delete the directory by hand (not using remove_worktree)
    shutil.rmtree(path)

    # Prune should clean up metadata
    worktree.prune(tmp_path)

    # We just check it doesn't error
    assert True, "prune doesn't error"
