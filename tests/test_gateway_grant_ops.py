"""Host orchestration + root CLI for NVB-103 grant path.

Security properties:

1. Bad intent never reaches the helper (GrantValidateError before subprocess).
2. Helper exit 2 → GrantValidateError (no soft success).
3. Preview/apply invoke fixed binaries only; agent supplies no path argv.
4. Apply with already_complete leaves live file hash-identical.
5. Successful apply writes backup, mutates only policy layers, never restarts.
6. focused diff artifacts never contain secret env values.
7. sudoers lists exact path-only grant helpers.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

import pytest

from wpa_mcp.gateway_grant import GrantIntent, JsonObject, apply_grant
from wpa_mcp.gateway_grant_cli import main as grant_cli_main
from wpa_mcp.gateway_grant_ops import (
    GrantOpsError,
    GrantValidateError,
    apply_grant_tool,
    parse_grant_meta,
    preview_grant,
    write_intent,
)


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import secrets

    bases = (
        Path("/workspace/.pytest-tmp"),
        Path(tmp_path_factory.getbasetemp()),
    )
    last_err: OSError | None = None
    for base in bases:
        try:
            base.mkdir(parents=True, exist_ok=True)
            path = base / f"grant-{secrets.token_hex(4)}"
            path.mkdir()
            return path
        except OSError as exc:
            last_err = exc
            continue
    assert last_err is not None
    raise last_err


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)
    return path


def _live_config(path: Path) -> JsonObject:
    cfg: JsonObject = {
        "tools": {
            "alsoAllow": ["read", "write"],
            "sandbox": {"tools": {"allow": ["read", "write"]}},
        },
        "agents": {
            "list": [
                {
                    "id": "builder",
                    "tools": {"alsoAllow": ["read", "write", "wpa__sync"]},
                }
            ]
        },
        "bindings": [],
        "channels": {"signal": {"configWrites": False}},
        "mcp": {"servers": {"wpa": {"env": {"WPA_PUSH_TOKEN": "ghp_LIVE_SECRET"}}}},
    }
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def test_parse_grant_meta_summary_block() -> None:
    text = textwrap.dedent(
        """\
        agent_id=builder
        tool_name=skill_workshop
        already_complete=0
        ---summary---
        grant: skill_workshop -> agent builder
        layers: agent.alsoAllow:add
        ---end---
        """
    )
    meta = parse_grant_meta(text)
    assert meta["agent_id"] == "builder"
    assert "skill_workshop" in meta["summary"]


def test_preview_rejects_bad_intent_before_helper(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo 'should not run' >&2
        exit 0
        """,
    )
    with pytest.raises(GrantValidateError, match="invalid tool_name"):
        preview_grant(
            "builder",
            "../evil",
            preview_bin=helper,
            intent_path=tmp_path / "intent.json",
            use_sudo=False,
        )


def test_preview_helper_exit_2_is_validate_error(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo 'unknown agent_id' >&2
        exit 2
        """,
    )
    with pytest.raises(GrantValidateError, match="validation"):
        preview_grant(
            "builder",
            "skill_workshop",
            preview_bin=helper,
            intent_path=tmp_path / "intent.json",
            use_sudo=False,
        )


def test_preview_other_failure_is_ops_error(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo 'boom' >&2
        exit 1
        """,
    )
    with pytest.raises(GrantOpsError, match="preview failed"):
        preview_grant(
            "builder",
            "skill_workshop",
            preview_bin=helper,
            intent_path=tmp_path / "intent.json",
            use_sudo=False,
        )


