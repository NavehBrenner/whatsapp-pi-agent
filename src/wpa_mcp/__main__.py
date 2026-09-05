"""The `wpa` MCP server — stdio, spawned by the gateway.

Stdio deliberately: no loopback port, so nothing to firewall. That is runbook 06's
rule and NVB-27's lesson, applied before it can bite.

⚠️ WHAT THIS PROCESS HOLDS: a GitHub push token in `WPA_PUSH_TOKEN` (NVB-36), and —
from NVB-37 — the ability to ask root to run three fixed binaries via sudoers
(`wpa-apply`, `wpa-apply-preview`, `wpa-config-pull`). The MCP server is a child of
`wpa-openclaw`, so a compromised gateway holds those rights whether or not a human
approved a particular call. That is NVB-22, accepted with the trigger named rather
than pretended away. What the per-call approval buys is that the *code* was merged
by a human and the *config* is the candidate whose host-rendered diff a human read.

ADR 0013 puts a tool credential in its own MCP server entry's `env`, one entry per
principal, and `builder` is the only agent that names `wpa__*`.

Two consequences worth carrying: this process is now worth compromising, so
`push.py` scrubs git's output rather than passing it to the model; and a rotated
token needs a **gateway restart**, not `openclaw mcp reload`, because the child is
spawned per turn from in-memory config.

⚠️ THE CLASS IS `MCPServer`, NOT `FastMCP`. `mcp.server.fastmcp` was removed in the
SDK's 2.0.0; every tutorial and most model memory still says FastMCP, and the failure
is an ImportError at spawn that the gateway reports as a dead server.

⚠️ THE TOOL NAME THE AGENT SEES IS NOT THE ONE BELOW. OpenClaw derives a prefix from
the `mcp.servers` key and joins it with `__`, and ADR 0013 records that long or
duplicate prefixes may be truncated or suffixed. `sync` here plus key `wpa` is
*expected* to resolve to `wpa__sync` — read it back from `openclaw mcp probe wpa
--json` before pinning it in a tool allowlist, never assume it.

Diagnostics go to stderr. The gateway shows a tool result to the model, so git's raw
stderr must not travel in one: it is attacker-influenceable only in theory here, but
the habit is the point, and a failed fetch is more useful to a human in the journal
than to the model mid-turn.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# Imported under another name so the decorated function below can be called `sync`:
# the SDK derives the input schema's title from the function name, and
# `sync_toolArguments` is a Python detail the model has no use for.
from wpa_mcp.config_pull import ConfigPullError, ConfigPullResult
from wpa_mcp.config_pull import config_pull as run_config_pull
from wpa_mcp.deploy import DeployCheckError, DeployError, DeployResult
from wpa_mcp.deploy import apply as run_apply
from wpa_mcp.deploy import preview as run_preview
from wpa_mcp.push import PushError, PushResult
from wpa_mcp.push import push as run_push
from wpa_mcp.sync import SyncError, SyncResult
from wpa_mcp.sync import sync as run_sync

# The one thing this server needs to know, and it comes from the server entry's `env`
# in openclaw.json rather than from an argument. ADR 0009's shape: the host fixes the
# target, the agent supplies nothing, so there is nothing to smuggle through.
REPO = Path(os.environ.get("WPA_REPO", "/var/lib/openclaw/.openclaw/workspace-builder/repo"))

# Read once at spawn, and absent is a supported state: a box with no push credential
# still serves `wpa__sync`. `push` then fails at call time with a message that says so,
# rather than taking the whole server down at import and turning a missing optional
# credential into "Connection closed".
PUSH_TOKEN = os.environ.get("WPA_PUSH_TOKEN") or None

server: MCPServer[None] = MCPServer(
    name="wpa",
    # `openclaw mcp probe` prints this, so an empty default would make two deployed
    # versions indistinguishable at exactly the moment someone is debugging which is
    # running. Tracks the version in pyproject.toml by hand — there is one of each.
    version="0.1.0",
    instructions=(
        "Tools for the checkout of this assistant's own source repository, and for "
        "deploying origin/main (plus an optional candidate gate config) onto the Pi."
    ),
)


@server.tool(
    name="sync",
    description=(
        "Fetch origin/main into the workspace checkout and fast-forward onto it when "
        "the tree is clean. Reports the resulting commit either way. Never discards "
        "uncommitted changes or local commits — if either is present it changes "
        "nothing and says so."
    ),
    # read_only is false because a fast-forward moves the working tree; destructive is
    # false because not discarding work is the property the code guarantees.
    #
    # ⚠️ SNAKE_CASE HERE, camelCase ON THE WIRE. The MCP spec spells these
    # `readOnlyHint`/`destructiveHint` and the server emits them that way, but SDK
    # 2.0.0's model takes the snake_case field names — the camelCase spelling is an
    # alias mypy rejects as an unexpected keyword.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def sync() -> SyncResult:
    """No parameters, which is the point — see the module docstring."""
    try:
        return run_sync(REPO)
    except SyncError as exc:
        print(f"wpa__sync: {exc}", file=sys.stderr, flush=True)
        raise


@server.tool(
    name="push",
    description=(
        "Push a branch you have already committed in this workspace to origin, so you "
        "can open a pull request for it. Fast-forward only and never forced: if origin "
        "has commits your branch does not, it says so and changes nothing — rebase and "
        "call it again. Refuses to push 'main'. Commit your work with git first; this "
        "pushes an existing branch and creates nothing. Returns the branch, its sha, "
        "the base to open the pull request against, and the repository — pass those to "
        "create_pull_request."
    ),
    # Not read-only: it publishes. Not destructive: refusing to force is the guarantee.
    # Idempotent because pushing a ref that is already there is a no-op at the remote.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def push(branch: str) -> PushResult:
    """`branch` is the only parameter, and the only untrusted input this server takes."""
    try:
        return run_push(REPO, branch, PUSH_TOKEN)
    except PushError as exc:
        # Already scrubbed — `push.py` builds these strings itself and logs git's own
        # output separately. Logged here too so the journal shows which tool failed.
        print(f"wpa__push: {exc}", file=sys.stderr, flush=True)
        raise


@server.tool(
    name="config_pull",
    description=(
        "Copy the live gate config into the builder workspace candidate path so you "
        "can edit real ACIs there. Never writes the live file. Returns paths and "
        "hashes, not the file body — read the candidate on disk when you need to edit. "
        "No parameters."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ),
)
def config_pull() -> ConfigPullResult:
    """No parameters — host-fixed paths only."""
    try:
        return run_config_pull()
    except ConfigPullError as exc:
        print(f"wpa__config_pull: {exc}", file=sys.stderr, flush=True)
        raise


@server.tool(
    name="deploy",
    description=(
        "Deploy origin/main onto the Pi, and install the workspace candidate gate "
        "config over the live one when present. No parameters: the code is whatever "
        "is already on origin/main, the config is the candidate on disk. Validates "
        "the candidate with gate.signal --check before asking for approval; a bad "
        "config never reaches a human decision. On approval runs a fixed root "
        "sequence (fetch+reset, install config, install.sh). Reports restart notices "
        "and never restarts anything itself. allow-once only — no standing grant."
    ),
    # Destructive in the operational sense (root mutates /opt/wpa). Still no force
    # push and no openclaw.json write — those stay out of scope.
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False
    ),
)
def deploy() -> DeployResult:
    """No parameters. Approval is enforced by the wpa-approve plugin before this runs.

    We still re-run preview's check path conceptually via apply's own --check, so a
    TOCTOU edit between prompt and YES cannot wedge the gate. Calling preview here
    would double-fetch; apply is the mutation and it validates again.
    """
    # Fail closed on a bad candidate *before* spending an approval, when the plugin
    # did not already block. The plugin's describe() also runs preview; this is the
    # belt for callers that bypass the plugin in tests or misconfiguration.
    try:
        run_preview()
    except DeployCheckError:
        raise
    except DeployError as exc:
        print(f"wpa__deploy: {exc}", file=sys.stderr, flush=True)
        raise
    try:
        return run_apply()
    except (DeployCheckError, DeployError) as exc:
        print(f"wpa__deploy: {exc}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    server.run()
