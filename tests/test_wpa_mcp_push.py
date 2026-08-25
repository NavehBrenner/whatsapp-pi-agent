"""Tests for `wpa__push`.

Two properties, and both are security properties rather than features:

1. **Nothing is ever force-pushed, and `main` is never pushed at all.** If
   `test_a_non_fast_forward_is_reported_never_forced` fails, the tool has become able
   to destroy history on a branch someone is reviewing.
2. **git's stderr never reaches the caller.** The push carries a token; git prints the
   remote URL into its own error text and a helper can be made to print into it. Every
   assertion about a message here is checking that the *fixed* string came back and the
   raw one did not.

Real git repositories in a tmpdir, no mocks — same reasoning as `test_wpa_mcp.py`: the
behaviour under test is what `git push` actually does about a non-fast-forward, and a
mock would assert about itself.

The token passed below is a dummy on purpose. A local path remote never asks for a
credential, so the helper never runs; what the tests exercise is every decision around
it. The one thing they cannot cover is the credential itself working, which is why the
plan verifies that against GitHub on the Pi.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wpa_mcp.push import PushError, push

# Never reaches a remote — see the module docstring. Distinctive so a test can assert it
# did not leak into a message.
TOKEN = "dummy-token-ghp-not-real-8f3a"


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo`, failing the test loudly if it errors."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    """Add a file and commit it, returning the new sha."""
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A **bare** origin holding `main`, and a working clone of it.

    Bare is the difference from `test_wpa_mcp.py`'s fixture: pushing to a branch that a
    non-bare remote has checked out is refused by `receive.denyCurrentBranch`, which
    would make `test_main_is_refused` pass for entirely the wrong reason.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(origin))

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _identify(work)
    _commit(work, "first")
    _git(work, "push", "-q", "origin", "main")
    return origin, work


def _origin_sha(origin: Path, branch: str) -> str:
    """What origin thinks `branch` is, or "" if it has no such ref."""
    proc = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(origin), "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return proc.stdout.strip()


def test_an_existing_branch_reaches_origin(repos: tuple[Path, Path]) -> None:
    origin, work = repos
    _git(work, "checkout", "-q", "-b", "feature")
    sha = _commit(work, "second")

    result = push(work, "feature", TOKEN)

    assert result.pushed
    assert result.sha == sha, "the reported sha is the one that was pushed"
    assert _origin_sha(origin, "feature") == sha, "origin must actually have it"
    assert result.base == "main", "the PR's base, for the agent to pass on"


def test_main_is_refused(repos: tuple[Path, Path]) -> None:
    """The protected branch, refused here rather than left to GitHub's settings."""
    origin, work = repos
    before = _origin_sha(origin, "main")
    _commit(work, "sneaky")

    result = push(work, "main", TOKEN)

    assert not result.pushed
    assert "protected" in result.reason
    assert _origin_sha(origin, "main") == before, "main must not have moved"
    assert result.sha == "", "nothing was resolved, so there is no sha to report"


def test_a_branch_that_does_not_exist_is_refused(repos: tuple[Path, Path]) -> None:
    _origin, work = repos

    result = push(work, "no-such-branch", TOKEN)

    assert not result.pushed
    assert "no such branch" in result.reason


@pytest.mark.parametrize(
    "hostile",
    [
        "--upload-pack=/bin/sh",  # an option if it ever reached argv unqualified
        "--force",
        "../escape",
        "feature..x",
        "feature.lock",
        "with space",
        "trailing/",
        "",
        "x" * 300,
    ],
)
def test_a_hostile_branch_name_is_refused(repos: tuple[Path, Path], hostile: str) -> None:
    """The name comes from the model, so it is untrusted input at a trust boundary."""
    _origin, work = repos

    result = push(work, hostile, TOKEN)

    assert not result.pushed
    assert result.sha == ""


def test_a_non_fast_forward_is_reported_never_forced(repos: tuple[Path, Path]) -> None:
    """The property that makes this tool safe to give an agent."""
    origin, work = repos

    # Someone else's commit lands on the branch first.
    other = work.parent / "other"
    _git(work.parent, "clone", "-q", str(origin), str(other))
    _identify(other)
    _git(other, "checkout", "-q", "-b", "feature")
    theirs = _commit(other, "theirs")
    _git(other, "push", "-q", "origin", "feature")

    # The agent's branch of the same name diverges from it.
    _git(work, "checkout", "-q", "-b", "feature")
    _commit(work, "mine")

    result = push(work, "feature", TOKEN)

    assert not result.pushed
    assert "non-fast-forward" in result.reason
    assert _origin_sha(origin, "feature") == theirs, "the other commit must survive"


def test_a_refusal_carries_no_git_output_and_no_token(repos: tuple[Path, Path]) -> None:
    """A rejection message is written here, not copied out of git."""
    origin, work = repos
    other = work.parent / "other"
    _git(work.parent, "clone", "-q", str(origin), str(other))
    _identify(other)
    _git(other, "checkout", "-q", "-b", "feature")
    _commit(other, "theirs")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "checkout", "-q", "-b", "feature")
    _commit(work, "mine")

    reason = push(work, "feature", TOKEN).reason

    assert TOKEN not in reason
    assert str(origin) not in reason, "the remote URL is git's to print, not ours"
    for leak in ("error:", "hint:", "fatal:", "! [rejected]"):
        assert leak not in reason


def test_an_unreachable_remote_raises_a_fixed_message(tmp_path: Path) -> None:
    """Not a refusal — a fault. Still nothing of git's in what the caller sees."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    _git(lonely, "init", "-q", "--initial-branch=main")
    _identify(lonely)
    _commit(lonely, "only")
    _git(lonely, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    _git(lonely, "checkout", "-q", "-b", "feature")
    _commit(lonely, "work")

    with pytest.raises(PushError) as caught:
        push(lonely, "feature", TOKEN)

    message = str(caught.value)
    assert "journal" in message, "the detail is in the journal, and the message says so"
    assert TOKEN not in message
    assert "nowhere.git" not in message


def test_no_token_configured_is_an_error_not_a_silent_no_op(repos: tuple[Path, Path]) -> None:
    """A box without the credential must fail loudly here and still serve `wpa__sync`."""
    _origin, work = repos
    _git(work, "checkout", "-q", "-b", "feature")
    _commit(work, "second")

    with pytest.raises(PushError, match="not configured"):
        push(work, "feature", None)


def test_the_repo_slug_is_owner_name(tmp_path: Path) -> None:
    """So the agent can name the repo in the PR body without a second tool call."""
    repo = tmp_path / "slug"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _identify(repo)
    _commit(repo, "only")
    _git(repo, "remote", "add", "origin", "https://github.com/NavehBrenner/whatsapp-pi-agent.git")

    # `main` is refused without touching the remote, which is all this needs.
    assert push(repo, "main", TOKEN).repo == "NavehBrenner/whatsapp-pi-agent"
