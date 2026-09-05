"""Deploy origin/main (and optional candidate config) onto the Pi.

THE ONE THING THAT MUST NOT BREAK: the agent never chooses *what* is deployed.

`wpa__deploy` takes no parameters. Code is whatever already landed on `origin/main`
through a human-merged PR. Config is the candidate file on disk whose host-rendered
diff the owner just approved — or absent, in which case live config is left alone.
The privileged sequence lives in `/usr/local/bin/wpa-apply` and accepts no argv.

Validation of a present candidate (`gate.signal --check`) happens **before** the
owner is prompted (plugin + preview helper) and again inside apply (TOCTOU). A
config that cannot start must never be a YES away from wedging the gate.

No SDK import — same as sync/push.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wpa_mcp.paths import APPLY_BIN, PREVIEW_BIN, SUMMARY_MAX

TIMEOUT_PREVIEW_SEC = 60
TIMEOUT_APPLY_SEC = 600


class DeployError(RuntimeError):
    """Deploy could not be attempted, or the helper failed unexpectedly.

    Safe to show: fixed strings written here, never a free-form dump of root output
    beyond what apply deliberately printed as its summary trail.
    """


class DeployCheckError(DeployError):
    """Candidate failed `gate.signal --check`. Must block before any approval prompt."""


@dataclass(frozen=True)
class DeployPreview:
    """Host-rendered approval text plus whether the candidate is deployable."""

    summary: str
    check_ok: bool
    check_detail: str
    has_candidate: bool
    target_sha: str
    target_subject: str


@dataclass(frozen=True)
class DeployResult:
    """What apply did. `output` is install.sh's trail (restart notices included)."""

    ok: bool
    target_sha: str
    target_subject: str
    config_applied: bool
    output: str
    reason: str


def _run(
    cmd: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"deploy helper timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise DeployError(
            "deploy helper is not installed — run deploy/install.sh on the Pi"
        ) from exc


def preview(
    *,
    preview_bin: Path = PREVIEW_BIN,
    use_sudo: bool = True,
) -> DeployPreview:
    """Render the approval summary from disk artifacts. Never from model params.

    Exit codes from the helper:
      0  summary on stdout, check ok (or no candidate)
      2  candidate failed --check; stdout still holds a short reason
      1  other failure
    """
    cmd = [str(preview_bin)] if not use_sudo else ["sudo", "-n", str(preview_bin)]
    proc = _run(cmd, timeout=TIMEOUT_PREVIEW_SEC)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 2:
        detail = stdout or stderr or "candidate failed gate.signal --check"
        print(f"wpa__deploy preview check failed: {detail}", file=sys.stderr, flush=True)
        raise DeployCheckError(
            "candidate config failed gate.signal --check — "
            "refused before asking for approval. "
            f"Detail: {detail[:300]}"
        )
    if proc.returncode != 0:
        print(f"wpa__deploy preview failed: {stderr or stdout}", file=sys.stderr, flush=True)
        raise DeployError(
            "deploy preview failed — see the gateway journal for the helper's output"
        )

    meta = _parse_preview_meta(stdout)
    summary = meta.get("summary", stdout)
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 3] + "..."

    return DeployPreview(
        summary=summary,
        check_ok=True,
        check_detail=meta.get("check", "ok"),
        has_candidate=meta.get("has_candidate", "0") == "1",
        target_sha=meta.get("sha", ""),
        target_subject=meta.get("subject", ""),
    )


def apply(
    *,
    apply_bin: Path = APPLY_BIN,
    use_sudo: bool = True,
) -> DeployResult:
    """Run the fixed apply sequence after a human allow-once.

    Re-validates the candidate inside the helper. Does not restart any service —
    install.sh names what still needs a restart and leaves the timing to a human.
    """
    cmd = [str(apply_bin)] if not use_sudo else ["sudo", "-n", str(apply_bin)]
    proc = _run(cmd, timeout=TIMEOUT_APPLY_SEC)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 2:
        detail = stderr or stdout or "candidate failed check at apply time"
        print(f"wpa__deploy apply check failed: {detail}", file=sys.stderr, flush=True)
        raise DeployCheckError(
            "candidate failed gate.signal --check at apply time — nothing was modified. "
            f"Detail: {detail[:300]}"
        )
    if proc.returncode != 0:
        print(f"wpa__deploy apply failed: {stderr or stdout}", file=sys.stderr, flush=True)
        # Include the tail of stdout: install.sh progress is the operator trail and is
        # written by our scripts, not by git-with-a-token.
        tail = "\n".join(stdout.splitlines()[-40:]) if stdout else ""
        raise DeployError(
            "deploy apply failed — see the gateway journal. "
            + (f"Last output:\n{tail}" if tail else "")
        )

    meta = _parse_preview_meta(stdout)
    return DeployResult(
        ok=True,
        target_sha=meta.get("sha", ""),
        target_subject=meta.get("subject", ""),
        config_applied=meta.get("config_applied", "0") == "1",
        output=stdout,
        reason=meta.get(
            "reason",
            f"deployed {meta.get('sha', '')[:8] or 'origin/main'}",
        ),
    )


def _parse_preview_meta(text: str) -> dict[str, str]:
    """Read `key=value` lines the helpers emit; ignore the free-form rest."""
    meta: dict[str, str] = {}
    summary_lines: list[str] = []
    in_summary = False
    for line in text.splitlines():
        if line.strip() == "---summary---":
            in_summary = True
            continue
        if line.strip() == "---end---":
            in_summary = False
            continue
        if in_summary:
            summary_lines.append(line)
            continue
        if "=" in line and not line.startswith(" ") and line[0].isalnum():
            key, _, value = line.partition("=")
            if key.isidentifier():
                meta[key] = value
    if summary_lines:
        meta["summary"] = "\n".join(summary_lines).strip()
    return meta
