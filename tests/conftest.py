"""Shared pytest fixtures."""

import pytest

import kuska


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
