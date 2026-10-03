"""Git worktree management for kuska tasks.

The git layer: every git operation in one module with no kuska dependencies
beyond pathlib. Testable against a throwaway repo without database, daemon or agent.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import git

BRANCH_PREFIX = "kuska/"
WORKTREES_DIR = "worktrees"


def slug(title: str) -> str:
    """Convert title to a slug for use in branch names.

    - lowercase
    - every run of non-[a-z0-9] becomes a single "-"
    - strip leading and trailing "-"
    - truncate to 40 chars then strip "-" again
    - return "" if nothing survives
    """
    # Convert to lowercase
    s = title.lower()

    # Replace runs of non-[a-z0-9] with single "-"
    s = re.sub(r"[^a-z0-9]+", "-", s)

    # Strip leading and trailing "-"
    s = s.strip("-")

    # Truncate to 40 chars
    s = s[:40]

    # Strip "-" again after truncation
    s = s.strip("-")

    return s


def branch_name(task_id: int, title: str) -> str:
    """Generate a branch name from task id and title.

    Format: "kuska/17-fix-search-ranking" or "kuska/17" when slug() is empty.
    """
    s = slug(title)
    if s:
        return f"{BRANCH_PREFIX}{task_id}-{s}"
    return f"{BRANCH_PREFIX}{task_id}"


def worktree_path(project: Path, task_id: int) -> Path:
    """Get the path for a worktree for a task.

    project / ".agents" / "worktrees" / f"task-{task_id}"
    """
    return Path(project) / ".agents" / WORKTREES_DIR / f"task-{task_id}"


def is_git_repo(project: Path) -> bool:
    """Check if project is a git repository."""
    try:
        git.Repo(project)
        return True
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        return False


def has_commits(project: Path) -> bool:
    """Check if the repository has at least one commit."""
    try:
        repo = git.Repo(project)
        repo.head.commit
        return True
    except Exception:
        return False


def base_branch(project: Path) -> str:
    """Get the main checkout's current branch.

    Uses `rev-parse --abbrev-ref HEAD`. Falls back to "main" if detached.
    """
    try:
        repo = git.Repo(project)
        # Get the current branch name
        if repo.head.is_detached:
            return "main"
        return repo.active_branch.name
    except Exception:
        return "main"


def ensure_worktree(project: Path, task_id: int, title: str, base: str) -> tuple[Path, str, bool]:
    """Ensure a worktree exists for a task.

    Returns (path, branch, created).

    If the path already exists and git lists it as a worktree, return it with
    created=False. Otherwise `git worktree add -b <branch> <path> <base>`.
    If the branch already exists but the worktree does not (removed by hand),
    `git worktree add <path> <branch>` without -b.
    Create parent directories first.

    Raises RuntimeError if `git worktree add` itself fails (e.g. disk full,
    permission denied) - a real failure, not the "already exists" case handled
    above, so it must not be reported back as a fake success.
    """
    path = worktree_path(project, task_id)
    branch = branch_name(task_id, title)

    try:
        repo = git.Repo(project)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        return path, branch, False

    # Check if worktree already exists and is functional (directory exists)
    existing_worktrees = list_worktrees(project)
    resolved_path = path.resolve()
    for wt in existing_worktrees:
        if Path(wt["path"]).resolve() == resolved_path and Path(wt["path"]).exists():
            return path, branch, False

    # Create parent directories
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Check if branch already exists
        branch_exists = False
        try:
            repo.heads[branch]
            branch_exists = True
        except IndexError:
            branch_exists = False

        # Check if there's a stale worktree entry (registered but missing)
        stale_worktree = False
        for wt in existing_worktrees:
            if Path(wt["path"]).resolve() == path.resolve() and not Path(wt["path"]).exists():
                stale_worktree = True
                break

        if stale_worktree:
            # Clean up stale worktree entry first
            try:
                repo.git.worktree("remove", str(path))
            except Exception:
                pass

        if branch_exists:
            # Branch exists but worktree doesn't - use without -b
            repo.git.worktree("add", str(path), branch)
        else:
            # Create new branch and worktree
            repo.git.worktree("add", "-b", branch, str(path), base)

        return path, branch, True
    except git.GitCommandError as e:
        raise RuntimeError(f"git worktree add failed for task {task_id}: {e}") from e


def rebase_onto(path: Path, base: str) -> tuple[bool, str]:
    """Rebase the worktree onto base branch.

    `git -C <path> rebase <base>`. On non-zero exit, run
    `git -C <path> rebase --abort` and return (False, stderr-first-line).
    Never leave a worktree mid-rebase.
    Returns (True, "") on success.
    """
    try:
        repo = git.Repo(path)
        try:
            repo.git.rebase(base)
            return True, ""
        except git.GitCommandError as e:
            # Abort the rebase to clean up
            try:
                repo.git.rebase("--abort")
            except Exception:
                pass
            # Try to extract a meaningful error message
            # GitPython formats errors with both stdout and stderr
            error_msg = str(e)
            # Look for actual error content - could be in stdout, stderr, or the exception message
            if "CONFLICT" in str(e):
                first_line = "conflict: merge conflict encountered"
            elif e.stderr:
                # e.stderr contains the full error output
                first_line = e.stderr.split("\n")[0] if e.stderr else "rebase failed"
                if not first_line or first_line.startswith("\n"):
                    # Parse nested format
                    lines = e.stderr.split("\n")
                    for line in lines:
                        if line.strip() and not line.startswith("  "):
                            first_line = line.strip()
                            break
            else:
                first_line = error_msg.split("\n")[0] if error_msg else "rebase failed"
            return False, first_line if first_line else "rebase failed"
    except Exception as e:
        return False, str(e).split("\n")[0]


def is_dirty(path: Path) -> bool:
    """Check if worktree has uncommitted changes.

    `status --porcelain` non-empty, including untracked files.
    """
    try:
        repo = git.Repo(path)
        # status() returns dict with index and working tree changes
        # untracked_files returns list of untracked files
        status = repo.git.status("--porcelain")
        return bool(status)
    except Exception:
        return False


def commit_all(path: Path, message: str) -> str | None:
    """Commit all changes.

    `add -A` then `commit -m <message>`. Returns the new sha, or None when
    there was nothing to commit. Pass --no-verify: a project's own hooks are
    not this system's business and a failing hook must not strand the work.
    """
    try:
        repo = git.Repo(path)

        # Check if there are changes to commit
        if not is_dirty(path):
            return None

        # Add all changes
        repo.git.add("-A")

        # Commit with --no-verify
        try:
            commit = repo.git.commit("-m", message, "--no-verify")
            # Get the SHA of the new commit
            sha = repo.head.commit.hexsha
            return sha
        except git.GitCommandError as e:
            # If commit fails (e.g., nothing to commit), return None
            if "nothing to commit" in e.stderr or "nothing to commit" in str(e):
                return None
            raise
    except Exception:
        return None


def ahead_count(path: Path, branch: str, base: str) -> int:
    """Count commits ahead of base branch.

    `rev-list --count <base>..<branch>` 0 on any failure.
    """
    try:
        repo = git.Repo(path)
        count = repo.git.rev_list("--count", f"{base}..{branch}")
        return int(count)
    except Exception:
        return 0


def diff_stat(project: Path, branch: str, base: str) -> dict:
    """Get diff statistics between branches.

    `diff --numstat <base>...<branch>` — THREE dots, the merge base.
    Returns {"files": int, "insertions": int, "deletions": int}.
    Binary files report "-" in numstat; count the file, add 0 lines.
    """
    try:
        repo = git.Repo(project)
        output = repo.git.diff("--numstat", f"{base}...{branch}")

        files = 0
        insertions = 0
        deletions = 0

        for line in output.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                # First part is insertions, second is deletions
                add = parts[0]
                remove = parts[1]

                files += 1
                # Binary files show "-" instead of counts
                if add != "-":
                    insertions += int(add)
                if remove != "-":
                    deletions += int(remove)

        return {"files": files, "insertions": insertions, "deletions": deletions}
    except Exception:
        return {"files": 0, "insertions": 0, "deletions": 0}


def merged_branches(project: Path, base: str) -> set[str]:
    """Get branches merged into base.

    `branch --merged <base> --format=%(refname:short)`, filtered to those
    starting with BRANCH_PREFIX. Excludes base itself.
    """
    try:
        repo = git.Repo(project)
        output = repo.git.branch("--merged", base, "--format=%(refname:short)")

        merged = set()
        for line in output.split("\n"):
            line = line.strip()
            if line and line.startswith(BRANCH_PREFIX) and line != base:
                merged.add(line)

        return merged
    except Exception:
        return set()


def merge_base(project: Path, branch: str, base: str) -> str | None:
    """The commit `branch` and `base` diverge from (`git merge-base`), or None on failure."""
    try:
        repo = git.Repo(project)
        return repo.git.merge_base(branch, base).strip() or None
    except Exception:
        return None


def is_branch_merged(project: Path, branch: str, base: str, base_sha: str | None) -> bool:
    """True when `branch` had commits of its own and all of them are in `base`.

    git lists a fresh branch with no commits as merged too, so the tip must
    differ from base_sha, the commit the branch started from. Without a
    recorded base_sha the branch cannot be judged: False. Any failure: False.
    """
    if not base_sha:
        return False
    try:
        repo = git.Repo(project)
        tip = repo.git.rev_parse(branch).strip()
        if tip == base_sha:
            return False
        try:
            repo.git.merge_base("--is-ancestor", tip, base)
        except git.GitCommandError:
            return False
        return True
    except Exception:
        return False


def list_worktrees(project: Path) -> list[dict]:
    """List all worktrees in the project.

    Parse `worktree list --porcelain` into
    [{"path": str, "branch": str|None, "head": str}, ...].
    """
    try:
        repo = git.Repo(project)
        output = repo.git.worktree("list", "--porcelain")

        worktrees = []
        current_wt = None

        for line in output.split("\n"):
            if not line.strip():
                continue

            if line.startswith("worktree "):
                # New worktree entry
                path = line[9:].strip()  # "worktree " is 9 chars
                current_wt = {"path": path, "branch": None, "head": ""}
                worktrees.append(current_wt)
            elif line.startswith("branch ") and current_wt:
                # Branch reference
                branch = line[7:].strip()  # "branch " is 7 chars
                # Strip the "refs/heads/" prefix, keep the rest (e.g. "kuska/17-fix")
                if branch.startswith("refs/heads/"):
                    current_wt["branch"] = branch[len("refs/heads/") :]
                else:
                    current_wt["branch"] = branch
            elif line.startswith("detached") and current_wt:
                # Detached HEAD
                current_wt["branch"] = None

        return worktrees
    except Exception:
        return []


def branch_for_path(project: Path, path: str | Path) -> str | None:
    """The branch checked out in the worktree at `path`, or None."""
    target = Path(path).resolve()
    for wt in list_worktrees(project):
        if wt.get("branch") and Path(wt["path"]).resolve() == target:
            return wt["branch"]
    return None


def remove_worktree(project: Path, path: Path, branch: str | None) -> tuple[bool, str]:
    """Remove a worktree and its branch.

    `worktree remove --force <path>`, then `branch -D <branch>` when branch
    is given. Then `worktree prune`. Returns (ok, detail).
    """
    try:
        repo = git.Repo(project)

        # Remove the worktree
        try:
            repo.git.worktree("remove", "--force", str(path))
        except git.GitCommandError as e:
            return False, str(e).split("\n")[0]

        # Remove the branch if given
        if branch:
            try:
                repo.git.branch("-D", branch)
            except git.GitCommandError:
                # Branch might not exist, that's ok
                pass

        # Prune
        try:
            repo.git.worktree("prune")
        except Exception:
            pass

        return True, ""
    except Exception as e:
        return False, str(e).split("\n")[0]


def prune(project: Path) -> None:
    """Clean up metadata for worktrees deleted by hand.

    `worktree prune` — clears metadata for directories deleted by hand.
    """
    try:
        repo = git.Repo(project)
        repo.git.worktree("prune")
    except Exception:
        pass
