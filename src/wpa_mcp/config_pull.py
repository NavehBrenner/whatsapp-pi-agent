"""Seed the sandbox candidate config from the live gate config.

THE ONE THING THIS MUST NOT DO: write the live file.

Live `config/config.toml` is root:wpa-config 0640 and holds real people, group ids
and profile grants. The agent cannot read it from the sandbox. This tool asks a
fixed root helper to copy live → candidate under the builder workspace so the agent
can edit real ACIs there (NVB-37), then deploy them through `wpa__deploy`.

No SDK import — same reason as sync/push: tests exercise the logic without paying
`import mcp.server`.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wpa_mcp.paths import CANDIDATE_CONFIG, PULL_BIN

TIMEOUT_SEC = 30


class ConfigPullError(RuntimeError):
    """The seed could not run. Message is safe to show the model."""


@dataclass(frozen=True)
class ConfigPullResult:
    """What the candidate looks like after a pull.

    Bodies stay on disk — the tool result names paths and hashes so a routine pull
    does not dump live identifiers into the transcript by default. The agent `read`s
    the candidate when it needs to edit.
    """

    live_path: str
    candidate_path: str
    live_sha256: str
    candidate_sha256: str
    bytes_copied: int
    changed: bool
    reason: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_pull(
    *,
    pull_bin: Path = PULL_BIN,
    candidate: Path = CANDIDATE_CONFIG,
    use_sudo: bool = True,
) -> ConfigPullResult:
    """Run the fixed pull helper and describe the resulting candidate.

    `use_sudo` is true on the box (the helper is root-only) and false in tests that
    point `pull_bin` at a stand-in script already runnable as the test user.
    """
    before = _sha256(candidate) if candidate.is_file() else ""

    cmd = [str(pull_bin)] if not use_sudo else ["sudo", "-n", str(pull_bin)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigPullError(f"config pull timed out after {TIMEOUT_SEC}s") from exc
    except FileNotFoundError as exc:
        raise ConfigPullError(
            "config pull helper is not installed — run deploy/install.sh on the Pi"
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"wpa__config_pull: {detail}", file=sys.stderr, flush=True)
        raise ConfigPullError(
            "config pull failed — see the gateway journal for the helper's output"
        )

    if not candidate.is_file():
        raise ConfigPullError(
            f"pull reported success but candidate is missing at {candidate}"
        )

    after = _sha256(candidate)
    size = candidate.stat().st_size
    # The helper prints `live_sha256=...` for the live file; fall back to candidate
    # hash only if a test stub omitted it (live is unreadable to this process).
    live_hash = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("live_sha256="):
            live_hash = line.split("=", 1)[1].strip()
            break
    if not live_hash:
        live_hash = after

    live_path = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("live_path="):
            live_path = line.split("=", 1)[1].strip()
            break

    changed = before != after
    return ConfigPullResult(
        live_path=live_path or "/opt/wpa/config/config.toml",
        candidate_path=str(candidate),
        live_sha256=live_hash,
        candidate_sha256=after,
        bytes_copied=size,
        changed=changed,
        reason=(
            f"candidate {'updated' if changed else 'unchanged'} "
            f"({size} bytes, sha256 {after[:12]}…)"
        ),
    )
