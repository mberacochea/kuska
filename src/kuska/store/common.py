"""What every store module shares: per-call model binding, and path normalisation."""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

from peewee import SqliteDatabase

from ..models import MODELS


def bound(fn_):
    """Bind the models to the database this call is for.

    Several projects can be open in one process (the web app switches between
    them), so binding happens per call rather than once at import.
    """

    @functools.wraps(fn_)
    def wrapper(db: SqliteDatabase, *args: Any, **kwargs: Any):
        with db.bind_ctx(MODELS):
            return fn_(db, *args, **kwargs)

    return wrapper


def normalize_path(path: str, project_dir: str | os.PathLike | None = None) -> str:
    """Normalize a file path to a consistent project-relative form.

    The same file can be named many ways ("./src/a.py", "/abs/src/a.py",
    "src/../src/a.py"); the claude daemon's read tracking needs one name per
    file. This collapses ".." and "." and resolves to a canonical relative
    path (relative to project_dir if given).

    Args:
        path: File or directory path (absolute or relative).
        project_dir: Project root for computing relative paths.

    Returns:
        str: Normalized path, typically relative to project_dir.

    Examples:
        >>> normalize_path("./src/foo.py", "/home/user/proj")
        "src/foo.py"
        >>> normalize_path("src/../src/foo.py", "/home/user/proj")
        "src/foo.py"
    """
    # collapse "." and ".." textually first, so two agents naming the same
    # file differently still land on the same key
    candidate = Path(os.path.normpath(str(path)))
    if project_dir:
        base = Path(project_dir).resolve()
        absolute = candidate if candidate.is_absolute() else base / candidate
        try:
            candidate = absolute.resolve().relative_to(base)
        except ValueError:
            candidate = absolute.resolve()
    result = str(candidate)
    # An absolute result only happens when project_dir was given and the path
    # escaped it (the relative_to() above failed) - callers such as
    # guardrails.check_outside_project rely on os.path.isabs() of this return
    # value to detect that. Stripping "/" indiscriminately would erase the
    # one signal that carries, so only trim the leading slash of paths that
    # were never anchored to a project in the first place.
    if candidate.is_absolute() and project_dir:
        return result.rstrip("/") or "/"
    return result.strip("/") or "."
