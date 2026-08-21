#!/usr/bin/env bash
# Launch the `wpa` MCP server for the gateway. Installed as /usr/local/bin/wpa-mcp.
#
# WHY THIS SCRIPT EXISTS AT ALL, because three lines of exec looks like ceremony:
# OpenClaw REFUSES TO PASS `PYTHONPATH` to an stdio MCP server. It strips it and says
# so once, in the gateway log, at debug volume:
#
#   [bundle-mcp] server "wpa": env "PYTHONPATH" is blocked for stdio startup safety
#                and was ignored.
#   [bundle-mcp] failed to start server "wpa": McpError: MCP error -32000: Connection closed
#
# The second line is what `mcp probe` shows you, and on its own it reads as a crashing
# server rather than a stripped variable. Everything else in this repo puts the src
# tree on the path with `Environment=PYTHONPATH=/opt/wpa/src` in a unit file, which
# works fine — systemd is not OpenClaw. An MCP server cannot, so the variable is set
# HERE, inside a process OpenClaw has already agreed to spawn.
#
# The alternative was to give the repo a build backend and `uv sync` the project into
# its own venv as an installed package. That is more conventional Python and it is a
# bigger change: it would make packaging decisions for wpa-reader and wpa-gate, which
# are happily unpackaged and have no dependencies to install. Revisit if a second
# component ever needs importing rather than executing.
#
# Not exec'd through `uv run`: that would resolve and possibly WRITE the lock at spawn
# time, on every turn, as a user with no cache dir. The venv is built once by
# install.sh; this just uses the interpreter in it.
set -euo pipefail

# Written by `deploy/install.sh` (uv sync --no-dev --locked). If it is missing, the
# deploy skipped or failed that step — say which, because "Connection closed" is all
# the gateway will report.
venv=/opt/wpa/.venv/bin/python
[ -x "$venv" ] || {
  echo "wpa-mcp: $venv missing — run deploy/install.sh (needs uv; see runbook 01)" >&2
  exit 1
}

# The gateway spawns this as `openclaw`, and /opt/wpa/src is root-owned, so a stray
# __pycache__ write would fail. It is also one less thing to clean out of a deploy.
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/opt/wpa/src

# WPA_REPO is deliberately NOT defaulted here — it comes from the server entry's `env`
# in openclaw.json, which is the one place a per-agent MCP server's configuration
# belongs (ADR 0013). The module has its own fallback for the same value.
exec "$venv" -m wpa_mcp "$@"
