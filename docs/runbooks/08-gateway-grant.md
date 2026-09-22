# Runbook 08 — `wpa__gateway_grant_tool` (NVB-103)

Approval-gated grant of **one existing tool name** to **one existing agent** in live
`openclaw.json` policy layers. The agent supplies intent only; host code owns the
mutation. Design: [ADR 0014](../decisions/0014-governed-gateway-config-grants.md).

This does **not** replace hand-edits for structural changes (new agents, plugins,
bindings). It is the vertical slice that unblocks common tool grants (e.g.
`skill_workshop`).

---

## What exists

| Piece | Path / name |
|---|---|
| MCP tool | `wpa__gateway_grant_tool` (probe the resolved name) |
| Preview helper | `/usr/local/bin/wpa-grant-preview` |
| Apply helper | `/usr/local/bin/wpa-grant-apply` |
| Intent artifact | `…/workspace-builder/config/last-grant-intent.json` (host-written) |
| Focused diff | `…/workspace-builder/config/last-grant-preview.diff` |
| Sudoers | same `/etc/sudoers.d/wpa-openclaw` as deploy (two extra binaries) |
| Ask-first plugin | `wpa-approve` gates **deploy and grant** |

Live file: `/var/lib/openclaw/.openclaw/openclaw.json`. The sandbox must never open it
for write. Full live JSON is **not** copied into the workspace.

---

## Layers the grant touches

1. **Agent** `agents.list[id].tools.alsoAllow`  
   First alsoAllow on an agent **seeds the global** `tools.alsoAllow` set, then appends
   (OpenClaw replaces rather than merges agent alsoAllow).
2. **Room ceilings** for group bindings that already have `tools.allow` — never invents
   a ceiling where none exists.
3. **Sandbox** `tools.sandbox.tools.allow` (sandboxed runs).

---

## First-time enable (human, on the box)

After merge + `sudo deploy/install.sh`:

1. **MCP tool filter** — `mcp.servers.wpa.toolFilter.include` must list
   `gateway_grant_tool` (and the existing tools). Probe:
   `openclaw mcp probe wpa --json`.
2. **Builder allowlists** — agent-level `alsoAllow` **and** the builder room ceiling
   must both name `wpa__gateway_grant_tool` (two edits; one alone is a silent no-op).
3. **Plugin** — `wpa-approve` already loaded for deploy; it now also gates the grant tool.
   Restart gateway after plugin install if the unit was already running an old copy:
   `sudo systemctl restart wpa-openclaw.service`
4. **Approvals route** — same as runbook 07; no route ⇒ grant is **blocked**, not hung.

Verify privilege:

```bash
sudo -u openclaw sudo -l
# must show wpa-apply, wpa-apply-preview, wpa-config-pull,
#           wpa-grant-preview, wpa-grant-apply — no ALL, no args
```

Prove the grant preview path:

```bash
# host must have written intent first; normally the MCP tool does that
sudo -u openclaw sudo -n /usr/local/bin/wpa-grant-preview
# exit 0 + summary, or exit 2 on validation failure
```

---

## Agent flow

1. Call `wpa__gateway_grant_tool(agent_id="builder", tool_name="skill_workshop")`
   (or another **existing** agent id / tool name).
2. Host writes intent → root preview validates + focused policy diff.
3. Bad intent / unknown agent / validate fail → **refused before approval**.
4. Good → Signal approval with host summary (allow-once / deny only).
5. On allow: re-validate → backup live → atomic install.
6. Output says **gateway restart still required**. Nothing restarts itself.

```bash
sudo systemctl restart wpa-openclaw.service
```

Deny leaves live `openclaw.json` hash-identical. A second grant of the same tool is a
no-op (`already_complete`).

---

## Approval card

Warning + host summary, roughly:

```
grant: skill_workshop -> agent builder
layers: agent.alsoAllow:add,room.allow[…]:add,sandbox.allow:add
already: no
check: ok (…)
diff: …/last-grant-preview.diff
restart: required after apply (not performed)
```

Open the focused diff for the real list changes. It must never contain MCP env / PAT
values.

---

## Recovery

- Backups: `/var/lib/openclaw/.openclaw/backups/openclaw.json.<UTC>` (or
  `WPA_OPENCLAW_BACKUP_DIR`). Restore by hand, then restart the gateway.
- Partial failure before install: live untouched (exit 2). After install: restore from
  the backup path printed in the apply summary.

---

## Honesty about root

Same as runbook 07: compromised **gateway** holds these helpers. Compromised **agent**
is still sandboxed. Approval is host diff review, not a bound on gateway compromise.

`channels.signal.configWrites` stays **false**.

---

## Checks after first supervised grant

- [ ] deny left live openclaw.json hash-identical
- [ ] bad tool / unknown agent never produced an approval prompt
- [ ] allow-once appended the tool on every required layer
- [ ] backup written; secrets absent from workspace diff
- [ ] `allow-always` / ♾️ not offered
- [ ] restart reported, not performed
- [ ] after human restart, tool is actually callable by that agent
