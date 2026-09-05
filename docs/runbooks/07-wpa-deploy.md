# Runbook 07 — `wpa__deploy` and the candidate gate config

**NVB-37.** One approval-gated intent that puts `origin/main` on the Pi and optionally
installs a sandbox **candidate** over the live gate config. The agent chooses *when*,
never *what*.

This runbook is the operator half. The design lives in the issue and in
[AGENTS.md](../../AGENTS.md); the threat boundary is [ADR 0009](../decisions/0009-agents-are-containers-that-ask-by-name.md)
(intent, not a command) plus the sudoers honesty in NVB-37 itself.

---

## What exists

| Piece | Path / name |
|---|---|
| MCP tools | `wpa__sync`, `wpa__push`, `wpa__config_pull`, `wpa__deploy` |
| Candidate config | `/var/lib/openclaw/.openclaw/workspace-builder/config/config.toml` |
| Live config | `/opt/wpa/config/config.toml` (`root:wpa-config 0640`) |
| Preview helper | `/usr/local/bin/wpa-apply-preview` |
| Apply helper | `/usr/local/bin/wpa-apply` |
| Seed helper | `/usr/local/bin/wpa-config-pull` |
| Sudoers | `/etc/sudoers.d/wpa-openclaw` |
| Ask-first plugin | `wpa-approve` gates **`wpa__deploy` only** |

Candidate path is **outside the git checkout** on purpose: real ACIs, never staged by a
confused `git add -A`. Still under the builder workspace bind, so `read` / `write` work
inside the sandbox.

---

## First-time enable (human, on the box)

`install.sh` ships the helpers and the sudoers file. It does **not** edit
`openclaw.json`. After merging NVB-37 and running install once by hand:

```bash
cd /opt/wpa && sudo git pull && sudo deploy/install.sh
```

Then, on the live gateway config (mirror the comments in
`config/openclaw.example.json5`):

1. **MCP tool filter** — `mcp.servers.wpa.toolFilter.include` must list
   `sync`, `push`, `config_pull`, `deploy` (read the resolved names from
   `openclaw mcp probe wpa --json`, never assume the prefix).
2. **Builder allowlists** — agent-level `alsoAllow` *and* the builder room ceiling
   must both name `wpa__config_pull` and `wpa__deploy` (two edits; one alone is a
   silent no-op — same lesson as `wpa__sync`).
3. **Plugin loaded** — `plugins.load.paths` includes the installed plugin dir and
   `plugins.allow` includes `wpa-approve`.
4. **Approval route** — `approvals.plugin.enabled: true` with a target the owner can
   answer. No route ⇒ deploy is **blocked**, not hung (NVB-16 hardware lesson).
5. Restart the gateway when the plugin or MCP env changed:
   `sudo systemctl restart wpa-openclaw.service`

Verify privilege is exactly three binaries:

```bash
sudo -u openclaw sudo -l
# must show only wpa-apply, wpa-apply-preview, wpa-config-pull — no ALL, no args
```

### Two prerequisites `sudo -l` will not tell you about

Both were found the first time a human tried to use `wpa__deploy`, on 2026-09-05, and
both let `sudo -l` look perfectly correct while every call failed.

**1. `wpa-openclaw.service` must not set `NoNewPrivileges=`.** NNP is inherited by every
descendant and cannot be dropped, so `sudo` — setuid — can never escalate from the MCP
child. The symptom is `sudo: The "no new privileges" flag is set, which prevents sudo
from running as root`, raised before any approval prompt, on all three helpers.

`PrivateDevices=yes` has to go with it. It is seccomp-backed, and systemd re-implies
`NoNewPrivileges=yes` in order to install a seccomp filter on a `User=`-scoped unit.
**`systemctl show` lies about this** — it reported `NoNewPrivileges=no` while the running
process still had `NoNewPrivs: 1`. Check the process, not the property:

```bash
sudo grep NoNewPrivs /proc/$(systemctl show wpa-openclaw.service -p MainPID --value)/status
# NoNewPrivs:    0
```

