"""Host orchestration for NVB-103 grant-tool apply.

THE AGENT SUPPLIES ONLY agent_id + tool_name. This module:

1. Writes a host-owned intent file (model never free-edits it).
2. Calls fixed root helpers for preview (validate + focused diff) and apply.
3. Never restarts the gateway — reports that a restart is still needed.

Live openclaw.json never enters the sandbox as a full candidate. The helpers
mutate a working copy under /run or /var/tmp as root, validate, then install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wpa_mcp.gateway_grant import GrantError, GrantIntent, intent_to_public_dict
from wpa_mcp.paths import (
    GRANT_APPLY_BIN,
    GRANT_INTENT,
    GRANT_PREVIEW_BIN,
    SUMMARY_MAX,
)

TIMEOUT_PREVIEW_SEC = 60
TIMEOUT_APPLY_SEC = 120


class GrantOpsError(RuntimeError):
    """Grant could not be attempted. Message is safe to show the model."""


class GrantValidateError(GrantOpsError):
    """Intent or resulting config failed validation before / at apply."""


@dataclass(frozen=True)
class GrantPreview:
    """Host-rendered approval text for one grant."""

    summary: str
    agent_id: str
    tool_name: str
    already_complete: bool
    layers: str
    check_ok: bool


@dataclass(frozen=True)
class GrantResult:
    """What apply did. Never claims the gateway reloaded."""

    ok: bool
    agent_id: str
    tool_name: str
    already_complete: bool
    backup_path: str
    output: str
    reason: str
    restart_needed: bool


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GrantOpsError(f"grant helper timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise GrantOpsError(
            "grant helper is not installed — run deploy/install.sh on the Pi"
        ) from exc


def _parse_meta(text: str) -> dict[str, str]:
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
        if "=" in line and not line.startswith(" ") and line[:1].isalnum():
            key, _, value = line.partition("=")
            if key.isidentifier():
                meta[key] = value
    if summary_lines:
        meta["summary"] = "\n".join(summary_lines).strip()
    return meta


def write_intent(intent: GrantIntent, *, path: Path = GRANT_INTENT) -> Path:
    """Host writes the only intent artifact the helpers will read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = intent_to_public_dict(intent)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def preview_grant(
    agent_id: str,
    tool_name: str,
    *,
    preview_bin: Path = GRANT_PREVIEW_BIN,
    intent_path: Path = GRANT_INTENT,
    use_sudo: bool = True,
) -> GrantPreview:
    """Validate + render focused policy diff. Blocks bad intent before approval."""
    try:
        intent = GrantIntent.from_mapping(
            {"op": "grant_tool", "agent_id": agent_id, "tool_name": tool_name}
        )
    except GrantError as exc:
        raise GrantValidateError(str(exc)) from exc

    write_intent(intent, path=intent_path)

    cmd = [str(preview_bin)] if not use_sudo else ["sudo", "-n", str(preview_bin)]
    proc = _run(cmd, timeout=TIMEOUT_PREVIEW_SEC)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 2:
        detail = stdout or stderr or "grant validation failed"
        print(
            f"wpa__gateway_grant_tool preview refused: {detail}",
            file=sys.stderr,
            flush=True,
        )
        raise GrantValidateError(
            "grant failed validation — refused before asking for approval. "
            f"Detail: {detail[:300]}"
        )
    if proc.returncode != 0:
        print(
            f"wpa__gateway_grant_tool preview failed: {stderr or stdout}",
            file=sys.stderr,
            flush=True,
        )
        raise GrantOpsError(
            "grant preview failed — see the gateway journal for the helper's output"
        )

    meta = _parse_meta(stdout)
    summary = meta.get("summary", stdout)
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 3] + "..."

    return GrantPreview(
        summary=summary,
        agent_id=meta.get("agent_id", intent.agent_id),
        tool_name=meta.get("tool_name", intent.tool_name),
        already_complete=meta.get("already_complete", "0") == "1",
        layers=meta.get("layers", ""),
        check_ok=True,
    )


def apply_grant_tool(
    agent_id: str,
    tool_name: str,
    *,
    apply_bin: Path = GRANT_APPLY_BIN,
    intent_path: Path = GRANT_INTENT,
    use_sudo: bool = True,
) -> GrantResult:
    """Re-validate then install the grant. Caller must already hold allow-once."""
    try:
        intent = GrantIntent.from_mapping(
            {"op": "grant_tool", "agent_id": agent_id, "tool_name": tool_name}
        )
    except GrantError as exc:
        raise GrantValidateError(str(exc)) from exc

    write_intent(intent, path=intent_path)

    cmd = [str(apply_bin)] if not use_sudo else ["sudo", "-n", str(apply_bin)]
    proc = _run(cmd, timeout=TIMEOUT_APPLY_SEC)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode == 2:
        detail = stderr or stdout or "grant validation failed at apply"
        print(
            f"wpa__gateway_grant_tool apply refused: {detail}",
            file=sys.stderr,
            flush=True,
        )
        raise GrantValidateError(
            "grant failed validation at apply time — nothing was modified. "
            f"Detail: {detail[:300]}"
        )
    if proc.returncode != 0:
        print(
            f"wpa__gateway_grant_tool apply failed: {stderr or stdout}",
            file=sys.stderr,
            flush=True,
        )
        tail = "\n".join(stdout.splitlines()[-40:]) if stdout else ""
        raise GrantOpsError(
            "grant apply failed — see the gateway journal. "
            + (f"Last output:\n{tail}" if tail else "")
        )

    meta = _parse_meta(stdout)
    already = meta.get("already_complete", "0") == "1"
    return GrantResult(
        ok=True,
        agent_id=meta.get("agent_id", intent.agent_id),
        tool_name=meta.get("tool_name", intent.tool_name),
        already_complete=already,
        backup_path=meta.get("backup_path", ""),
        output=stdout,
        reason=meta.get(
            "reason",
            f"granted {intent.tool_name} to {intent.agent_id}"
            + (" (already complete)" if already else "")
            + "; gateway restart still required",
        ),
        restart_needed=meta.get("restart_needed", "1") == "1",
    )


parse_grant_meta = _parse_meta
