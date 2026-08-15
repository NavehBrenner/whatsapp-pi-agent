# Runbook 06 — the project room

A Signal group bound to one repository, served by its own agent, which can file and
comment on that repo's issues and is woken when the repo moves.

It exists so the owner can start coding work from his phone: describe the task, the
agent opens an issue and asks the runner for a plan, the plan comes back into the
room, he approves, a PR appears. [Runbook 05](05-opencode-ci-token.md) covers the
runner half; this covers the room.

## 0. Why it is a separate agent

`load_config` refuses one agent in two conversations ("an agent session may not span
conversations"), so the owner's DM agent cannot also serve this room. That refusal is
doing real work here rather than being a formality: this room receives issue bodies
and PR titles from a **public** repo, which is text a stranger wrote arriving at an
agent with durable memory. A shared session would carry a poisoned issue into the
private chat; a shared workspace would keep it in the same `MEMORY.md`.

The cost is that the project room does not remember the DM's context, and that is
the intended trade rather than a limitation to work around.

| | |
|---|---|
| Signal group | `mKfSyvnnWHQHR1Au6FjN8CEAM/02Y2iBPkGnsUgalKo=`, two members: the owner and the assistant |
| Gate conversation | `code-invariants`, agent `code-invariants`, profile `project` (`send_to` = `["self"]`) |
| OpenClaw agent | `code-invariants`, workspace `~/.openclaw/workspace-code-invariants` |
| Session key | `agent:code-invariants:signal:group:mKfSyvnn…` |

`members` is two entries because **the assistant is a member of its own group**.
Leave its ACI out and the room refuses everything forever, which reads exactly like
a bug — `deploy/pin-group.py` prints the correct block.

## 1. Deploying the agent

Four things, and skipping the last is the `liron` failure from #24:

```bash
# 1. gate: conversation + profile in config.toml, then
sudo -u wpa-gate PYTHONPATH=/opt/wpa/src python3 -m gate.signal --check /opt/wpa/config/config.toml

# 2. openclaw: agents.list entry, a binding (GROUP ids carry NO `uuid:` prefix), and
#    the room's tools.allow ceiling
sudo -u openclaw HOME=/var/lib/openclaw openclaw config validate

# 3. the workspace, with an IDENTITY.md — an agent without one answers
#    "I don't have a name yet"
sudo -u openclaw install -d -m 0700 /var/lib/openclaw/.openclaw/workspace-code-invariants

# 4. ITS OWN auth profile. Without this it reads through to `main` and appears to
#    work right up until `main` is actually empty.
sudo -u openclaw HOME=/var/lib/openclaw openclaw models auth --agent code-invariants login
```

`--agent` is an option on the **parent** command: `openclaw models auth --agent X
login`, never `… login --agent X`.

Verify the fourth with `openclaw models --agent code-invariants status` and read the
`effective=` path. If it names `agents/main/...`, the login did not take.

## 2. The GitHub MCP server

`/usr/local/bin/github-mcp-server` (v1.9.0, arm64, checksum-verified), spawned over
**stdio** by the gateway. Stdio deliberately: there is no loopback port, so nothing
to firewall — NVB-27's lesson applied before it could bite.

Three narrowings, weakest to strongest:

| Layer | Where |
|---|---|
| `toolFilter.include` | `mcp.servers.github` in `openclaw.json` |
| `--tools=…` | the server's own argv |
| **the PAT** | fine-grained, `Issues: read/write`, one repo, no contents |

The PAT is the floor and the only one that holds if the other two are misconfigured.

```bash
sudo -u openclaw HOME=/var/lib/openclaw openclaw mcp probe github --json
# expect: 4 tools, diagnostics: []
```

### `create_issue` does not exist

`list-scopes` advertises it, `--tools=create_issue` is accepted without complaint,
and **nothing registers**. The real tool is `issue_write`. Only the tool *count* in
`mcp probe` exposed it — the same silent-non-grant failure ADR 0011 records for
OpenClaw tool lists, appearing here in GitHub's own server.

It also costs the narrow verb ADR 0010 asks for: `issue_write` is a consolidated
write that can close and relabel, and there is no create-only tool. The containment
falls back to the PAT's scope.

**Put the tool count in the checklist, not just `config validate`.** `mcp probe`
returns tools and no diagnostics against a *dead* credential, because listing tools
does not authenticate.

### A rotated token needs a gateway restart

`openclaw mcp reload` disposes cached runtimes but does **not** re-read a changed
env. The MCP child is spawned per turn from the gateway's in-memory config, so after
editing the token you get 401s from a child holding the old one — while the file and
the config both hold the new one. Diagnosed by hashing:

```bash
sudo sh /path/to/check-mcp-token.sh    # file / config / live process, hashes only
```

Restart `wpa-openclaw` after any credential change.

## 3. The watcher

`wpa-gh-watch.timer` → `/usr/local/bin/wpa-gh-watch`, every 60s.

GitHub cannot push (the Pi is not reachable) and the agent cannot poll (no
scheduling tool, no sandbox network), so this box looks on its behalf. Idle ticks
cost one authenticated HTTPS call and **no model tokens**; a turn is spent only when
something happened. That is the whole reason it is not an `openclaw cron` job waking
the agent on a timer to find nothing.

It reads the token out of `mcp.servers.github.env` rather than keeping a second
copy, filters events authored by the token owner (so the agent is never woken about
its own `/oc` comments), and dedupes against a `seen` list.

Three things it watches: new issue comments, new or updated issues and PRs, and
**failed workflow runs**. The last is not author-filtered — a failure matters whoever
caused it, and the ones caused by the agent's own PRs are exactly the ones it must
act on. The `actions/runs` endpoint takes no `since`, so that window is applied
client-side, and the dedupe id carries the conclusion so a re-run that fails again is
a new event.

The Issues-scoped PAT reads `actions/runs` on a public repo without extra
permissions — verified, HTTP 200 — so no widening was needed for this.

The agent can then investigate it: `actions_get`, `actions_list` and `get_job_logs`
are granted read-only, so it pulls the failing job's log and reports the actual error
rather than guessing. `actions_run_trigger` — the fourth tool in that toolset — is
deliberately withheld: re-running a workflow is a write and the agent has no reason
to hold it. The Issues-scoped PAT reads all three on a public repo, verified before
they were granted.

### `openclaw system event` does not wake anything here

It is the obvious-looking call. It enqueues an event and returns `ok` **without
running a turn**, because the heartbeat that consumes the queue is suppressed by a
comments-only `HEARTBEAT.md`. `--expect-final` still returns `ok`. The working
primitive is:

```bash
openclaw agent --session-key "<key>" --deliver --timeout 120 --message "…"
```

`--deliver` alone suffices. And note the gateway logs **no** `delivered reply` line
for a CLI-initiated turn, so that log proves nothing either way — the room does.

### The cursor advances on wall-clock, not on events

Anchoring it to the newest event seen freezes the window on a quiet repo, so every
poll re-fetches a widening slice of history. With `per_page=50` and no pagination
that eventually drops events off the end — a quiet week silently breaking the first
busy day. It advances to `now - 5min` each run; the overlap plus the seen-list is
what makes that safe.

## 4. The code mirror

`wpa-project-sync.timer` → `/usr/local/bin/wpa-project-sync`, ticking every 60s.

`/workspace/repo` inside the sandbox is a shallow, single-branch mirror of `main`,
which the agent reads with the `read` tool it already has. **That is the whole point
of doing it this way**: the alternatives were exec plus a network in the sandbox
(undoing NVB-14/23/25) or GitHub repo tools (one file per call, a 5000-char window,
and a wider PAT). A directory appearing in a workspace it already reads grants
nothing new at all.

- **The agent decides when.** It cannot run the sync, but it can ask: writing any
  text into `/workspace/repo-sync.request` makes the next tick sync immediately and
  consumes the file. ADR 0009's shape — an intent, not a command, so there are no
  arguments to smuggle anything through. The write tool rejects an empty file, so
  the request needs a byte in it.
- **`reset --hard` and `clean`, not `pull`.** The workspace is read-write to the
  agent, so the checkout can have been edited. A mirror that force-heals is easier
  to reason about at 04:00 than one that wedges on a conflict, and the clone branch
  self-heals even if `.git` is deleted.
- **`repo-sync.json` is as much the point as the checkout.** It carries the commit,
  its subject and the fetch time, and lives outside the checkout so `git clean` does
  not remove it. A mirror that merely looks current is worse than none: the agent is
  told to state which commit it is reasoning from.
- The staleness ceiling with nobody asking is `max_age` in the script (10 min), not
  the timer interval. The timer's minute is the resolution at which a request is
  picked up.

git goes through libcurl, which does Happy Eyeballs and falls back to IPv4 on its
own — this is **not** the Go-resolver trap that needs `GODEBUG=netdns=cgo`.

## 5. Verifying without touching the repo

Rewind the cursor past an existing event and let it replay:

```bash
sudo sh -c 'echo 2026-08-14T18:00:00Z > /var/lib/wpa-gh-watch/cursor
            : > /var/lib/wpa-gh-watch/seen
            chown openclaw:openclaw /var/lib/wpa-gh-watch/{cursor,seen}'
sudo systemctl start wpa-gh-watch.service
sudo journalctl -u wpa-gh-watch -n 15 --no-pager     # expect a "waking …" line
```

The script rewrites a correct cursor on the way out, so this is self-cleaning.

**Probe design matters here.** An earlier check asked the agent for a count of open
issues, got `0`, and was taken as success — but the repo genuinely had zero, so the
success value and the failure value were identical and a dead credential passed. Ask
for something whose failure is distinguishable, or check a process's environment
rather than a model's report.

## 5a. DNS, and why every Go binary here struggled

The LAN router at `10.10.0.138` answers glibc's queries and does **not** answer the
Go resolver's. Measured against the GitHub MCP server on 2026-08-15:

```
router only        14 of 15 lookups failed   dial tcp: lookup api.github.com
                                             on 10.10.0.138:53: no such host
getent / curl      10 of 10 fine, HTTP 200
public resolver    15 of 15 fine
```

So this was never a blip. It reached a human three times as an agent saying "DNS
blip, retrying", which is exactly how a systematic failure hides — the paraphrase
sounded transient and nobody measured it until it was in the way.

**`GODEBUG=netdns=cgo` does not fix this class of binary.** It selects a resolver
that is not compiled into a `CGO_ENABLED=0` build, and `github-mcp-server` is
statically linked (`ldd` → "not a dynamic executable"). It was set on that MCP server
for a day and did nothing. It *did* genuinely fix dockerd, which is dynamically
linked — same symptom, same box, and only one of the two could take that cure. Check
the linkage before reaching for it.

The fix is `deploy/networkmanager/90-wpa-dns.conf` (`dns=none`) plus a hand-written
`/etc/resolv.conf` putting public resolvers first. Two things about the shape:

- **`dns=none`, not per-connection `ipv4.dns`.** NM merges DNS from every active
  connection, so setting it on eth0 alone left wlan0 injecting the router at the top
  of the list — observed, and the retest went straight back to 9 of 10 failing.
- **Reload, do not reactivate.** `systemctl reload NetworkManager` leaves links up.
  SSH to this box arrives over wlan0, so `nmcli con up preconfigured` would cut the
  session performing the fix.

The router stays as the last nameserver: it cannot serve the Go resolver, but it is
the only thing that knows LAN names, and a resolver only falls through to it on
timeout rather than NXDOMAIN.

## 6. When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| Room silent on new PRs/plans | watcher failing; a Signal DM should have said so | `journalctl -u wpa-gh-watch -n 50` |
| Agent reports 401 from GitHub | child spawned before a token change | restart `wpa-openclaw`, then hash file/config/process |
| A granted tool "is not available" | one of six policy layers stripped it | `journalctl -u wpa-openclaw \| grep tool-policy` — it names the layer |
| Agent answers in the wrong room | binding uses a `uuid:` prefix on a group id | group ids carry no prefix; check `sessions.json` |
| Everything replayed after a reboot | cursor lost | `StateDirectory=` owns it; check it exists and is `openclaw`-owned |
| Agent describes code that has changed | mirror stale or frozen | `repo-sync.json` carries the commit and fetch time; `journalctl -u wpa-project-sync` |
| Agent says a tool it was granted "is not available" | an agent-level `alsoAllow` **replaced** the global one | list the agent's own `tools.alsoAllow` and check the global names are repeated in it |

## Operational notes

- **An agent-level `tools.alsoAllow` REPLACES the global one; it does not merge.**
  Granting the GitHub tools to `code-invariants` therefore removed `read`, `write`,
  `edit` and `apply_patch` from it — including its ability to maintain its own
  `MEMORY.md` — and nothing said so. `config validate` passed, the room ceiling still
  listed those tools, and the only symptom was the agent reporting a shorter tool
  list than the config implies. An agent's own list must repeat the global names.
  This is the same class as the `--tools=create_issue` trap: a grant that appears to
  grant and does not.
- **Six layers can strip a tool**, and the tool-policy log names which one every
  time: `tools.profile`, global `tools.deny`, agent `alsoAllow`, the room's
  `tools.allow` ceiling, `tools.sandbox.tools.allow`, and a **built-in**
  `sandbox tools.deny` that is not in our config at all. That last one is why the
  agent has no `cron`: upstream denies scheduling to sandboxed agents, and clearing
  the list would mean emptying an unenumerable default to unmask one entry.
- **Subagents inherit the parent's own tools, but not its MCP tools.** A child gets
  the file tools (`read`, `write`, `edit`, `apply_patch`, plus `web_search` where the
  parent has it) and is stripped of `github__*` as non-inheritable and of
  `sessions_spawn`/`subagents` as a recursion guard. So a subagent can read and map
  the mirror; it cannot post to GitHub as you, which is the right split.

  An earlier pass concluded they inherit *nothing* and die with "No callable tools
  remain". That was true at the time and true for the wrong reason: the child's list
  is derived from the parent's **effective** allowlist, and the parent's was empty of
  file tools because of the `alsoAllow`-replaces-global fault above. Fixing that
  fixed subagents as a side effect. Worth remembering as a shape: a platform
  limitation inferred from a symptom that was really local misconfiguration.

  The inherited set follows the *session*, not the agent — a subagent spawned from a
  `--agent` CLI probe gets a narrower list than one spawned in the room or DM,
  because those sessions have different effective policy. Check in the session you
  actually use.
- **The MCP server is gateway-global, not agent-bound.** Only `code-invariants` is
  granted the tools, but that is tool *policy*, not an absent credential — the
  distinction ADR 0010 exists to make. If policy ever fails open the blast radius is
  issues and comments on one repo, which is why the PAT's scope matters more than it
  looks. Per-agent MCP servers are not supported on `2026.7.1-2`.
- **No registry rows were added** for these grants. `src/agent/registry.py` is
  checked against `[[agent.profiles]].tools`, and nothing lists them: enforcement is
  OpenClaw's `alsoAllow` plus the room ceiling. Adding rows nothing references would
  be dead entries in a list whose whole value is that it refuses unknown names. The
  two-config drift this leaves is the cost ADR 0011 already accepts.