`ProtectSystem=full` and `ProtectHome=yes` are mount-based, imply nothing, and stay. The
unit is hand-installed and **not** in `deploy/systemd/`, so `install.sh` will neither fix
nor undo this.

**2. git must accept `/opt/wpa` as safe for root.** The checkout is owned by a login user,
and root's exemption from the dubious-ownership check keys on `$SUDO_UID` — uid 1000 for a
human running `sudo git pull`, uid 991 for the gateway. So `git fetch` fails on the deploy
path and only on the deploy path, which is why no manual deploy ever revealed it.
`install.sh` now adds `safe.directory` system-wide; on a box that predates it:

```bash
sudo git config --system --add safe.directory /opt/wpa
```

Prove the whole privilege path in one command before handing it to the agent:

```bash
sudo -u openclaw sudo -n /usr/local/bin/wpa-apply-preview
# exit 0 and a summary block; anything else is one of the two above
```

---

## Agent flow

1. **Code** lands as a PR → you merge to `main` (branch protection).
2. Optional config: `wpa__config_pull` → edit candidate (real ACIs) with `read`/`write`.
3. `wpa__deploy` (no args):
   - helper runs `gate.signal --check` on the candidate when present
   - bad candidate → **refused before any approval prompt**
   - good (or no) candidate → Signal approval with host-rendered summary
4. You **allow-once** or **deny**. `allow-always` is not offered.
5. On allow: `sudo wpa-apply` → fetch + `reset --hard origin/main` → install candidate
   if any → `deploy/install.sh`. **Nothing is restarted**; the output names what still
   needs a restart.

Code-only deploy (no candidate file) is allowed; the prompt must say live config is
unchanged.

---

## Approval card (512-char budget)

The plugin warning plus a host summary roughly:

```
main: <sha8> <subject≤40>
code: reset /opt/wpa → origin/main
config: none — live unchanged | +N/-M live→cand | …
live:<sha8> cand:<sha8>
check: OK (…)
diff: …/last-deploy-preview.diff
```

Full unified diff:  
`/var/lib/openclaw/.openclaw/workspace-builder/config/last-deploy-preview.diff`  
Open it for large config edits; the Signal card will not hold a real diff.

---

## Recovery

- **Config**: `wpa-config-backup.path` copies every live write to
  `/var/backups/wpa-config/`. Restore with `install -o root -g wpa-config -m 0640`
  and `--check` before restarting the gate (runbook 04).
- **Code**: `git -C /opt/wpa log` / reset to a previous sha is a human call — no
  rollback tool.
- **Partial apply** (reset succeeded, install.sh failed): re-run
  `sudo /usr/local/bin/wpa-apply` or `sudo /opt/wpa/deploy/install.sh` by hand.

---

## Honesty about root

`openclaw` has `NOPASSWD` on those three paths, and the MCP child inherits it **only
because `wpa-openclaw.service` no longer sets `NoNewPrivileges=`** — see the two
prerequisites above. A compromised **gateway** is root on this path whether or not you
tapped allow-once. The sandbox bounds a compromised **agent**. Closing the gateway half
is [NVB-22](https://linear.app/naveh-brenner/issue/NVB-22/containerize-the-gateway-when-the-trigger-fires).

That sentence used to read "the MCP child inherits that" with no condition, and it was
false for as long as it existed: the unit set `NoNewPrivileges=yes`, so the sudoers file
shipped, installed, and could never be used.

What approval actually buys: merged code + a config diff a human saw.

---

## Out of scope here

- Writing `openclaw.json` by tool
- Service restarts by tool ([NVB-48](https://linear.app/naveh-brenner/issue/NVB-48))
- Placeholder ACIs (rejected — real ACIs in the candidate)
- Rollback tool

---

## Checks after first supervised deploy

- [ ] deny left `/opt/wpa` and live config hash-identical
- [ ] allow-once moved `/opt/wpa` to `origin/main` and ran install.sh
- [ ] candidate install kept `root:wpa-config 0640` and fired a config backup
- [ ] bad candidate never produced an approval prompt
- [ ] `allow-always` / ♾️ not offered
- [ ] `deploy/check-agent-auth.sh` still green
- [ ] restart notices only — no unit restarted by apply itself
