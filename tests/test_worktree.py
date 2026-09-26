#!/usr/bin/env python3
"""Standalone tests for kuska.worktree - git layer with no kuska dependencies."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kuska import worktree

PASSED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    """Check a condition and track pass/fail."""
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)


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
    print("\nslug()")
    check("simple title", worktree.slug("Fix search ranking") == "fix-search-ranking")
    check("lowercase", worktree.slug("UPPERCASE") == "uppercase")
    check("unicode chars", worktree.slug("café résumé") == "caf-r-sum")
    check("punctuation runs", worktree.slug("fix!!!bug???") == "fix-bug")
    check("leading/trailing punct", worktree.slug("---fix-bug---") == "fix-bug")
    check("only punctuation", worktree.slug("!!!???") == "")
    check("empty string", worktree.slug("") == "")

    # Test 200-character title
    long_title = "a" * 50 + "b" * 50 + "c" * 50 + "d" * 50  # 200 chars
    long_slug = worktree.slug(long_title)
    check("200-char title truncated", len(long_slug) <= 40)
    check("200-char title no trailing dash", not long_slug.endswith("-"))

    # Test title with many punctuation/spaces at truncation boundary
    boundary_title = "test-" * 30  # Creates a title > 40 chars
    boundary_slug = worktree.slug(boundary_title)
    check("truncate then strip dashes", not boundary_slug.endswith("-"))


def test_branch_name():
    """Test branch name generation."""
    print("\nbranch_name()")
    check("with slug", worktree.branch_name(17, "Fix search ranking") == "kuska/17-fix-search-ranking")
    check("empty slug", worktree.branch_name(42, "!!!") == "kuska/42")
    check("prefix", worktree.branch_name(1, "test") == "kuska/1-test")


def test_worktree_path():
    """Test worktree path generation."""
    print("\nworktree_path()")
    project = Path("/tmp/myproject")
    path = worktree.worktree_path(project, 17)
    check("correct path", path == project / ".agents" / "worktrees" / "task-17")
    check("task id in path", "task-17" in str(path))


def test_is_git_repo():
    """Test git repo detection."""
    print("\nis_git_repo()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        # Not a git repo
        check("not a git repo", not worktree.is_git_repo(tmp))

        # Initialize git repo
        init_git_repo(tmp)
        check("is a git repo", worktree.is_git_repo(tmp))

        # Non-existent path
        check("non-existent path", not worktree.is_git_repo(tmp / "nonexistent"))
    finally:
        shutil.rmtree(tmp)


def test_base_branch():
    """Test base branch detection."""
    print("\nbase_branch()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)

        branch = worktree.base_branch(tmp)
        check("detects main or master", branch in ["main", "master"])

        # Not a git repo
        default_branch = worktree.base_branch(tmp / "nonexistent")
        check("defaults to main", default_branch == "main")
    finally:
        shutil.rmtree(tmp)


def test_ensure_worktree_create():
    """Test creating a new worktree."""
    print("\nensure_worktree() - create")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        path, branch, created = worktree.ensure_worktree(tmp, 1, "Test task", base)

        check("created is True", created)
        check("correct path", path == tmp / ".agents" / "worktrees" / "task-1")
        check("path exists", path.exists())
        check("correct branch", branch == "kuska/1-test-task")
    finally:
        shutil.rmtree(tmp)


def test_ensure_worktree_idempotent():
    """Test that ensure_worktree is idempotent."""
    print("\nensure_worktree() - idempotent")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # First call
        path1, branch1, created1 = worktree.ensure_worktree(tmp, 2, "Test task", base)
        check("first call creates", created1)

        # Second call
        path2, branch2, created2 = worktree.ensure_worktree(tmp, 2, "Test task", base)
        check("second call doesn't create", not created2)
        check("same path", path1 == path2)
        check("same branch", branch1 == branch2)
    finally:
        shutil.rmtree(tmp)


def test_ensure_worktree_branch_exists():
    """Test ensure_worktree when directory deleted but branch exists."""
    print("\nensure_worktree() - branch exists, dir deleted")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create worktree
        path, branch, created = worktree.ensure_worktree(tmp, 3, "Test task", base)
        check("created", created)

        # Delete the directory by hand
        shutil.rmtree(path)
        check("directory deleted", not path.exists())

        # Call ensure_worktree again - should recreate it
        path2, branch2, created2 = worktree.ensure_worktree(tmp, 3, "Test task", base)
        check("recreated worktree", created2)
        check("path restored", path2.exists())
    finally:
        shutil.rmtree(tmp)


def test_is_dirty():
    """Test dirty tree detection."""
    print("\nis_dirty()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create and setup worktree
        path, _, _ = worktree.ensure_worktree(tmp, 4, "Test task", base)

        # Clean tree
        check("clean tree", not worktree.is_dirty(path))

        # Modify a file
        (path / "README.md").write_text("# Modified\n")
        check("dirty after modification", worktree.is_dirty(path))

        # Create untracked file
        (path / "untracked.txt").write_text("untracked\n")
        check("dirty with untracked files", worktree.is_dirty(path))
    finally:
        shutil.rmtree(tmp)


def test_commit_all():
    """Test committing changes."""
    print("\ncommit_all()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create worktree
        path, _, _ = worktree.ensure_worktree(tmp, 5, "Test task", base)

        # Test commit on clean tree returns None
        sha = worktree.commit_all(path, "Empty commit")
        check("clean tree returns None", sha is None)

        # Make a change
        (path / "newfile.txt").write_text("content\n")
        check("dirty before commit", worktree.is_dirty(path))

        # Commit the change
        sha = worktree.commit_all(path, "Add newfile.txt")
        check("commit returns sha", sha is not None)
        check("sha is hex string", isinstance(sha, str) and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))
        check("clean after commit", not worktree.is_dirty(path))
    finally:
        shutil.rmtree(tmp)


def test_ahead_count():
    """Test counting commits ahead of base."""
    print("\nahead_count()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create worktree
        path, branch, _ = worktree.ensure_worktree(tmp, 6, "Test task", base)

        # Initially 0 commits ahead
        count = worktree.ahead_count(path, branch, base)
        check("initial count is 0", count == 0)

        # Make and commit a change
        (path / "file1.txt").write_text("content1\n")
        worktree.commit_all(path, "Commit 1")

        count = worktree.ahead_count(path, branch, base)
        check("count is 1", count == 1)

        # Make and commit another change
        (path / "file2.txt").write_text("content2\n")
        worktree.commit_all(path, "Commit 2")

        count = worktree.ahead_count(path, branch, base)
        check("count is 2", count == 2)
    finally:
        shutil.rmtree(tmp)


def test_rebase_onto():
    """Test rebasing worktree onto base."""
    print("\nrebase_onto() - successful rebase")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create worktree and make commits
        path, branch, _ = worktree.ensure_worktree(tmp, 7, "Test task", base)
        (path / "file1.txt").write_text("content1\n")
        worktree.commit_all(path, "Commit in branch")

        # Make a commit in the main repo
        (tmp / "main_file.txt").write_text("main content\n")
        subprocess.run(
            ["git", "add", "main_file.txt"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Main commit"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )

        # Rebase the worktree
        success, detail = worktree.rebase_onto(path, base)
        check("rebase succeeds", success, detail)
        check("rebase detail empty on success", detail == "")
        check("worktree clean after rebase", not worktree.is_dirty(path))
    finally:
        shutil.rmtree(tmp)


def test_rebase_conflict():
    """Test rebase with conflict."""
    print("\nrebase_onto() - conflict handling")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create worktree and modify the same file
        path, branch, _ = worktree.ensure_worktree(tmp, 8, "Test task", base)
        (path / "README.md").write_text("# Branch version\n")
        worktree.commit_all(path, "Modify README in branch")

        # Modify the same file in main
        (tmp / "README.md").write_text("# Main version\n")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Modify README in main"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )

        # Try to rebase - should fail but be aborted cleanly
        success, detail = worktree.rebase_onto(path, base)
        check("rebase fails", not success)
        check("error detail provided", len(detail) > 0, detail)

        # Check that rebase is not in progress
        rebase_dir = path / ".git" / "rebase-merge"
        check("rebase-merge cleaned up", not rebase_dir.exists())

        # Tree should be clean (rebase aborted)
        check("tree is clean after abort", not worktree.is_dirty(path))
    finally:
        shutil.rmtree(tmp)


def test_diff_stat():
    """Test diff_stat with merge base."""
    print("\ndiff_stat()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create worktree and make a commit
        path, branch, _ = worktree.ensure_worktree(tmp, 9, "Test task", base)
        (path / "branch_file.txt").write_text("branch content\n" * 10)
        worktree.commit_all(path, "Commit in branch")

        # Make commits in main AFTER branching
        (tmp / "main_file.txt").write_text("main content\n" * 5)
        subprocess.run(
            ["git", "add", "main_file.txt"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit in main after branch"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )

        # Get diff stats - should only include the branch commit, not the main commit
        stats = worktree.diff_stat(tmp, branch, base)
        check("files in diff", stats["files"] == 1, f"expected 1 file, got {stats['files']}")
        check("insertions > 0", stats["insertions"] > 0, f"insertions: {stats['insertions']}")
        check("deletions == 0", stats["deletions"] == 0, f"deletions: {stats['deletions']}")
    finally:
        shutil.rmtree(tmp)


def test_merged_branches():
    """Test listing merged branches."""
    print("\nmerged_branches()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Initially no merged kuska branches
        merged = worktree.merged_branches(tmp, base)
        check("no merged branches initially", len(merged) == 0)

        # Create and merge a branch
        path, branch, _ = worktree.ensure_worktree(tmp, 10, "Test task", base)
        (path / "file.txt").write_text("content\n")
        worktree.commit_all(path, "Work on branch")

        # Merge the branch into main
        subprocess.run(
            ["git", "merge", branch],
            cwd=tmp,
            check=True,
            capture_output=True,
        )

        # Now the branch should appear as merged
        merged = worktree.merged_branches(tmp, base)
        check("merged branch appears", branch in merged)
        check("base not in merged", base not in merged)
    finally:
        shutil.rmtree(tmp)


def test_list_worktrees():
    """Test listing worktrees."""
    print("\nlist_worktrees()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create multiple worktrees
        path1, branch1, _ = worktree.ensure_worktree(tmp, 11, "Task 1", base)
        path2, branch2, _ = worktree.ensure_worktree(tmp, 12, "Task 2", base)

        # List worktrees
        worktrees = worktree.list_worktrees(tmp)

        # Should have at least 3: main repo + 2 worktrees
        check("worktrees listed", len(worktrees) >= 3)

        # Check that our worktrees are in the list (use resolved paths for comparison)
        paths = {Path(wt["path"]).resolve() for wt in worktrees}
        check("worktree 1 listed", path1.resolve() in paths)
        check("worktree 2 listed", path2.resolve() in paths)
    finally:
        shutil.rmtree(tmp)


def test_remove_worktree():
    """Test removing a worktree."""
    print("\nremove_worktree()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create a worktree
        path, branch, _ = worktree.ensure_worktree(tmp, 13, "Task to remove", base)
        check("worktree created", path.exists())

        # Remove it
        ok, detail = worktree.remove_worktree(tmp, path, branch)
        check("remove succeeds", ok, detail)
        check("path removed", not path.exists())

        # Ensure we can create a new one with the same id
        path2, branch2, created = worktree.ensure_worktree(tmp, 13, "Task to remove", base)
        check("can recreate worktree", created)
        check("new path exists", path2.exists())
    finally:
        shutil.rmtree(tmp)


def test_prune():
    """Test pruning deleted worktrees."""
    print("\nprune()")
    tmp = Path(tempfile.mkdtemp(prefix="kuska-test-"))
    try:
        init_git_repo(tmp)
        base = worktree.base_branch(tmp)

        # Create a worktree
        path, _, _ = worktree.ensure_worktree(tmp, 14, "Task to prune", base)

        # Delete the directory by hand (not using remove_worktree)
        shutil.rmtree(path)

        # Prune should clean up metadata
        worktree.prune(tmp)

        # We just check it doesn't error
        check("prune doesn't error", True)
    finally:
        shutil.rmtree(tmp)


def main() -> None:
    """Run all tests."""
    test_slug()
    test_branch_name()
    test_worktree_path()
    test_is_git_repo()
    test_base_branch()
    test_ensure_worktree_create()
    test_ensure_worktree_idempotent()
    test_ensure_worktree_branch_exists()
    test_is_dirty()
    test_commit_all()
    test_ahead_count()
    test_rebase_onto()
    test_rebase_conflict()
    test_diff_stat()
    test_merged_branches()
    test_list_worktrees()
    test_remove_worktree()
    test_prune()

    print(f"\n✓ all {PASSED} checks passed")


if __name__ == "__main__":
    main()
