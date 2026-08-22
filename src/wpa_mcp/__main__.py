"""The `wpa` MCP server — stdio, spawned by the gateway, one tool.

Stdio deliberately: no loopback port, so nothing to firewall. That is runbook 06's
rule and NVB-27's lesson, applied before it can bite.

WHAT THIS PROCESS HOLDS: nothing. No credential, no token, no network reach beyond
`git fetch` against a public repo. Phase 2 ships the least dangerous tool on purpose,
so the server is proven before Phase 4 hangs an approval-gated deploy off it.

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
from wpa_mcp.sync import SyncError, SyncResult
from wpa_mcp.sync import sync as run_sync

# The one thing this server needs to know, and it comes from the server entry's `env`
# in openclaw.json rather than from an argument. ADR 0009's shape: the host fixes the
# target, the agent supplies nothing, so there is nothing to smuggle through.
REPO = Path(os.environ.get("WPA_REPO", "/var/lib/openclaw/.openclaw/workspace-builder/repo"))

server: MCPServer[None] = MCPServer(
    name="wpa",
    # `openclaw mcp probe` prints this, so an empty default would make two deployed
    # versions indistinguishable at exactly the moment someone is debugging which is
    # running. Tracks the version in pyproject.toml by hand — there is one of each.
    version="0.1.0",
    instructions="Tools for the checkout of this assistant's own source repository.",
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


if __name__ == "__main__":
    server.run()
