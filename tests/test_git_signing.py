"""Signing: kuska's branch commits are unsigned, squash_merge keeps signing.
No real GPG: a fake gpg.program records its calls in a marker file."""
import os
import stat
import subprocess

import pytest

from kuska import worktree


def _gpg_script(tmp_path, ok):
    marker = tmp_path / "gpg-called"
    script = tmp_path / ("gpg-ok" if ok else "gpg-fail")
    body = f'echo called >> "{marker}"\n'
    if ok:
        body += (
            "printf '\\n[GNUPG:] SIG_CREATED D 1 8 00 0 FAKE\\n' >&2\n"
            "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nZmFrZQ==\\n-----END PGP SIGNATURE-----\\n'\n"
        )
    else:
        body += "exit 1\n"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, marker


@pytest.fixture
def signing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in list(os.environ):
        if k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT") or k.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(k)

    def setup(ok):
        script, marker = _gpg_script(tmp_path, ok)
        (home / ".gitconfig").write_text(
            "[user]\n\tname = T\n\temail = t@example.com\n"
            "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n"
            f"[gpg]\n\tprogram = {script}\n"
        )
        return marker

    return setup


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)


def _unsigned_env():
    return {**os.environ, **worktree.unsigned_git_env()}


def _repo_with_commit(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    env = _unsigned_env()
    _git(d, "init", "-b", "main", env=env)
    (d / "a.txt").write_text("a\n")
    _git(d, "add", "-A", env=env)
    r = _git(d, "commit", "-m", "init", env=env)
    assert r.returncode == 0, r.stderr
    return d


def _has_gpgsig(repo, rev="HEAD"):
    return "gpgsig" in _git(repo, "cat-file", "commit", rev).stdout


def test_baseline_signing_is_forced(signing, tmp_path):
    marker = signing(False)
    d = tmp_path / "r"
    d.mkdir()
    _git(d, "init", "-b", "main")
    (d / "a").write_text("a")
    _git(d, "add", "-A")
    r = _git(d, "commit", "-m", "x")
    assert r.returncode != 0
    assert "gpg" in (r.stderr + r.stdout).lower()
    assert marker.exists()


def test_env_override_commits_unsigned(signing, tmp_path):
    marker = signing(False)
    d = tmp_path / "r"
    d.mkdir()
    env = _unsigned_env()
    _git(d, "init", "-b", "main", env=env)
    (d / "a").write_text("a")
    _git(d, "add", "-A", env=env)
    r = _git(d, "commit", "-m", "x", env=env)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    assert not _has_gpgsig(d)


def test_commit_all_is_unsigned(signing, tmp_path):
    marker = signing(False)
    d = _repo_with_commit(tmp_path)
    (d / "b.txt").write_text("b\n")
    sha = worktree.commit_all(d, "change")
    assert sha
    assert not _has_gpgsig(d)
    assert not marker.exists()


def test_rebase_onto_is_unsigned(signing, tmp_path):
    marker = signing(False)
    d = _repo_with_commit(tmp_path)
    env = _unsigned_env()
    _git(d, "checkout", "-b", "kuska/x", env=env)
    (d / "b.txt").write_text("b\n")
    assert worktree.commit_all(d, "branch work")
    _git(d, "checkout", "main", env=env)
    (d / "c.txt").write_text("c\n")
    _git(d, "add", "-A", env=env)
    assert _git(d, "commit", "-m", "base moves", env=env).returncode == 0
    _git(d, "checkout", "kuska/x", env=env)
    assert worktree.rebase_onto(d, "main") == (True, "")
    assert _git(d, "log", "--oneline", "main..HEAD").stdout.count("\n") == 1
    assert not _has_gpgsig(d)
    assert not marker.exists()


def test_squash_merge_keeps_signing(signing, tmp_path):
    marker = signing(True)
    d = _repo_with_commit(tmp_path)
    env = _unsigned_env()
    _git(d, "checkout", "-b", "kuska/x", env=env)
    (d / "b.txt").write_text("b\n")
    assert worktree.commit_all(d, "branch work")
    _git(d, "checkout", "main", env=env)
    status, sha = worktree.squash_merge(d, "kuska/x", "squashed")
    assert status == "merged", sha
    assert marker.exists()
    assert _has_gpgsig(d, sha)


def test_unsigned_git_env_appends_to_existing_count():
    env = worktree.unsigned_git_env(
        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x"}
    )
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_1"] == "commit.gpgsign"
    assert env["GIT_CONFIG_VALUE_1"] == "false"
    assert env["GIT_CONFIG_KEY_2"] == "tag.gpgsign"
    assert env["GIT_CONFIG_VALUE_2"] == "false"
    assert "GIT_CONFIG_KEY_0" not in env and "GIT_CONFIG_VALUE_0" not in env
