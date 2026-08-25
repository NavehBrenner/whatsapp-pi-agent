"""Push a branch the agent already committed, and never anything else.

THE ONE THING THAT MUST NOT BREAK: git's stderr never reaches the model.

`sync.py` puts git's stderr in its exception message on purpose — it fetches a public
repo and a failed fetch is more useful to a human when it says why. This module holds a
token. Git prints the remote URL in its error text, credential helpers can be made to
print into it, and the model's transcript is the one place that must never carry a
credential. So every failure here is reported as one of a fixed set of strings, and the
detail goes to this process's stderr, which is the journal.

Not a general git wrapper. Four operations, in this order: validate the name, refuse
`main`, resolve the ref, push it fast-forward-only. The agent does everything else
itself — it has `exec` and a bind-mounted workspace, so branching, committing and
running the tests happen in its container against the same `.git` this reads.

No SDK import, same as `sync.py`: the tests exercise the logic without paying
`import mcp.server`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wpa_mcp.sync import TIMEOUT_SEC

# What a PR opens against, and the one branch this tool will not push. A constant
# rather than a parameter: `main` is the only base this repo has, and making it an
# argument would mean validating a second attacker-supplied ref for no gain.
BASE = "main"

# git's credential helper protocol is line-oriented key=value on stdout. `!` runs the
# rest through `sh`, so `$WPA_PUSH_TOKEN` expands out of the environment we hand to
# `subprocess.run` below — never from argv (readable by `ps`), never from `.git/config`
# (which outlives the process and lands in backups), never from the URL (which git
# echoes into its own error messages).
#
# The empty helper first is not decoration: `credential.helper` is a multi-valued
# config key, and an empty value resets the list. Without it a system-level helper
# configured on the box would be consulted first and could answer with the wrong
# credential, or block on a keyring prompt.
_CREDENTIAL_HELPER = (
    "!f(){ echo username=x-access-token; echo password=$WPA_PUSH_TOKEN; }; f"
)

# `check-ref-format` does the validation, so this only stops something absurd from
# reaching argv at all.
MAX_BRANCH_LEN = 200


class PushError(RuntimeError):
    """The push could not be attempted or failed unexpectedly.

    The message is safe to show — it is one of a fixed set written here, never git's
    output. A *refused* push is not this: refusals come back as a `PushResult` with
    `pushed=False`, because "origin has commits you don't" is an answer the agent can
    act on, not a fault.
    """


@dataclass(frozen=True)
class PushResult:
    """Where the branch ended up, and whether it moved.

    `sha` is empty when the push was refused before the ref was resolved — an invalid
    name, or `main`. There is nothing to report a sha for in those cases and inventing
    one would read as success.
    """

    branch: str
    sha: str
    base: str
    repo: str
    pushed: bool
    reason: str


def _run(repo: Path, token: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `repo` and hand the whole result back, errors included.

    Deliberately not `sync._git`, which raises on a non-zero exit with git's stderr in
    the message. Here every caller inspects the exit code itself, because "that branch
    does not exist" and "the remote rejected it" are outcomes to report rather than
    crashes — and because that message must not be built at all.
    """
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": "/usr/bin:/bin",
        # Not the invoking user's home: it keeps their git config, signing key and any
        # credential helper of their own out of a push made on the agent's behalf.
        "HOME": str(repo),
    }
    if token is not None:
        env["WPA_PUSH_TOKEN"] = token
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PushError(f"git timed out after {TIMEOUT_SEC}s") from exc


def _log(detail: str) -> None:
    """Send the unscrubbed detail where a human can read it and the model cannot."""
    print(f"wpa__push: {detail}", file=sys.stderr, flush=True)


def _origin_slug(repo: Path) -> str:
    """`owner/name` for origin, or "" if it cannot be read.

    Cosmetic — it exists so the agent can name the repo in the PR body without a second
    tool call. A failure here must not fail the push, which is why it returns a string
    rather than raising.
    """
    proc = _run(repo, None, "remote", "get-url", "origin")
    if proc.returncode != 0:
        return ""
    url = proc.stdout.strip().removesuffix(".git")
    # Handles https://host/owner/name and git@host:owner/name alike: the last two
    # path-ish segments are the slug in both.
    parts = url.replace(":", "/").rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def push(repo: Path, branch: str, token: str | None) -> PushResult:
    """Push `branch` to origin, fast-forward-only, or explain why it didn't.

        invalid name   rejected by `git check-ref-format`  -> refuse, no git contact
        `main`         protected, and not the agent's      -> refuse, no git contact
        no such branch nothing committed under that name   -> refuse
        non-ff         origin moved                        -> refuse, NEVER force
        otherwise      push it

    There is no `--force` path and no `--force-with-lease` path. Not forcing is the
    property this tool guarantees, not a mode it can be talked out of.
    """
    slug = _origin_slug(repo)

    def refused(reason: str, sha: str = "") -> PushResult:
        return PushResult(
            branch=branch, sha=sha, base=BASE, repo=slug, pushed=False, reason=reason
        )

    if not branch or len(branch) > MAX_BRANCH_LEN:
        return refused("branch name is empty or absurdly long")

    # Every git invocation below prefixes `refs/heads/`, so a name beginning with `-`
    # can never be read as an option — including this validation call. `check-ref-format`
    # is git's own rule set: it is what rejects `..`, a `.lock` suffix, `~^:?*[`, control
    # characters, `@{`, and a trailing `/`. Writing that as a regex here would be a worse
    # copy of a thing git already does correctly.
    if _run(repo, None, "check-ref-format", f"refs/heads/{branch}").returncode != 0:
        return refused("not a valid git branch name")

    if branch == BASE:
        # Branch protection would refuse this anyway; refusing here means the token is
        # never even offered for it, and the refusal does not depend on a GitHub setting
        # staying configured the way it is today.
        return refused(f"{BASE} is protected — push a branch and open a pull request")

    resolved = _run(repo, None, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if resolved.returncode != 0:
        return refused("no such branch in the workspace — commit it first")
    sha = resolved.stdout.strip()

    if token is None:
        raise PushError("push is not configured on this server")

    proc = _run(
        repo,
        token,
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={_CREDENTIAL_HELPER}",
        "push",
        "origin",
        # Fully-qualified on both sides: no `push.default` guesswork, no chance of a
        # name being taken for an option, and it fails rather than creating something
        # surprising if the remote has a tag by the same name.
        f"refs/heads/{branch}:refs/heads/{branch}",
    )
    if proc.returncode != 0:
        _log(f"push of {branch} failed: {proc.stderr.strip()}")
        stderr = proc.stderr.lower()
        if "non-fast-forward" in stderr or "fetch first" in stderr or "rejected" in stderr:
            return refused(
                "non-fast-forward — origin has commits this branch does not; "
                "rebase onto origin/main and push again",
                sha=sha,
            )
        # Everything else is a fault rather than an answer: bad credential, no network,
        # a remote that hung up. The detail is in the journal line above.
        raise PushError("push failed — see the gateway journal for git's output")

    return PushResult(
        branch=branch,
        sha=sha,
        base=BASE,
        repo=slug,
        pushed=True,
        reason=f"pushed {branch} at {sha[:8]}",
    )
