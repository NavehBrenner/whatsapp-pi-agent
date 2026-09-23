"""Host-fixed paths the wpa MCP tools act on.

Nothing here is taken from a tool argument. ADR 0009's shape: the host pins the
targets, the agent supplies intent only, so there is nothing to smuggle through a
parameter. Override via env only for tests and for the rare install that does not
use the default layout.
"""

from __future__ import annotations

import os
from pathlib import Path

# The builder's checkout. Same default as __main__.py / the openclaw.json server entry.
REPO = Path(
    os.environ.get("WPA_REPO", "/var/lib/openclaw/.openclaw/workspace-builder/repo")
)

# Workspace root — parent of the checkout when using the default layout. Candidate
# config lives here deliberately, *outside* the git tree, so a confused `git add -A`
# cannot stage real ACIs into a commit.
WORKSPACE = Path(
    os.environ.get(
        "WPA_WORKSPACE",
        str(REPO.parent),
    )
)

# Live gate config on the box. root:wpa-config 0640; the gateway cannot read it.
LIVE_CONFIG = Path(os.environ.get("WPA_LIVE_CONFIG", "/opt/wpa/config/config.toml"))

# Candidate the agent edits. Real ACIs (NVB-37 decision). openclaw-owned after pull.
CANDIDATE_CONFIG = Path(
    os.environ.get(
        "WPA_CANDIDATE_CONFIG",
        str(WORKSPACE / "config" / "config.toml"),
    )
)

# Where preview writes the full unified diff (Signal's description is capped at 512).
PREVIEW_DIFF = Path(
    os.environ.get(
        "WPA_PREVIEW_DIFF",
        str(WORKSPACE / "config" / "last-deploy-preview.diff"),
    )
)
PREVIEW_TEXT = Path(
    os.environ.get(
        "WPA_PREVIEW_TEXT",
        str(WORKSPACE / "config" / "last-deploy-preview.txt"),
    )
)

# Deployed tree install.sh and gate.signal run from.
OPT_WPA = Path(os.environ.get("WPA_OPT", "/opt/wpa"))

# Privileged helpers installed by deploy/install.sh. No arguments, ever.
APPLY_BIN = Path(os.environ.get("WPA_APPLY_BIN", "/usr/local/bin/wpa-apply"))
PREVIEW_BIN = Path(os.environ.get("WPA_PREVIEW_BIN", "/usr/local/bin/wpa-apply-preview"))
PULL_BIN = Path(os.environ.get("WPA_PULL_BIN", "/usr/local/bin/wpa-config-pull"))

# Signal approval description budget (core clamps at 512; keep headroom for the warning).
DESCRIPTION_MAX = 512
SUMMARY_MAX = 400

# Live gateway config. root-owned; the sandbox must never open this path for write.
# Default matches the Pi layout; override in tests via env.
LIVE_OPENCLAW_CONFIG = Path(
    os.environ.get(
        "WPA_LIVE_OPENCLAW_CONFIG",
        "/var/lib/openclaw/.openclaw/openclaw.json",
    )
)

# Privileged helpers for the NVB-103 grant path. No arguments, ever.
GRANT_PREVIEW_BIN = Path(
    os.environ.get("WPA_GRANT_PREVIEW_BIN", "/usr/local/bin/wpa-grant-preview")
)
GRANT_APPLY_BIN = Path(
    os.environ.get("WPA_GRANT_APPLY_BIN", "/usr/local/bin/wpa-grant-apply")
)

# Host-written grant intent spool. MUST stay outside the sandbox bind mount.
# The wpa-approve before_tool_call hook stages it from validated event.params;
# the MCP tool body must never overwrite it (compare-and-refuse on mismatch).
# Default: /run/wpa/grant-intent.json (install.sh: root:openclaw 0770 dir).
GRANT_INTENT = Path(
    os.environ.get("WPA_GRANT_INTENT", "/run/wpa/grant-intent.json")
)

# Focused redacted policy diff/text for humans (and the agent after the fact).
# These are secret-free snapshots; workspace is fine. Intent is not.
GRANT_PREVIEW_DIFF = Path(
    os.environ.get(
        "WPA_GRANT_PREVIEW_DIFF",
        str(WORKSPACE / "config" / "last-grant-preview.diff"),
    )
)
GRANT_PREVIEW_TEXT = Path(
    os.environ.get(
        "WPA_GRANT_PREVIEW_TEXT",
        str(WORKSPACE / "config" / "last-grant-preview.txt"),
    )
)
