"""Shared pytest setup.

The big suites below are still plain scripts with a `main()` that exits at the
first failure. They run as subprocesses from `test_legacy_suites.py`; pytest
must not import them itself (`test_web.py` defines `test_web(project)`, which
pytest would call with fixtures that do not exist).
"""

import pytest

import kuska

collect_ignore = [
    "test_core.py",
    "test_daemon.py",
    "test_web.py",
    "test_worktree.py",
    "test_search.py",
    "test_guardrails.py",
    "test_concurrent_init.py",
    "eventfmt_test.py",
    "run_all.py",
]


@pytest.fixture
def project(tmp_path):
    """A bare project directory with one configured agent."""
    path = tmp_path / "proj"
    (path / ".agents" / "prompts").mkdir(parents=True)
    kuska.config_path(path).write_text(
        '[agents.dev-agent]\nbackend = "claude"\nrole = "builder"\n'
    )
    return path


@pytest.fixture
def conn(project):
    """An initialised database for `project`, with its agents registered."""
    db = kuska.connect(kuska.db_path(project))
    kuska.init_db(db)
    kuska.sync_agents_from_config(db, project)
    yield db
    db.close()
