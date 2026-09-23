# 0014 — Governed gateway-config grants (intent tools, not free edit)

**Status:** Accepted 2026-09-22 ([NVB-103](https://linear.app/naveh-brenner/issue/NVB-103))
**Amends:** [0011](0011-openclaw-owns-the-channel-the-gate-owns-the-room.md) only in the narrow sense that *some* gateway-config mutations may now go through MCP + sudoers + approval — not through the Signal channel.
**Related:** [0009](0009-agents-are-containers-that-ask-by-name.md), [0010](0010-profiles-are-pre-bound-grant-bundles.md), [runbook 07](../runbooks/07-wpa-deploy.md), [runbook 08](../runbooks/08-gateway-grant.md), NVB-37

## Context

NVB-37 and ADR 0011 deliberately kept `openclaw.json` out of agent deploy:

- `channels.signal.configWrites: false` — a channel that can write config is a privilege-escalation path reachable by prompt injection.
- Gate `config.toml` is already the scary agent-authored input at deploy time; gateway config is scarier (tools, agents, bindings, plugins, secrets in MCP env).

That left no path for builder to grant a tool (e.g. `skill_workshop` for NVB-102) without a human hand-edit on the box.

## Decision

**v1 is option B: narrow intent tools.** Not full-file candidate edit of `openclaw.json`.

1. **Typed MCP args only** — `wpa__gateway_grant_tool(agent_id, tool_name)`. The model never free-edits a mutation artifact.
2. **Hook stages intent outside the sandbox mount** — `wpa-approve` `before_tool_call` validates `event.params` and writes `/run/wpa/grant-intent.json` (root:openclaw spool from install.sh). The MCP tool body **never overwrites** that file; on allow-once it compares typed args to the spool and refuses on mismatch (closes the approve-one/apply-another swap).
3. **Host mutates a working copy** of live config outside the sandbox, applying structured multi-layer rules:
   - agent `tools.alsoAllow` (seed from global on first alsoAllow — replaces, does not merge)
   - room `channels.signal.groups[*].tools.allow` only when a group binding already has a ceiling
   - `tools.sandbox.tools.allow`
4. **Validate before approval** — bad intent / unknown agent / self-grant of the grant tool / failed schema check never reaches YES.
5. **Host-rendered focused policy diff** in the approval prompt (redacted; no MCP `env` values).
6. **allow-once only** — `wpa-approve` gates the tool; no `allow-always`.
7. **Atomic install + dated backup** (O_EXCL temp in live.parent); **report restart required, never restart**.
8. **Live full JSON never pulled into the workspace** as a candidate. Secrets stay out of sandbox diffs. Intent is not workspace-resident either.
9. **`configWrites` stays false** — this path is MCP intent + fixed sudoers helpers, not channel configWrites.

## What approval buys (honest)

Same residual risk as NVB-37: a compromised gateway uid already holds the sudoers helpers (NVB-22). Approval means a human saw the host diff for this intent; it does not bound a compromised gateway.

## Consequences

- Unblocks NVB-102-class grants without hand-editing `openclaw.json` on every tool.
- Each new class of gateway mutation (enable plugin, add agent, add binding) needs its own intentional tool — preferred over general JSON edit.
- Full-file apply (option A) remains rejected for v1; hybrid C is a later destination only after redact + editable-subtree allowlists.

## Non-goals v1

- Auto gateway restart
- Editing `mcp.servers.*.env` / credentials
- allow-always / standing rewrite rights
- Channel-level `configWrites: true`
