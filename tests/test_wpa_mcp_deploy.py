"""Tests for deploy preview/apply plumbing and config_pull.

Security properties under test:

1. A candidate that fails gate.signal --check never reaches apply mutation when the
   helper exits 2 — DeployCheckError, not a soft reason string.
2. Preview/apply invoke fixed binaries only; the agent supplies no path argv.
3. config_pull never claims success without a candidate file on disk afterwards.
4. Meta parsing ignores free-form noise and prefers the ---summary--- block.

Real subprocesses against stub helpers in a tmpdir — no mocks of the logic under
test. Root sudo is not exercised here; use_sudo=False points at the stubs.
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from wpa_mcp.config_pull import ConfigPullError, config_pull
from wpa_mcp.deploy import (
    DeployCheckError,
    DeployError,
    _parse_preview_meta,
    apply,
    preview,
)


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Temp dir that can hold executable stub helpers.

    Prefer `/workspace/.pytest-tmp` when it exists and is writable — the builder
    sandbox mounts the workspace rw and often mounts `/tmp` noexec, so shell
    stubs must live under the workspace there. CI has neither that path nor a
    noexec `/tmp`, so fall back to pytest's own temp root.
    """
    import secrets

    bases = (
        Path("/workspace/.pytest-tmp"),
        Path(tmp_path_factory.getbasetemp()),
    )
    last_err: OSError | None = None
    for base in bases:
        try:
            base.mkdir(parents=True, exist_ok=True)
            path = base / f"deploy-{secrets.token_hex(4)}"
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


def test_parse_preview_meta_reads_summary_block() -> None:
    text = textwrap.dedent(
        """\
        sha=abc123
        subject=hello world
        has_candidate=1
        noise that is not key=value with spaces = ignored carefully
        ---summary---
        main: abc123 hello
        config: none
        ---end---
        trailing
        """
    )
    meta = _parse_preview_meta(text)
    assert meta["sha"] == "abc123"
    assert meta["subject"] == "hello world"
    assert meta["has_candidate"] == "1"
    assert "main: abc123 hello" in meta["summary"]
    assert "config: none" in meta["summary"]


def test_preview_ok_returns_summary(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo 'sha=deadbeefcafebabe'
        echo 'subject=ship it'
        echo 'has_candidate=0'
        echo 'check=ok'
        echo '---summary---'
        echo 'main: deadbeef ship it'
        echo 'code: reset /opt/wpa → origin/main'
        echo 'config: none — live unchanged'
        echo '---end---'
        exit 0
        """,
    )
    result = preview(preview_bin=helper, use_sudo=False)
    assert result.check_ok
    assert not result.has_candidate
    assert result.target_sha == "deadbeefcafebabe"
    assert "live unchanged" in result.summary


def test_preview_check_failure_is_deploy_check_error(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo '---summary---'
        echo 'check: FAIL: no [signal] section'
        echo '---end---'
        echo 'check failed detail' >&2
        exit 2
        """,
    )
    with pytest.raises(DeployCheckError, match="gate.signal --check"):
        preview(preview_bin=helper, use_sudo=False)


def test_preview_other_failure_is_deploy_error(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "preview",
        """\
        #!/bin/sh
        echo 'fetch failed' >&2
        exit 1
        """,
    )
    with pytest.raises(DeployError, match="preview failed"):
        preview(preview_bin=helper, use_sudo=False)


def test_apply_ok_parses_result(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "apply",
        """\
        #!/bin/sh
        echo 'sha=abcdef0123456789'
        echo 'subject=deploy me'
        echo 'config_applied=1'
        echo 'reason=deployed abcdef01 config_applied=1'
        echo '---summary---'
        echo 'deployed abcdef01 deploy me'
        echo '---end---'
        echo 'install.sh would run here'
        exit 0
        """,
    )
    result = apply(apply_bin=helper, use_sudo=False)
    assert result.ok
    assert result.config_applied
    assert result.target_sha.startswith("abcdef")
    assert "install.sh would run here" in result.output


def test_apply_check_failure_refuses(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "apply",
        """\
        #!/bin/sh
        echo 'candidate bad' >&2
        exit 2
        """,
    )
    with pytest.raises(DeployCheckError, match="apply time"):
        apply(apply_bin=helper, use_sudo=False)


def test_config_pull_reads_helper_metadata(tmp_path: Path) -> None:
    candidate = tmp_path / "cand.toml"
    live_hash = "a" * 64
    helper = _write_exec(
        tmp_path / "pull",
        f"""\
        #!/bin/sh
        echo 'content' > '{candidate}'
        echo 'live_path=/opt/wpa/config/config.toml'
        echo 'live_sha256={live_hash}'
        echo 'candidate_sha256=ignored'
        exit 0
        """,
    )
    result = config_pull(pull_bin=helper, candidate=candidate, use_sudo=False)
    assert result.changed
    assert result.bytes_copied == len("content\n")
    assert result.live_sha256 == live_hash
    assert result.candidate_path == str(candidate)
    assert candidate.read_text() == "content\n"


def test_config_pull_missing_candidate_after_success_is_error(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "pull",
        """\
        #!/bin/sh
        echo 'live_sha256=abc'
        exit 0
        """,
    )
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigPullError, match="candidate is missing"):
        config_pull(pull_bin=helper, candidate=missing, use_sudo=False)


def test_config_pull_helper_failure(tmp_path: Path) -> None:
    helper = _write_exec(
        tmp_path / "pull",
        """\
        #!/bin/sh
        echo 'no live config' >&2
        exit 1
        """,
    )
    with pytest.raises(ConfigPullError, match="config pull failed"):
        config_pull(pull_bin=helper, candidate=tmp_path / "x.toml", use_sudo=False)


def test_sudoers_file_is_exact_paths_no_args() -> None:
    """The standing root grant is three fixed binaries — nothing else."""
    text = Path("deploy/sudoers.d/wpa-openclaw").read_text()
    assert "NOPASSWD: /usr/local/bin/wpa-apply\n" in text
    assert "NOPASSWD: /usr/local/bin/wpa-apply-preview\n" in text
    assert "NOPASSWD: /usr/local/bin/wpa-config-pull\n" in text
    assert "ALL=(ALL)" not in text
    # No argument placeholders after the binary names on the rule lines.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "NOPASSWD:" not in line:
            continue
        rhs = line.split("NOPASSWD:", 1)[1].strip()
        assert " " not in rhs, f"sudoers rule must be path-only, got {rhs!r}"
        assert "*" not in rhs


def test_apply_scripts_declare_no_args_contract() -> None:
    for name in ("wpa-apply", "wpa-apply-preview", "wpa-config-pull"):
        body = Path("deploy") / name
        text = body.read_text()
        assert "No arguments" in text or "no arguments" in text or "No arguments." in text
