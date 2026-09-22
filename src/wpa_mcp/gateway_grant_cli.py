"""Root-side CLI for NVB-103 grant preview/apply.

Invoked only by ``/usr/local/bin/wpa-grant-{preview,apply}`` with a fixed
subcommand. Paths come from env defaults in ``wpa_mcp.paths`` — never from
agent-controlled argv beyond the subcommand name the wrapper hard-codes.

Does not restart the gateway. Writes a backup on apply. Live config is never
copied whole into the builder workspace.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from wpa_mcp.gateway_grant import (
    GrantError,
    GrantIntent,
    JsonObject,
    apply_grant,
    focused_policy_snapshot,
)
from wpa_mcp.paths import (
    GRANT_INTENT,
    GRANT_PREVIEW_DIFF,
    GRANT_PREVIEW_TEXT,
    LIVE_OPENCLAW_CONFIG,
)


def _load_json(path: Path) -> JsonObject:
    raw = path.read_text(encoding="utf-8")
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} root must be a JSON object")
    out: JsonObject = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise SystemExit(f"{path} keys must be strings")
        out[key] = value
    return out


def _load_intent(path: Path) -> GrantIntent:
    if not path.is_file():
        raise SystemExit(f"intent missing at {path}")
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"intent is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("intent must be a JSON object")
    typed: JsonObject = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise SystemExit("intent keys must be strings")
        typed[key] = value
    try:
        return GrantIntent.from_mapping(typed)
    except GrantError as exc:
        raise SystemExit(str(exc)) from exc


def _dump_json(data: JsonObject, path: Path) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _snapshot_text(snap: JsonObject) -> str:
    return json.dumps(snap, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _try_openclaw_validate(path: Path) -> tuple[bool, str]:
    """Best-effort schema check. Missing CLI is not a hard fail in v1."""
    exe = shutil.which("openclaw")
    if exe is None:
        return True, "ok (openclaw CLI absent — JSON + grant structure only)"
    for args in (
        [exe, "config", "validate", "--file", str(path)],
        [exe, "config", "check", "--file", str(path)],
    ):
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return True, "ok (openclaw config validate)"
        err = (proc.stderr or proc.stdout or "").strip()
        lower = err.lower()
        if "unknown" in lower or "invalid command" in lower:
            continue
        first = (err.splitlines() or ["openclaw validate failed"])[0][:200]
        return False, first
    return True, "ok (openclaw CLI present but no validate subcommand — JSON only)"


def _chown_openclaw(path: Path) -> None:
    try:
        pw = pwd.getpwnam("openclaw")
    except KeyError:
        return
    try:
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except OSError:
        pass
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _copy_config(before: JsonObject) -> JsonObject:
    return copy.deepcopy(before)


def cmd_preview(_args: argparse.Namespace) -> int:
    live = Path(os.environ.get("WPA_LIVE_OPENCLAW_CONFIG", str(LIVE_OPENCLAW_CONFIG)))
    intent_path = Path(os.environ.get("WPA_GRANT_INTENT", str(GRANT_INTENT)))
    diff_out = Path(os.environ.get("WPA_GRANT_PREVIEW_DIFF", str(GRANT_PREVIEW_DIFF)))
    text_out = Path(os.environ.get("WPA_GRANT_PREVIEW_TEXT", str(GRANT_PREVIEW_TEXT)))

    if not live.is_file():
        print(f"wpa-grant-preview: live config missing at {live}", file=sys.stderr)
        return 1

    try:
        intent = _load_intent(intent_path)
        before = _load_json(live)
    except SystemExit as exc:
        print(f"wpa-grant-preview: {exc}", file=sys.stderr)
        return 2

    after = _copy_config(before)

    try:
        report = apply_grant(after, intent, mutate=True)
    except GrantError as exc:
        print(f"wpa-grant-preview: {exc}", file=sys.stderr)
        return 2

    before_snap = focused_policy_snapshot(before, intent)
    after_snap = focused_policy_snapshot(after, intent)

    diff_out.parent.mkdir(parents=True, exist_ok=True)
    before_lines = _snapshot_text(before_snap).splitlines(keepends=True)
    after_lines = _snapshot_text(after_snap).splitlines(keepends=True)
    diff_body = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="live-policy",
            tofile="granted-policy",
        )
    )
    diff_out.write_text(diff_body, encoding="utf-8")
    _chown_openclaw(diff_out)

    with tempfile.TemporaryDirectory(prefix="wpa-grant-") as tmp:
        candidate = Path(tmp) / "openclaw.json"
        _dump_json(after, candidate)
        ok, check_line = _try_openclaw_validate(candidate)
        if not ok:
            print(f"wpa-grant-preview: validate failed: {check_line}", file=sys.stderr)
            return 2

    layers = ",".join(c.layer + ":" + c.action for c in report.changes)
    already = "1" if report.already_complete else "0"
    agent = intent.agent_id
    tool = intent.tool_name
    layers_disp = layers if layers else "(none)"
    already_disp = "yes" if report.already_complete else "no"

    summary_lines = [
        f"grant: {tool} -> agent {agent}",
        f"layers: {layers_disp}",
        f"already: {already_disp}",
        f"check: {check_line}",
        f"diff: {diff_out}",
        "restart: required after apply (not performed)",
    ]
    summary = "\n".join(summary_lines)
    if len(summary) > 400:
        summary = summary[:397] + "..."

    text_out.parent.mkdir(parents=True, exist_ok=True)
    text_out.write_text(summary + "\n", encoding="utf-8")
    _chown_openclaw(text_out)

    print(f"agent_id={agent}")
    print(f"tool_name={tool}")
    print(f"already_complete={already}")
    print(f"layers={layers}")
    print(f"check={check_line}")
    print(f"diff_path={diff_out}")
    print(f"live_path={live}")
    print("---summary---")
    print(summary)
    print("---end---")
    return 0


def cmd_apply(_args: argparse.Namespace) -> int:
    live = Path(os.environ.get("WPA_LIVE_OPENCLAW_CONFIG", str(LIVE_OPENCLAW_CONFIG)))
    intent_path = Path(os.environ.get("WPA_GRANT_INTENT", str(GRANT_INTENT)))
    backup_dir = Path(
        os.environ.get(
            "WPA_OPENCLAW_BACKUP_DIR",
            str(live.parent / "backups"),
        )
    )

    if os.geteuid() != 0:
        # Tests set WPA_GRANT_ALLOW_NONROOT=1 to exercise apply without root.
        if os.environ.get("WPA_GRANT_ALLOW_NONROOT") != "1":
            print("wpa-grant-apply: must run as root", file=sys.stderr)
            return 2

    if not live.is_file():
        print(f"wpa-grant-apply: live config missing at {live}", file=sys.stderr)
        return 1

    try:
        intent = _load_intent(intent_path)
        before = _load_json(live)
    except SystemExit as exc:
        print(f"wpa-grant-apply: {exc}", file=sys.stderr)
        return 2

    after = _copy_config(before)

    try:
        report = apply_grant(after, intent, mutate=True)
    except GrantError as exc:
        print(f"wpa-grant-apply: {exc}", file=sys.stderr)
        return 2

    if report.already_complete:
        print(f"agent_id={intent.agent_id}")
        print(f"tool_name={intent.tool_name}")
        print("already_complete=1")
        print("backup_path=")
        print("restart_needed=0")
        print("reason=already granted on every required layer — no write")
        print("---summary---")
        print(f"grant {intent.tool_name} -> {intent.agent_id}: already complete")
        print("---end---")
        return 0

    with tempfile.TemporaryDirectory(prefix="wpa-grant-") as tmp:
        candidate = Path(tmp) / "openclaw.json"
        _dump_json(after, candidate)
        ok, check_line = _try_openclaw_validate(candidate)
        if not ok:
            print(f"wpa-grant-apply: validate failed: {check_line}", file=sys.stderr)
            return 2

        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"openclaw.json.{stamp}"
        shutil.copy2(live, backup_path)
        try:
            backup_path.chmod(0o600)
        except OSError:
            pass

        st = live.stat()
        install_tmp = live.with_name(live.name + ".wpa-grant-tmp")
        shutil.copyfile(candidate, install_tmp)
        try:
            os.chown(install_tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        try:
            install_tmp.chmod(st.st_mode & 0o777)
        except OSError:
            pass
        os.replace(install_tmp, live)

    layers = ",".join(c.layer + ":" + c.action for c in report.changes)
    print(f"agent_id={intent.agent_id}")
    print(f"tool_name={intent.tool_name}")
    print("already_complete=0")
    print(f"layers={layers}")
    print(f"backup_path={backup_path}")
    print("restart_needed=1")
    print(f"check={check_line}")
    reason = (
        f"granted {intent.tool_name} to {intent.agent_id}; "
        "gateway restart still required"
    )
    print(f"reason={reason}")
    print("---summary---")
    print(f"granted {intent.tool_name} -> agent {intent.agent_id}")
    print(f"layers: {layers}")
    print(f"backup: {backup_path}")
    print("restart: REQUIRED (not performed)")
    print("---end---")
    print("wpa-grant-apply: done", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wpa-grant")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preview", help="validate + focused diff")
    sub.add_parser("apply", help="backup + install grant")
    args = parser.parse_args(argv)
    if args.cmd == "preview":
        return cmd_preview(args)
    if args.cmd == "apply":
        return cmd_apply(args)
    raise SystemExit(f"unknown cmd {args.cmd!r}")


if __name__ == "__main__":
    sys.exit(main())
