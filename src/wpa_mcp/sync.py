"""Advance the builder's checkout to `origin/main`, or explain why it didn't.

THE ONE THING THAT MUST NOT BREAK: this never discards uncommitted work.

`deploy/sync-project-repo.sh` force-heals — `reset --hard` plus `clean -qfd` on every
tick — and that is right for the reviewer's mirror, which is read-only to the agent
looking at it. This workspace is the opposite: `builder` *authors* here. The same
policy would be data loss on a 60-second timer, so a dirty or diverged tree is
reported and left exactly as it was. Discarding an author's work is a decision, and a
decision belongs to a human.

For the same reason this checkout must never join `wpa-project-sync.timer`.

No SDK import here on purpose. The protocol binding lives in `__main__.py`, so the
tests exercise the logic without paying `import mcp.server` — 1.15s on the Pi — and
without a JSON-RPC round trip standing between a failure and its cause.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# A fetch over a slow uplink is the slow case; past this the turn is better off
# hearing "it timed out" than waiting. git also inherits GIT_TERMINAL_PROMPT=0 below,
# so a credential prompt fails immediately instead of blocking until this expires.
TIMEOUT_SEC = 60


class SyncError(RuntimeError):
    """A git command failed. The message is for stderr, not for the tool result."""


@dataclass(frozen=True)
class SyncResult:
    """What the checkout looks like after a sync attempt.

    `advanced` is the only field that says whether anything moved; `reason` says why
    it did not. Both are here because "sync returned successfully" and "the tree is
    now at origin/main" are different claims, and the agent needs the second one.
    """

    commit: str
    subject: str
    behind: int
    dirty: bool
    advanced: bool
    reason: str
    fetched_at: str


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return its stdout, stripped."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            check=False,
            # Without this a credential prompt on a private remote blocks until the
            # timeout with no output. The remote is public, so needing a credential
            # at all is a misconfiguration worth failing fast on.
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": str(repo)},
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {' '.join(args)} timed out after {TIMEOUT_SEC}s") from exc
    if proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Is `ancestor` reachable from `descendant`? Exit code 1 means no, not an error."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SEC,
        check=False,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    if proc.returncode not in (0, 1):
        raise SyncError(f"git merge-base failed: {proc.stderr.strip()}")
    return proc.returncode == 0


def sync(repo: Path) -> SyncResult:
    """Fetch `origin/main` and fast-forward onto it when that is safe.

    The fetch always runs — it writes only to `.git`, so it is safe on any tree, and
    it is what makes `behind` a real number rather than a guess. What the fetch
    cannot decide is whether to move the working tree, and that is the whole
    judgement here:

      dirty      uncommitted changes    -> report, change nothing
      diverged   local commits          -> report, change nothing
      behind     clean and catchable    -> fast-forward
      current    already there          -> say so
    """
    _git(repo, "fetch", "--quiet", "origin", "main")
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    dirty = _git(repo, "status", "--porcelain") != ""
    head = _git(repo, "rev-parse", "HEAD")
    upstream = _git(repo, "rev-parse", "origin/main")

    # Counted before any fast-forward, so the number describes what the caller's
    # checkout was missing rather than always reading zero.
    behind = int(_git(repo, "rev-list", "--count", f"HEAD..origin/{'main'}"))

    if head == upstream:
        reason, advanced = "already at origin/main", False
    elif dirty:
        # Deliberately checked after `head == upstream`: a dirty tree that is already
        # current has nothing to report beyond being current.
        reason, advanced = "uncommitted changes — left untouched", False
    elif not _is_ancestor(repo, head, upstream):
        # Local commits. A fast-forward is impossible and a reset would delete them.
        reason, advanced = "local commits not on origin/main — left untouched", False
    else:
        _git(repo, "merge", "--ff-only", "origin/main")
        reason, advanced = f"fast-forwarded {behind} commit(s)", True

    return SyncResult(
        commit=_git(repo, "rev-parse", "HEAD"),
        subject=_git(repo, "log", "-1", "--pretty=%s"),
        behind=behind,
        dirty=dirty,
        advanced=advanced,
        reason=reason,
        fetched_at=fetched_at,
    )