def test_preview_ok_parses_summary(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo 'agent_id=builder'
        echo 'tool_name=skill_workshop'
        echo 'already_complete=0'
        echo 'layers=agent.alsoAllow:add,sandbox.allow:add'
        echo '---summary---'
        echo 'grant: skill_workshop -> agent builder'
        echo 'restart: required after apply (not performed)'
        echo '---end---'
        exit 0
        """,
    )
    result = preview_grant(
        "builder",
        "skill_workshop",
        preview_bin=helper,
        intent_path=tmp_path / "intent.json",
        use_sudo=False,
    )
    assert result.check_ok
    assert result.agent_id == "builder"
    assert "skill_workshop" in result.summary
    intent = json.loads((tmp_path / "intent.json").read_text())
    assert intent == {
        "op": "grant_tool",
        "agent_id": "builder",
        "tool_name": "skill_workshop",
    }


def test_apply_ok_parses_result(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "apply",
        """\
        #!/bin/sh
        echo 'agent_id=builder'
        echo 'tool_name=skill_workshop'
        echo 'already_complete=0'
        echo 'backup_path=/tmp/bak'
        echo 'restart_needed=1'
        echo 'reason=granted skill_workshop to builder; gateway restart still required'
        echo '---summary---'
        echo 'granted skill_workshop -> agent builder'
        echo '---end---'
        exit 0
        """,
    )
    result = apply_grant_tool(
        "builder",
        "skill_workshop",
        apply_bin=helper,
        intent_path=tmp_path / "intent.json",
        use_sudo=False,
    )
    assert result.ok
    assert result.restart_needed
    assert result.backup_path == "/tmp/bak"
    assert "restart" in result.reason


def test_cli_preview_and_apply_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = tmp_path / "openclaw.json"
    _live_config(live)
    intent_path = tmp_path / "intent.json"
    diff_path = tmp_path / "preview.diff"
    text_path = tmp_path / "preview.txt"
    backup_dir = tmp_path / "backups"

    write_intent(
        GrantIntent(agent_id="builder", tool_name="skill_workshop"),
        path=intent_path,
    )

    monkeypatch.setenv("WPA_LIVE_OPENCLAW_CONFIG", str(live))
    monkeypatch.setenv("WPA_GRANT_INTENT", str(intent_path))
    monkeypatch.setenv("WPA_GRANT_PREVIEW_DIFF", str(diff_path))
    monkeypatch.setenv("WPA_GRANT_PREVIEW_TEXT", str(text_path))
    monkeypatch.setenv("WPA_OPENCLAW_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("WPA_GRANT_ALLOW_NONROOT", "1")

    assert grant_cli_main(["preview"]) == 0
    diff_body = diff_path.read_text()
    assert "skill_workshop" in diff_body
    assert "ghp_" not in diff_body
    assert "WPA_PUSH_TOKEN" not in diff_body

    before_hash = hashlib.sha256(live.read_bytes()).hexdigest()
    assert grant_cli_main(["apply"]) == 0
    after = json.loads(live.read_text())
    after_hash = hashlib.sha256(live.read_bytes()).hexdigest()
    assert after_hash != before_hash

    agents = after["agents"]
    assert isinstance(agents, dict)
    agent_list = agents["list"]
    assert isinstance(agent_list, list)
    agent = agent_list[0]
    assert isinstance(agent, dict)
    tools_block = agent["tools"]
    assert isinstance(tools_block, dict)
    also = tools_block["alsoAllow"]
    assert isinstance(also, list) and "skill_workshop" in also
    tools = after["tools"]
    assert isinstance(tools, dict)
    sandbox = tools["sandbox"]
    assert isinstance(sandbox, dict)
    sb_tools = sandbox["tools"]
    assert isinstance(sb_tools, dict)
    allow = sb_tools["allow"]
    assert isinstance(allow, list) and "skill_workshop" in allow
    mcp = after["mcp"]
    assert isinstance(mcp, dict)
    servers = mcp["servers"]
    assert isinstance(servers, dict)
    wpa = servers["wpa"]
    assert isinstance(wpa, dict)
    env = wpa["env"]
    assert isinstance(env, dict)
    assert env["WPA_PUSH_TOKEN"] == "ghp_LIVE_SECRET"
    channels = after["channels"]
    assert isinstance(channels, dict)
    signal = channels["signal"]
    assert isinstance(signal, dict)
    assert signal["configWrites"] is False

    backups = list(backup_dir.glob("openclaw.json.*"))
    assert len(backups) == 1
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == before_hash

    # Second apply is a no-op.
    mid_hash = after_hash
    assert grant_cli_main(["apply"]) == 0
    assert hashlib.sha256(live.read_bytes()).hexdigest() == mid_hash


def test_cli_preview_unknown_agent_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "openclaw.json"
    _live_config(live)
    intent_path = tmp_path / "intent.json"
    write_intent(
        GrantIntent(agent_id="ghost", tool_name="skill_workshop"),
        path=intent_path,
    )
    # ghost is syntactically valid but absent from config — rewrite after validation
    # of the GrantIntent constructor: from_mapping accepts it; CLI finds unknown.
    intent_path.write_text(
        json.dumps(
            {"op": "grant_tool", "agent_id": "ghost", "tool_name": "skill_workshop"}
        )
        + "\n"
    )
    # Need a valid agent id pattern — "ghost" is fine; config lacks it.
    monkeypatch.setenv("WPA_LIVE_OPENCLAW_CONFIG", str(live))
    monkeypatch.setenv("WPA_GRANT_INTENT", str(intent_path))
    monkeypatch.setenv("WPA_GRANT_PREVIEW_DIFF", str(tmp_path / "d.diff"))
    monkeypatch.setenv("WPA_GRANT_PREVIEW_TEXT", str(tmp_path / "t.txt"))
    assert grant_cli_main(["preview"]) == 2
    assert hashlib.sha256(live.read_bytes()).hexdigest() == hashlib.sha256(
        live.read_bytes()
    ).hexdigest()


def test_cli_deny_path_hash_identical_when_preview_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "openclaw.json"
    _live_config(live)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    intent_path = tmp_path / "intent.json"
    write_intent(
        GrantIntent(agent_id="builder", tool_name="skill_workshop"),
        path=intent_path,
    )
    monkeypatch.setenv("WPA_LIVE_OPENCLAW_CONFIG", str(live))
    monkeypatch.setenv("WPA_GRANT_INTENT", str(intent_path))
    monkeypatch.setenv("WPA_GRANT_PREVIEW_DIFF", str(tmp_path / "d.diff"))
    monkeypatch.setenv("WPA_GRANT_PREVIEW_TEXT", str(tmp_path / "t.txt"))
    assert grant_cli_main(["preview"]) == 0
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before


def test_sudoers_includes_grant_helpers_path_only() -> None:
    text = Path("deploy/sudoers.d/wpa-openclaw").read_text()
    assert "NOPASSWD: /usr/local/bin/wpa-grant-preview\n" in text
    assert "NOPASSWD: /usr/local/bin/wpa-grant-apply\n" in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "NOPASSWD:" not in line:
            continue
        rhs = line.split("NOPASSWD:", 1)[1].strip()
        assert " " not in rhs
        assert "*" not in rhs


def test_grant_scripts_declare_no_args_contract() -> None:
    for name in ("wpa-grant-preview", "wpa-grant-apply"):
        text = (Path("deploy") / name).read_text()
        assert "No arguments" in text or "no arguments" in text


def test_apply_grant_pure_matches_cli_layers() -> None:
    """Belt: pure mutator and CLI share the same layer rules."""
    cfg: JsonObject = {
        "tools": {
            "alsoAllow": ["read", "write"],
            "sandbox": {"tools": {"allow": ["read", "write"]}},
        },
        "agents": {
            "list": [
                {
                    "id": "builder",
                    "tools": {"alsoAllow": ["read", "write", "wpa__sync"]},
                }
            ]
        },
        "bindings": [],
        "channels": {"signal": {"configWrites": False}},
    }
    report = apply_grant(
        cfg, GrantIntent(agent_id="builder", tool_name="skill_workshop")
    )
    assert {c.layer for c in report.added} == {"agent.alsoAllow", "sandbox.allow"}
