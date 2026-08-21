"""Tests for `wpa__sync`.

The one thing that must not break: a tree with uncommitted work, or with local
commits, comes back from a sync exactly as it went in. `builder` authors in this
checkout — the reviewer's mirror can force-heal because nobody is writing there, and
this one cannot. If `test_a_dirty_tree_is_left_alone` fails, read the docstring in
`wpa_mcp/sync.py` before "fixing" the test.

Real git repositories in a tmpdir rather than a mocked subprocess: the behaviour under
test is what git actually does with `merge --ff-only` and `merge-base --is-ancestor`,
which a mock would assert about itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wpa_mcp.sync import SyncError, sync


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo`, failing the test loudly if it errors."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        # Identity and hooks are per-repo below; keep the developer's own config,
        # signing key and templates out of it.
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    """Add a file and commit it, returning the new sha."""
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """An `origin` with one commit on main, and a clone of it.

    The clone is what `sync()` operates on; advancing `origin` is how a test makes the
    clone behind. A local path as the remote keeps the network out of the suite.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    _commit(origin, "first")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    return origin, clone


def test_a_clean_tree_advances_to_origin_main(repos: tuple[Path, Path]) -> None:
    origin, clone = repos
    ahead = _commit(origin, "second")

    result = sync(clone)

    assert result.advanced, "a clean tree one commit behind should fast-forward"
    assert result.commit == ahead, "the checkout should now be at origin/main"
    assert result.behind == 1, "behind counts what the checkout was missing"
    assert result.subject == "add second", "the subject describes the new HEAD"
    assert not result.dirty


def test_a_dirty_tree_is_left_alone(repos: tuple[Path, Path]) -> None:
    """The property that separates this from the reviewer's force-healing mirror."""
    origin, clone = repos
    _commit(origin, "second")
    before = _git(clone, "rev-parse", "HEAD")
    (clone / "work-in-progress").write_text("half a thought\n")

    result = sync(clone)

    assert not result.advanced, "a dirty tree must not be moved"
    assert result.dirty
    assert result.commit == before, "HEAD must not have moved"
    assert (clone / "work-in-progress").exists(), "uncommitted work must survive a sync"
    assert result.behind == 1, "it still reports how stale it is"
    assert "untouched" in result.reason


def test_a_tracked_file_edited_in_place_also_counts_as_dirty(repos: tuple[Path, Path]) -> None:
    """`clean -qfd` would spare this one; `reset --hard` would not. Neither runs."""
    origin, clone = repos
    _commit(origin, "second")
    (clone / "first").write_text("edited\n")

    result = sync(clone)

    assert not result.advanced
    assert result.dirty
    assert (clone / "first").read_text() == "edited\n", "the edit must survive"


def test_local_commits_are_not_discarded(repos: tuple[Path, Path]) -> None:
    """Diverged: a fast-forward is impossible and a reset would delete the work."""
    origin, clone = repos
    _commit(origin, "second")
    mine = _commit(clone, "mine")

    result = sync(clone)

    assert not result.advanced, "a diverged tree must not be moved"
    assert result.commit == mine, "the agent's own commit is still HEAD"
    assert not result.dirty, "committed work is not 'dirty' — this is the other guard"
    assert "local commits" in result.reason


def test_an_already_current_tree_reports_so(repos: tuple[Path, Path]) -> None:
    _origin, clone = repos

    result = sync(clone)

    assert not result.advanced, "nothing to do is not an advance"
    assert result.behind == 0
    assert result.reason == "already at origin/main"


def test_a_missing_remote_raises_rather_than_reporting_success(tmp_path: Path) -> None:
    """A failed fetch must not return a SyncResult — a stale sha would read as fresh."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    _git(lonely, "init", "-q", "--initial-branch=main")
    _git(lonely, "config", "user.email", "t@example.invalid")
    _git(lonely, "config", "user.name", "t")
    _commit(lonely, "only")

    with pytest.raises(SyncError):
        sync(lonely)
