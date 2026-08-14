# Changelog

Notable changes to this project, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow
[SemVer](https://semver.org/) once there's something to version.

This project is pre-release, so `Unreleased` is where everything lives for now.

Findings verified on hardware are recorded here as well as in the ADRs, because
several of them are the kind of thing that costs an evening to rediscover.

## [Unreleased]

### Changed — the runtime switch itself (NVB-23, 2026-08-14)

ADR 0012 said what to run on. This is the executing of it, and the config now says
`xai/grok-4.3` on OpenClaw's built-in runtime with `agentRuntime.id: "openclaw"`
**pinned**. `auto` prefers a registered harness when one supports the provider, so the
pin is what stops a future CLI install from silently moving the runtime back out from
under the sandbox.

- **`agents.defaults.cliBackends` is deleted.** It existed to floor Claude Code's own
  `Bash`/`Read`/`Write`, which OpenClaw's `tools.*` policy did not reach. With no CLI
  backend there is no host subprocess to floor. The lesson that outlived it is kept in
  the config as a comment: tool policy binds at session creation.
- **`sandbox.workspaceAccess` is `"rw"`, which was a live bug at `"none"`.** `none`
  gives tools a throwaway directory under `~/.openclaw/sandboxes`, so `MEMORY.md` is
  written and silently lost; `ro` disables `write`/`edit`/`apply_patch` outright. Only
  `rw` delivers the durable memory the switch exists to restore.
- **`read`/`write`/`edit`/`apply_patch` are deliberately not denied.** They are the
  memory path, and what makes them safe is the sandbox rather than the policy list —
  they are OpenClaw's own tools, so they execute inside the container against a
  workspace bind-mounted at `/workspace`. If `sandbox.mode` ever returns to `"off"`,
  they must be denied again in the same commit. The two settings are one decision.
- **`web_search` stays stripped by `minimal` for now**, against ADR 0012's note that
  the xAI credential powers it for free. Adding a content-ingestion path in the same
  change as a runtime switch means a regression in either cannot be attributed to
  either. It belongs with NVB-18's injection smoke test.
- `sandbox.docker` hardening: `network: "none"`, `readOnlyRoot`, `capDrop: ["ALL"]`,
  `pidsLimit: 256`, `memory: "512m"`, `tmpfs: ["/tmp"]`.

### Verified on hardware, 2026-08-14 (NVB-23) — the switch works

The agent answers on `xai/grok-4.3` through OpenClaw's built-in runtime, inside a Docker
sandbox, with durable memory. Each acceptance criterion and what actually proved it:

| Criterion | Evidence |
|---|---|
| Runtime moved | probe turn returns `PROBE-OK`; gateway logs `agent model: xai/grok-4.3`, `NRestarts=0` |
| Sandbox is real | `runtime: sandboxed`, container `openclaw-sbx-agent-owner-*` running the built image |
| `workspaceAccess: "rw"` took | workspace bind-mounted `workspace-owner -> /workspace rw`, and **no** `~/.openclaw/sandboxes` scratch dir exists |
| Durable memory | agent wrote a file → appeared in the real host workspace → read back in a **fresh** session |
| Per-agent separation | with a narrowing on one agent: `owner` → `apply_patch, edit, read, session_status, write`; `family` → `read, session_status` |
| `exec` denied | probe answers `NO_SHELL_TOOL` |
| Nothing else broke | Waydroid RUNNING on the same IP; reader, signal-cli, gate all active |

**The group's tool ceiling had to move too, and it was the last thing hiding a broken
memory path.** `channels.signal.groups[].tools.allow` was `["session_status"]`, set when
the room's ceiling was deliberately minimal under the old runtime. Every agent-level
check looked right, and the family agent did have the file tools — but a message *in the
group*, which is the only way that agent is ever actually reached, resolved to one
read-only tool. So it had durable memory everywhere except in production. Now
`["session_status", "read", "write", "edit", "apply_patch"]`: everything needed to manage
its own state, nothing outward-facing, `exec` still refused (`NO_SHELL_TOOL`) in that
room. Verified by probing the real group session key rather than the CLI's default
session — the default session is a different resolution path and would not have caught
it.

**One subscription permits a device-code login per agent.** `owner` and `family` each hold
their own `xai/oauth` profile issued to the same account with independent expiries, so ADR
0012's fallback — letting `main` hold the model credential — was not needed. `main` stays
empty and the "main holds nothing" invariant stands as written rather than as a
compromise.

### Added, 2026-08-14 (NVB-26) — web search, with the sandbox still on

Both agents now have `web_search` on their own xAI OAuth, with no separate key, while
`sandbox.mode: "all"` and per-agent containers stay exactly as they were. Verified
functionally rather than by tool name — live results through the owner's DM *and* the
family group path — with `exec` still refused and memory still persisting.

**One non-obvious token makes the difference, and its absence fails silently:**

```json5
tools: {
  alsoAllow: ["read", "write", "edit", "apply_patch", "web_search"],
  web: { search: { enabled: true, provider: "grok" } },
  sandbox: { tools: { allow: [ /* … */ "group:plugins", "web_search"] } },
}
```

An allowlist built only from known core tool names resolves `includePluginTools` to
false, which drops the xAI plugin's provider registrations. A tool whose provider never
registered does not appear, is not logged as removed, and raises no error — the same
silent-no-op shape as a deny naming an unknown tool.

**`image_generate` and `video_generate` stay off**, for reliability rather than
isolation. Calling `image_generate` starts a detached media task whose completion turn
times out (`rawError=terminated`), tripping an auth-profile cooldown so that *later*
turns fail with "xai hasn't been responding". The provider error underneath is
`400 Could not decrypt the provided encrypted_content` from xAI's Responses API — a
conversation-continuation problem, not a setting. Having the tools listed is harmless;
calling them is not, and in a shared room that means one request for a picture leaves the
conversation unresponsive.

#### Unflattering: the finding this replaces was wrong, and wrong in an avoidable way

An earlier entry here — and a struck consequence in ADR 0012 — claimed the sandbox
*structurally* excluded these tools, and concluded that per-agent isolation and web search
were mutually exclusive. That conclusion would have justified containerizing the gateway
as a prerequisite and re-architecting the channel. It was wrong.

The evidence was a sandbox-on/sandbox-off comparison in which the sandbox-off run **also**
had no sandbox allowlist — so the plugin providers loaded there and nowhere else. Two
variables moved and one was reported as the cause. The tell was visible before the
conclusion was written: `session_status` is equally gateway-side and was available under
the sandbox the whole time.

The reusable lesson is not about `group:plugins`. It is that **a capability that is
missing is not evidence of what removed it** — on this stack, three independent layers
(`tools.profile`, `tools.sandbox.tools.*`, and provider registration) each remove tools,
and only the first two say so in the log.

Also found: **`alsoAllow` with no `allow` produces `["*", …]`** — `pickSandboxToolPolicy`
unions against a wildcard, so `tools.sandbox.tools.alsoAllow` opens that layer to
everything rather than adding to the default set. The shipped config uses an explicit
`allow` instead. Whether top-level `tools.alsoAllow` behaves the same way — which we
depend on for the file tools — is still open in NVB-26.

### Verified on hardware, 2026-08-14 (NVB-23) — the container runtime

The ADR assumed a sandbox backend existed. **There was none.** OpenClaw registers
exactly two — `docker` and `ssh` — and shells out to `docker` on PATH with no
binary-override key in the schema. Nothing on the Pi provided it, so `sandbox.mode:
"all"` would have had nothing to run in. Four findings, all of which cost time:

- **Docker's default startup is what this project spent a day avoiding.** It installs a
  `DOCKER-USER` chain and flips the `FORWARD` policy to DROP — the documented reason Q6
  leaned toward rootless Podman, because Waydroid's networking would have been
  collateral. `"iptables": false`, `"ip6tables": false` and `"bridge": "none"` in
  `/etc/docker/daemon.json`, **written before the first daemon start**, avoid it
  entirely. Checked before and after: `FORWARD` still ACCEPT, no `docker0`, Waydroid
  still RUNNING on the same IP. Sandbox containers run with no network anyway, which is
  OpenClaw's own default and correct here — the tool calls that reach the network are
  made by the gateway, above the sandbox.
- **Every registry pull failed with `connect: network is unreachable`, and the network
  was fine.** The Pi has no global IPv6 address, only link-local; dockerd's Go resolver
  prefers AAAA and does not fall back, while glibc orders IPv4 first — `curl -4` reached
  the registry throughout. A one-line drop-in setting `GODEBUG=netdns=cgo` fixes it.
  Worth recording because the symptom points squarely at the uplink and the uplink is
  not the problem.
- **The sandbox image is not shipped in the npm package and is not auto-built.**
  OpenClaw fails fast pointing at `scripts/sandbox-setup.sh`, which exists only in a
  source checkout. The inline Dockerfile in the shipped docs is the npm path; it needs
  `--network host` when there is no bridge. `python3` in that image is load-bearing —
  it backs the write/edit helpers, which is why OpenClaw refuses to substitute plain
  `debian:bookworm-slim`.
- **`tools.profile: "minimal"` strips the file tools, so ADR 0012's memory path did not
  work until `alsoAllow` put them back.** Not denying them was not enough. With
  `minimal` and no `alsoAllow`, the agent's entire tool surface was `session_status`:
  asked to write a file it neither wrote one nor errored, and asked to enumerate its
  tools it named only that one. `alsoAllow: ["read", "write", "edit", "apply_patch"]`
  restored it — verified by the file appearing in the real host workspace and being
  read back in a *fresh* session.

  **This hid behind our own stale measurement.** The note in the config that "minimal
  removes these 18 tools" was taken on 2026-08-12 under `claude-cli`, where the file
  tools were stripped from the loopback bridge by `NATIVE_TOOL_EXCLUDE` regardless — so
  their absence from that list said nothing about the profile, and reading it as though
  it did is what let the gap through. It is precisely the "tool names are
  runtime-dependent" trap this config warns about, sprung on us rather than by us. Any
  observation about tool surfaces is only valid for the runtime it was taken on.
- **`openclaw sandbox explain` does not show the effective tool surface, despite
  looking exactly like it does.** It reported `allow (default): exec, process, read,
  write, …` for both agents while the agent actually had `exec` denied (probe answered
  `NO_SHELL_TOOL`) and the file tools missing entirely. That block is the sandbox
  *routing* default — which tools would run in the container if present — not what the
  agent has. Both errors it invites are bad: believing `exec` is available when it is
  not, and believing `write` is available when it is not. Ask the agent, or check the
  filesystem; do not read the surface off `explain`. (This is a large part of NVB-24's
  justification.)
- **A `models.providers.<id>` entry overrides the plugin's provider registration, base
  URL included — and the failure is a lie.** The xai plugin defines
  `XAI_BASE_URL = "https://api.x.ai/v1"`, so `baseUrl` in config looks redundant.
  Writing the entry at all replaces the plugin's registration, and an entry carrying
  only `agentRuntime` replaces it with one that has no base URL. OpenClaw then POSTs
  **the xAI OAuth token to `https://api.openai.com/v1/responses`**, which answers
  `401 Your authentication token is not from a valid issuer` (`code=invalid_issuer`).
  That reads as xAI rejecting the login and sends you back to redo the OAuth flow —
  the one thing that was never broken. The tell is the URL in
  `[model-fetch] start provider=xai … url=…`; read it before touching credentials.
- **`plugins.allow` is a hard allowlist over every plugin, stock ones included** — not
  just external ones, which is what the Signal work left us assuming. Each provider is
  one of ~68 stock plugins, so xAI had to be trusted there before it could be enabled;
  `entries.xai.enabled: true` on its own does nothing, and `openclaw plugins enable xai`
  says so plainly ("blocked by allowlist"). **The error that actually surfaces does
  not.** `openclaw models auth login --provider xai` reports `No provider plugins
  found. Install one via openclaw plugins install` — for a plugin that is installed,
  stock, and listed. Following that advice installs something you already have. Worse,
  the gateway *auto-enables* the provider plugin for the configured model at runtime
  "without writing config", so a running gateway can be happy on a provider the CLI
  swears is missing. `plugins list` is the ground truth; the error text is not.
- **The `openclaw` uid is now in the `docker` group, which is root-equivalent on this
  host.** Recorded as a decision rather than a detail: it widens what a compromised
  *gateway* reaches, which ADR 0012 scopes out to NVB-22 on the grounds that injection
  into an *agent* is the likelier event. That reasoning is unchanged but its price is
  now higher, and NVB-22's trigger should be read with that in mind.

Also confirmed while checking that the Pi was healthy: **the reader's cursor and its
liveness file measure different things**, and the gap between them is not a fault. The
cursor advances on allowlisted rows only (46185, unchanged since 2026-08-13 08:53);
liveness records the DB's max `_id` (46444). The 259-row gap plus a `msgstore.db-wal`
written minutes earlier is the signature of a healthy reader and a silent allowlist —
which is the NVB-6 finding, now three days old and unlikely to change by waiting.

### Added

- **The runtime is the one the sandbox can reach**
  ([ADR 0012](docs/decisions/0012-the-runtime-is-the-one-the-sandbox-can-reach.md),
  superseding ADR 0011's agent runtime and the billing premise behind it). The
  `claude-cli` pin was made on billing grounds and turned out to foreclose the isolation
  NVB-14 exists for. Read out of the shipped `dist/` of `openclaw@2026.7.1-2` — the exact
  version on the Pi — rather than the prose docs.

  **The sandbox never reached the runtime we had pinned.** OpenClaw sandboxes *its own*
  tools; the gateway stays on the host, and under `claude-cli` the CLI is a host
  subprocess, so its native `Bash`/`Read`/`Write` ran beside the gateway as the gateway
  uid. `sandbox.mode: "all"` governed nothing that executed.

  **And the bridge will not substitute.** `NATIVE_TOOL_EXCLUDE` is exactly the six
  sandboxed names — `read`, `write`, `edit`, `apply_patch`, `exec`, `process` — because
  `claude-cli` declares `nativeToolMode: "always-on"`. One call site, passed
  unconditionally, no config flag. So the obvious cheaper fix — strip the CLI's natives
  and hand it OpenClaw's sandboxed ones — is not expressible.

  **Which made memory and isolation mutually exclusive.** Memory is plain Markdown
  written with an ordinary file-write tool. Floor the CLI's natives and nothing can write
  `MEMORY.md`; un-floor them and `Write` takes absolute paths as the shared uid, so every
  agent reads every other agent's store. `compaction.memoryFlush` does not rescue it: it
  prompts the *model* to write, and OpenClaw skips the whole compaction flow for backends
  declaring `ownsNativeCompaction`, which `claude-cli` does.

  **Decision: the built-in runtime, with xAI OAuth** — subscription billing, no registered
  harness, therefore sandboxed. Stated honestly in the ADR: the built-in runtime is *not*
  architecturally safer, it is the only place the configuration we want is reachable.

  **Two things verified along the way that outlive the decision.** Per-agent tool policy
  is real and is *existence* separation — one scoped list answers both `tools/list` and
  `tools/call`, so an agent is never told a capability exists that it cannot use, which is
  ADR 0010's property confirmed. And the tools that hold real credentials are **never**
  sandboxed on any runtime: NVB-17/18's calendar and mail tools make outbound HTTPS calls,
  and `resolveSandboxToolPolicyForAgent` is an allow/deny policy layer, not a routing
  table. That splits NVB-14 — switching runtimes bounds what a compromised *agent*
  reaches, containerizing bounds what a compromised *gateway* reaches — and the second is
  now NVB-22 with a written trigger.

  **A live config bug fell out of it.** `sandbox.workspaceAccess` is `"none"` in
  `config/openclaw.example.json5`, which would write memory into a throwaway directory
  under `~/.openclaw/sandboxes`. It must be `"rw"`; `"ro"` disables the write tools
  outright. Fixed in NVB-23, not here.

  **The billing premise underneath ADR 0011 is out of date.** It argued partly from
  February 2026 terms prohibiting subscription reuse by third-party apps. OpenClaw's docs
  cite Anthropic's 15 June 2026 support update stating the opposite. Not load-bearing for
  ADR 0012, which rests on sandbox reachability — but it must not be cited again without
  rechecking.

- **OpenClaw owns the channel, the gate owns the room**
  ([ADR 0011](docs/decisions/0011-openclaw-owns-the-channel-the-gate-owns-the-room.md),
  config in [`config/openclaw.example.json5`](config/openclaw.example.json5)). Most of
  what `src/gate/signal.py` was built to do already exists as configuration, including
  the two things we had marked as work — approvals and per-agent credential isolation.
  The gate keeps five responsibilities none of the frameworks model, and roughly 1,000
  lines come out. Hermes was evaluated and rejected on one project-specific fact: its
  Signal allowlist is keyed on phone numbers, and Signal does not share numbers by
  default, so a number-keyed allowlist matches nothing.

  **The schema is the source of truth and the prose docs are not.** Seven config keys
  derived from documentation were wrong and were corrected against
  `openclaw config schema`: `agents.list` vs `agents.entries`, a `transport` object
  that did not exist, `channels.signal.execApprovals`, the shape of `groups[].tools`,
  and the `peer.kind` grammar. A wrong key is accepted silently, which is why the
  example config is checked in with a `WHY` per line rather than generated by a wizard.

  **The Signal channel needs two settings, not one.** Both `plugins.allow: ["signal"]`
  and `plugins.entries.signal.enabled: true` are required; with only one the channel
  never connects and nothing says why.

- **Q4 is settled properly, not just answered (NVB-13).** ADR 0011 now states Managed
  Agents' case at full strength rather than in passing, because on the control this
  project cares most about it is *stronger* than what we chose: a vault credential is
  substituted at egress and cannot be read or exfiltrated by the sandbox even under
  prompt injection — the exact property we measured ours lacking the same day. It loses
  on something specific: that property does not survive a self-hosted sandbox, and
  WhatsApp data on the Pi means tool execution has to be self-hosted.

  Two additions make the decision reviewable rather than merely recorded. **Nothing
  reviews a new default tool before it reaches a credentialed agent** — an upgrade
  removed our tool floor and the agent gained a shell, announced as an unrecognized
  config key — so the answer is pinned versions, a probe gate before the channel starts,
  and the container that makes both non-critical. And **five named facts reopen the
  decision**, including the one that would invert it outright.

### Verified on hardware, 2026-08-12 (NVB-20)

- **Per-sender tool policy does not reach a CLI-backend agent, and fails open.**
  `channels.signal.groups[].toolsBySender` resolves on the dispatch path but not on the
  MCP loopback that serves the agent, so the deciding call sees no sender, falls
  through to `"*"`, and a narrowing rule silently grants. Established by instrumenting
  `matchToolsBySenderPolicy` — after a first conclusion ("Signal does not populate
  sender fields") that was wrong, and had been generalised from six failed key
  spellings without reading the matcher.

  Upstream already fixed it, in the beta channel only. Taking the beta was tried and
  rolled back: it removes `agents.defaults.cliBackends`, and without those flags the
  Claude CLI's native `Bash` is ungoverned by OpenClaw tool policy — the agent ran a
  shell twice, once with `tools.deny: [exec, process, terminal, code_execution]` and
  again with an explicit agent allowlist. **Everyone in a room therefore shares one
  tool set** until a stable release carries the fix *and* keeps a tool-flag surface
  (NVB-21). Reported upstream as openclaw/openclaw#122715 and #122716.

- **Shared group permissions, confirmed with two real senders.** The owner and a second
  family member asked the same question in the family group and got the same one-tool
  answer. The transcript also shows why `toolsBySender` looked plausible for so long:
  every message carries `senderId`, `senderName` and a correctly computed
  `senderIsOwner`, so **the identity reaches the prompt and never reaches the tool
  policy**. The second sender's turn resumed the first's session, which is ADR 0010
  rule 1 working as written.

- **Credential read-through is real, and the container is not optional.** A canary key
  written only to `main`'s store was retrieved and sent upstream by `family`, which has
  no store file at all — the provider returned 401, which is proof the key was found
  rather than missing. Worse, any file-reading tool defeats the boundary outright: the
  agent read arbitrary absolute paths as the gateway uid, and that uid owns *every*
  agent's auth store. Until `sandbox.mode` is real, "credentials are per agent" is a
  statement about tidiness (NVB-14, now blocking NVB-17 and NVB-18).

- **Three tool-policy mechanics, each of which cost a wrong assumption.**
  `--disallowedTools` does not override `--tools`; the allowlist wins, so a capability
  must be removed from the allowlist rather than added to the deny list. And **tool
  policy binds at session creation**: tightening it leaves existing sessions on the old
  capability set until their session store is cleared — a fresh agent had one tool
  while the resumed one still had `Read`.

- **There is no message that grants a capability, checked rather than assumed.** Asked
  over DM to grant itself `Bash` and enable `exec`, the config came back byte-identical
  and the turn ran normally rather than erroring. The tool that could have written it is
  stripped from every turn. The capability was absent, not declined — which is the half
  worth testing, since a model saying "I can't do that" proves nothing.

- **Typing indicators and read receipts cannot start a turn, by construction.** The
  Signal plugin reads `dataMessage`, `editMessage` and `syncMessage`, and contains zero
  references to `typingMessage` or `receiptMessage`. Settled from the bundle because
  signal-cli logs nothing per envelope, so an observed non-event could not distinguish
  "ignored correctly" from "never arrived".

- **Authority became a (conversation, sender) pair, and groups are forwardable
  (NVB-12).** The gate dropped every group message before this — a group has no
  single sender, so it had no principal. It now holds a table of conversations,
  each naming its permitted senders and the profile that applies to each of them
  *there*. The same person in a group and in their own chat are two principals with
  two profiles, and the emitted command says which applied.

  **One shape for both kinds.** There is no `direct`/`group` switch: a one-to-one is
  a conversation whose `id` is the other party's ACI and whose member set is exactly
  that person. Which kind it is, is derived by parsing the id — an ACI is a UUID, a
  group id is base64 — so config cannot contradict itself about what a room is, and
  `load_config` checks the one-to-one's duplicated halves agree. The cost is a few
  duplicated lines per DM; what it buys is one lookup table, one code path, and no
  class of bug where a group is handled by one-to-one logic.

- **Profiles are pre-bound grant bundles, and the container is the enforcement**
  ([ADR 0010](docs/decisions/0010-profiles-are-pre-bound-grant-bundles.md)). A
  profile lists tool instances from `src/agent/registry.py`, each already bound to
  one account and one verb, so granularity is *which instances are listed* rather
  than a scope language: `calendar.family.create_event` and `calendar.family.rw` are
  two grants over one calendar, and `calendar.personal.rw` is a different account.

  It is the GitHub fine-grained-PAT shape with one difference worth the ADR: a PAT is
  presented at request time and can be stolen or replayed, whereas a profile is
  compiled into the container. A tool outside the bundle is not refused at runtime —
  it is absent, and its credential was never mounted. The gate therefore emits the
  profile *name* and never the tool list, because a capability list travelling in a
  JSON line is a label the runner would have to trust.

- **A conversation names the agent that serves it; a sender may override it.** So a
  family group can run one shared agent everyone in the room activates, while the
  owner overrides it for their own session with a wider profile. Two rules, both
  checked at load, are what keeps that safe — and they **amend ADR 0007's "no two
  principals share an agent session"** rather than quietly contradicting it: an agent
  may not span two conversations, and senders sharing an agent must share its
  profile. Inside one room a shared session costs nothing already shared, since
  everyone reads everyone's messages; across rooms it would carry one room's text
  into another.

- **`--check`**, printing the resolved matrix — conversation, sender, agent, unit,
  profile, tools, credentials and who each may message. It is the pre-restart eyeball
  check, and the artefact to show a family member who asks what the thing can see.

- **Group membership is pinned, and drift refuses rather than degrades.** The gate
  reads the live member set back with `listGroups` over the socket it already holds,
  on connect and every 15 minutes, and immediately when an envelope looks like a
  group update. A group whose membership differs from the pinned list refuses
  everything, logs it, and — if `notify` names a conversation — says so in Signal
  once, because a refusing group is otherwise indistinguishable from a quiet one.
  It **fails closed**: every group refuses until the daemon has answered, so the
  window between connecting and knowing is shut rather than open.

### Changed

- **`config.toml` is not backward compatible.** `[[signal.principals]]` is gone,
  replaced by `[[signal.conversations]]` with `[[signal.conversations.senders]]`, and
  `[[agent.profiles]]` is new and required. `send_to` moved from the sender row to
  the profile, and its entries are now conversation labels rather than principal
  names — messaging a person is messaging a conversation, which is also what decides
  whether a send goes out as `recipient` or `groupId`. **The Pi's config must be
  migrated before this deploys**, or the gate refuses to start (which is the intended
  direction of failure, but it is a downtime).
- **Drop counters split.** `sender` still means a stranger at the door; the new
  `unlisted sender` means someone already inside an allowlisted room trying the
  handle, which is where probing in a family group will appear. `membership` is
  drift. They are kept apart because they want different reactions.
- The ack now goes to the **conversation** rather than to the sender, so in a group
  the message a confirmation quotes exists in the room where it will be typed.
- Pair and agent names are held to `[a-z0-9-]`. An agent name becomes a systemd unit
  instance and a directory under the outbox root, so this is a path-escape check, not
  a style rule.

### Verified on hardware

- **NVB-12 ran end to end on the Pi, 2026-08-12.** The owner sent one message in a
  Signal group and one in their own chat, and the same person arrived as two
  principals: `conversation=family principal=owner-family agent=family
  profile=owner-in-group` and `conversation=owner-1to1 principal=owner agent=owner
  profile=owner-full`. That is ADR 0008's central claim, observed rather than
  asserted. The ack landed in each conversation, and an outbox entry addressed to
  `self` from the group's agent went out as a `groupId` send.
- **No drift on connect**, with `members` pinned to what `deploy/pin-group.py`
  printed — the first real exercise of the member comparison against a live
  `listGroups`.
- **The documented `--check` command did not run.** `/opt/wpa` holds no installed
  package, so it needs `PYTHONPATH=/opt/wpa/src`, exactly as `wpa-gate.service`
  sets it. Found by running it rather than by reading it.

### Fixed

- **A pinned member list would have had to name each person twice.** The group
  fixtures shipped derived, and the capture the next day showed a `listGroups` member
  is `{"number": …, "uuid": …, "isAdmin": …}` — carrying *both* identifiers for
  anyone who shares their number, as the assistant's own row does. `_members_of` was
  adding both to the set, so a `members = [...]` written the obvious way never
  matched and the group would have refused forever. It takes the uuid now, falling
  back to the number, and refuses a member it cannot name at all.
- **`isMember: false` is drift.** The daemon still lists the members of a group the
  assistant has been removed from, so the old comparison would have found them equal
  and reported the room healthy.

### Unflattering

- **Two things shipped as guesses and one of them was wrong** (above). The group
  fixtures were derived because `listGroups` answered `[]` — the assistant was in no
  group — and the member shape was the guess that mattered. The other guess held: a
  group change really does arrive as `groupInfo.type: "UPDATE"`, with `message: null`
  and an incremented `revision`, so it is dropped as `no body` and the membership
  re-read has to be triggered on the drop path, which it was.
- **`revision` was tempting and is not used.** It increments on every group change,
  which looks like a cheaper drift signal than catching the update envelope — but
  membership is re-read on every connect anyway, so a revision cursor would only
  cover a change made while the gate was down. Written down because it will look like
  an oversight to the next reader.
- **One fixture is still derived: a message from an unlisted sender inside an
  allowlisted group.** That is the `unlisted sender` counter, where probing in a
  family room would appear, and there was no second person in the group to send it.

### Documented

- **Three directions of traffic**, added to
  [ADR 0009](docs/decisions/0009-agents-are-containers-that-ask-by-name.md). The
  question "could an off-the-shelf permission layer replace the gate?" kept
  recurring, and it is only answerable once the arrows are named apart:
  **into** the agent (invocation — Signal semantics, the gate's job), **out to
  people** (who it may address — also the gate), and **out to tools** (what it
  may do — the capability manifest and the container). Nothing off the shelf can
  stand in for arrows 1 and 2, because no agent framework knows what an ACI or a
  bodiless envelope is; the gate can never cover arrow 3, because it
  deliberately knows nothing about tools. They are complementary, and the
  question to ask of any arrow-3 candidate is *how does it know which principal
  is calling* — if several agents share one tool server, isolation enforced at
  the gate collapses quietly at the tool layer.
- **[Q4](docs/OPEN-QUESTIONS.md) is now a three-way decision and its own revisit
  trigger has fired.** Anthropic Managed Agents joins the custom Agent SDK build
  and OpenClaw: it ships per-tool permission policies with an allow/deny
  round-trip, vaults that keep credentials out of the sandbox by substituting at
  egress, per-session containers, and hard per-session spend budgets — and a
  self-hosted sandbox keeps tool execution on the Pi. Costs recorded too: no
  vault environment-variable credentials or memory stores when self-hosted, and
  **session history persisting on Anthropic's side**, which is a new fact for the
  threat model rather than a restatement of "the model sees the content".

  The trigger that fired is Q4's own: *revisit if the Signal and session plumbing
  dwarfs the agent logic.* M3 built exactly that plumbing. The lean is unchanged
  and the decision is deliberately deferred to its issue rather than settled in
  passing.

  Surveyed and not adopted: MCP gateways (MintMCP, Lunar.dev MCPX, TrueFoundry,
  agentgateway) gate MCP traffic, which the tool surface isn't yet, and each is
  another daemon on a loaded Pi. Plumbus has the right idea — `exposeAs` makes an
  unexposed capability *absent* rather than refused, which is this repo's
  invariant — but arrives with Node, pnpm, Fastify, Drizzle and PostgreSQL next
  to a stdlib-only Python codebase, at 1 star, to guard the control AGENTS.md
  calls load-bearing. The pattern is worth copying in about forty lines; the
  dependency is not.

### Decided, not yet built

- **[ADR 0008](docs/decisions/0008-authority-is-a-conversation-sender-pair.md) —
  authority is a (conversation, sender) pair.** The assistant should be usable in
  a family group and answerable in a dedicated confirmation conversation, neither
  of which the "one principal, one conversation" model can express. The same
  person in a group and in their own chat becomes two principals with two
  profiles, and **the group one is narrower**: a reply in a group is disclosed to
  everyone in it, so "the owner asked, use the owner's capabilities" would read
  the owner's calendar aloud to the family. Groups key on id, never name — names
  are chosen by members. Membership is pinned and drift refuses rather than
  degrades.

  It also settles how a `YES` is matched. Not newest-pending-wins: with two
  prompts outstanding that authorises the **wrong** action, silently. Confirmations
  are targeted, single-use, expiring, and answerable only by the pair they were
  sent to. Signal has no interactive messages, so the two mechanisms are quoted
  replies and reactions — **both captured on hardware 2026-08-11**, `quote.id` and
  `reaction.targetSentTimestamp` respectively, and a reaction arrives as a bodiless
  `dataMessage` that the gate drops as `no body` today.
- **[ADR 0009](docs/decisions/0009-agents-are-containers-that-ask-by-name.md) —
  the gate is the only process that touches Signal, and agents are containers.**
  The tempting design, letting the agent hold the socket, is wrong for a reason
  worth writing down: a JSON-RPC client of signal-cli does not get a send-only
  channel, it gets the receive stream too. The privileged process would see every
  envelope the gate refused. Instead the agent writes `{"to": "<name>", ...}` and
  the gate resolves the name through its own roster — so the agent handles no
  identifiers and can address exactly the people its profile lists.

  Isolation between principals stops being a promise: one container per principal,
  no access to Waydroid, the snapshot or the socket, WhatsApp context served by a
  broker per profile and defaulting to none. Agents never talk to each other
  directly; a request between them becomes a confirmation prompt to the owner.
  Measured on the Pi: `/dev/kvm` present, 6398MB available with everything running,
  so microVMs are the documented upgrade rather than a fantasy — with a written
  trigger for taking it.

  Recorded because it is the likeliest way this breaks: **Waydroid runs its own
  bridge and NAT**, so a container runtime that rewrites iptables could take the
  WhatsApp side down. "Waydroid still RUNNING, messages still arriving" is an
  acceptance criterion for that work, not an assumption.

### Added

- **An outbound path where the agent asks by name and the gate decides**
  (`src/gate/signal.py`). The gate could read; now it can speak, and it is still
  the only process that touches Signal ([ADR 0009](docs/decisions/0009-agents-are-containers-that-ask-by-name.md)).
  An agent writes `{"to": "mom", "text": "..."}` into its own outbox directory;
  the gate resolves the **name** through its own roster — the same
  `[[signal.principals]]` rows the inbound allowlist is built from — checks it
  against that principal's `send_to`, and sends.

  The indirection is the point. The agent handles no identifier, so it cannot
  forge or exfiltrate one, and the addressable set is exactly the roster rather
  than "whatever the agent asked for, validated". Handing the agent the socket
  instead was never an option: a JSON-RPC client of signal-cli receives the
  inbound stream as well, so it would see every envelope the gate refused.

  - **`send_to` defaults to `["self"]`** — an agent granted nothing can still
    answer its own principal and reach no one else. Names in a send list must be
    principals; one that isn't refuses to start, as does a duplicate principal
    name, which would leave `to: "mom"` with two answers.
  - **A spool directory per principal**, `/var/lib/wpa-gate/outbox/<principal>/`,
    not one shared file. It makes *who wrote this* a filesystem fact rather than
    a field the agent controls. 0700 and gate-owned today; NVB-14/15 gives each
    principal its own group at 0730 — write and execute so an agent can create
    and rename, no read so it cannot enumerate. **One group per principal, never
    one shared**, or agent A drops an entry in B's directory and sends under B's
    send list.
  - **Delivery is at-most-once: the entry is unlinked before the send.** A crash
    between the two loses a reply; the other order sends a person the same
    message twice, and only the gap is visible to whoever asked.
  - **The receive loop no longer blocks.** `for line in conn.makefile("r")`
    cannot also poll a directory, so it is now `select` on a 250ms timeout with a
    newline buffer, draining by the clock so a busy stream cannot starve the
    outbox. Single-threaded: two threads sharing one socket, in the process whose
    job is to be trustworthy, is a bad trade for a quarter second.
  - **The `send` response is captured, not discarded.** Its `timestamp` is the id
    a quoted reply carries back as `quote.id`, so without recording it a later
    `YES` cannot be matched to the prompt it answers. Acks are registered the same
    way — the message a person quotes when they confirm is usually the ack.
    Appended to `sent.jsonl`, capped, and replaced by NVB-16's expiring registry.

    Both response shapes were **captured off the daemon before the parser was
    written** (2026-08-11), and the failure one is not what it looks like:

    ```
    {"result": {"timestamp": 1786473936544, "results": [{"type": "SUCCESS", …}]}}
    {"error": {"code": -1, "message": "Failed to send message",
               "data": {"response": {"results": [{"type": "UNREGISTERED_FAILURE", …}],
                                     "timestamp": 1786473960560}}}}
    ```

    A failure **also carries a timestamp**, nested under `error.data.response` —
    record that one and a confirmation gets keyed to a prompt that never arrived.
    So the test is the presence of a *top-level* `result`, not of a timestamp. And
    the reason worth logging is the per-recipient `type`; `code` is `-1` for every
    failure there is.
  - **Refusals are counted and logged with no identifier and not the requested
    name either** (`refused send: not in list (n total)`) — that name is free text
    an agent chose, on its way to journald (threat model R4). Reasons:
    `not in list`, `malformed`, `too long`, `too many`, `send failed: <code>`.
  - **Hostile entries are refused rather than obeyed:** `O_NOFOLLOW`, so a symlink
    into `/var/lib/wpa-signal` is not a read primitive; `O_NONBLOCK` plus a
    regular-file check, because opening a fifo read-only blocks forever and one
    `mkfifo` would otherwise stop the whole control channel, inbound included;
    64KB per entry; 32 pending per directory, **refusing the newest and keeping
    the oldest**, since past that the likely explanation is a loop or an injection
    rather than a person's traffic.

  Verified end to end on hardware 2026-08-11: an entry naming an unlisted
  recipient produced `refused send: not in list (1 total)` and was consumed with
  nothing on the wire; an entry naming `self` was delivered to the phone and its
  timestamp written to `sent.jsonl`; an entry placed while the gate was **stopped**
  went out when it started; and a quoted reply to that message arrived carrying
  `reply_to = 1786474114086`, **the exact timestamp recorded for it** — the link
  NVB-16 needs, demonstrated rather than assumed. Waydroid stayed RUNNING and the
  reader's `max_id` kept climbing throughout.

  One finding from that run, which matters to NVB-16 and to nothing else yet:
  **an inbound envelope's timestamp and an outbound send's timestamp come from
  different clocks.** The ack for the quoted reply was recorded at
  `1786474460760` against a command whose envelope said `1786474461529` — the ack
  is stamped by Signal, the envelope by the sender's phone, and here the "reply"
  precedes the message it answers by 769ms. The ids are handles, not an ordering;
  a registry that sorts or expires by comparing across the two will be wrong by
  however far the phone's clock has drifted.

  Not built: a per-profile token bucket. The bounds above stop a burst, not a
  steady loop — write one entry, watch it go, repeat — but nothing can write to an
  outbox until NVB-14/15 grants an agent the directory, so there is no writer to
  loop yet. Marked in the code and deferred to that issue. Also out of scope:
  groups and (conversation, sender) pairs (NVB-12 — `self` is the 1:1 today and
  the line that must change is commented), any feedback channel back to the agent,
  sending to non-principals, retries, and an outbox disk quota.

- **The Signal account state is backed up encrypted, off the Pi, on a timer, and
  the restore has actually been run** (`deploy/backup-signal.sh`,
  `deploy/systemd/wpa-signal-backup.{service,timer}`, runbook 03 §3). NVB-9.

  `/var/lib/wpa-signal/` **is** the account, and the two ways to lose it don't
  overlap. Lose the directory: re-registration, which costs another captcha,
  another SMS, and a new identity key that every contact sees as a safety-number
  change. Leak it: someone else can *be* the assistant — read the control channel
  and send commands into it. Before this, the only copy was a **plaintext** tarball
  in a home directory on the WSL box, taken before `setPin` and from the old
  `~/.local/share/signal-cli` path, so it was simultaneously unencrypted, stale,
  and pointed at a location a restore could no longer land on.

  **The Pi cannot read its own backups.** `age` with a single recipient; the
  private key was generated on the WSL box and lives in the password manager, not
  on the Pi. This is the part that is easy to get wrong by encrypting with a key
  stored beside the ciphertext, which protects against disk loss and nothing else.
  Here a Pi compromise gets the live account — it was always going to — but not
  the archive of every previous generation of it.

  Destination is a **Backblaze B2 bucket**: off the Pi and off the WSL box, which
  is the actual requirement. The WSL box is not a backup target, it is a second
  thing that can die. Credentials go in `/etc/wpa-signal-backup.env` (root, 0600)
  as `RCLONE_CONFIG_WPABACKUP_*` variables, so rclone needs no second config file
  and no interactive `rclone config` to reproduce — same one-root-file pattern as
  `/etc/wpa-signal.env`.

  **The unit stops `signal-cli` for the copy.** The daemon holds the account lock
  and rewrites session state as messages arrive, so a live copy is not guaranteed
  coherent. Measured cost on hardware: the whole unit runs in ~1s and the socket
  is back well inside the ~19s signal-cli needs to reach a listening state, once a
  week, at 04:00 Sunday. Blobs are dated and never overwritten, so a corrupted
  backup cannot land on top of the last good one — which is a real way to lose an
  account, and the reason retention is by filename rather than by rotation.

  **The restore drill was run, not assumed**, 2026-08-11 — and run from the B2
  copy rather than a Pi-local file, so the download half was exercised too. Blob
  fetched to the WSL box, decrypted with the key the Pi has never held, unpacked to
  a scratch directory, and `signal-cli --config <scratch>/wpa-signal listAccounts`
  reported `Number: +972552645702`. Nothing was sent or received from the restored
  copy and no second daemon was started — Signal allows the account **one** primary
  device, so a restored copy running while the Pi still runs is not a hot spare, it
  is two clients claiming one identity. A restore is for when the Pi is gone.

  Three things found doing it, none of them in any documentation:

  - **`GODEBUG=netdns=cgo` is load-bearing.** rclone is Go, and Go's built-in
    resolver cannot resolve `api.backblazeb2.com` through this network's router:
    `no such host` on 3/3 attempts while `getent hosts` resolved the same name 5/5.
    cgo worked immediately. The failure surfaces as what looks like a Backblaze
    outage, weekly, on a unit nobody watches — the worst shape a bug can have here.
  - **`ReadWritePaths=/var/backups/wpa-signal` fails the unit with 226/NAMESPACE**
    on a fresh install. systemd builds the mount namespace before `ExecStart`, so
    it cannot name a directory the script has not created yet. The unit points at
    `/var/backups` and the script makes the subdir. Same trap as the
    `StateDirectory=` note in `wpa-reader.service`, approached from the other side.
  - **The restore machine needs a JRE 25 signal-cli of its own.** The WSL box had
    Java 8, which cannot run signal-cli at all. A backup you hold the key to but
    cannot open is not a restore path, so runbook 03 §3d now lists what the restore
    machine needs — including that the B2 key belongs in the password manager next
    to the age key, because the age key alone opens a blob you cannot download.
    The x86_64 build needs no libsignal surgery; §1a is an arm64 problem only.

  The registration PIN is not in the backup and not derivable from it. Password
  manager, next to the age key.

- **A trigger gate decides which Signal messages are commands** (`src/gate/signal.py`,
  `deploy/systemd/wpa-gate.service`). Until now the channel carried traffic in
  both directions and nothing said which of it counted; this is the half of M3
  that was missing. Deterministic host code, stdlib only, no model in it, and the
  only thing that ever forwards a message onward — so a capability to be invoked
  by anyone else does not exist rather than being filtered downstream.

  Three refusals, each from [ADR 0004](docs/decisions/0004-signal-control-channel.md)
  and runbook 03 §5:

  1. **The sender must be a configured principal, one-to-one.** Unlisted sender or
     a group: dropped and counted.
  2. **A trigger is a `dataMessage` with a non-empty body.** Typing indicators and
     read receipts arrive as `receive` notifications with no `dataMessage` at all,
     so a gate keyed on `method == "receive"` is invocable by anyone who can make
     the assistant's phone show "typing…" — and the sender check does not save you
     on an envelope carrying no command. The daemon cannot filter these;
     `--ignore-*` covers attachments, stories, avatars and stickers only.
  3. **A reply keeps what it replied to.** Accepted commands carry `reply_to`, the
     id of the quoted message, so a `YES` can be matched to the pending action it
     authorises instead of being read as a global "proceed". The registry itself
     is M4's; not losing the link is the gate's.

  Verified end to end on hardware 2026-08-11, from a phone: typing indicator →
  `dropped: no body (1 total)`; the message itself → `accepted principal=owner
  profile=owner len=10 reply_to=None`, one line in `commands.jsonl`, and the ack
  arriving on the phone; the phone's delivery and read receipts for that ack →
  three more `dropped: no body`. The journal carries the decisions and the
  counters and no message text.

  The message, typing and receipt fixtures are **real envelopes captured off the
  socket** and redacted; the other four are derived from the captured message by
  editing one field, since those actions were not driven on the phone.
  `tests/fixtures/signal/README.md` says which is which, and carries the capture
  procedure. It matters that the two load-bearing ones are real: both facts they
  encode — a typing indicator arriving as a bare `receive`, and `sourceNumber`
  being null — are absent from the documentation and were found by watching the
  wire.

  Accepted commands append to `/var/lib/wpa-gate/commands.jsonl` (0600) and the
  sender gets `ack <timestamp>` back, which is the handle a later confirmation
  quotes. journald gets decisions and running per-reason drop counters and
  **never bodies, never numbers** (threat model R4) — the drop counter is what
  will show someone probing the number.

  **Survives a reboot**, verified 2026-08-11: boot at 16:09:22, `wpa-gate` started
  16:09:38, the socket appeared ~26s after that, gate connected and a message sent
  from the phone was accepted with the ack arriving. Waydroid came back
  `Container: RUNNING` — not FROZEN — and no unit failed. The connect backoff caps
  at **10s rather than 30s** because that ceiling is the worst-case delay between
  the socket existing and the assistant answering: on this reboot the first
  connect landed ~25s after the socket appeared, all of it spent asleep.

  It reconnects. `active` is not `ready`: `Type=simple` marks signal-cli started
  when the JVM launches and the socket appeared ~19s later on a cold boot, and the
  daemon's own `Restart=on-failure` takes the socket away under a live gate. So
  connecting is a backoff loop rather than one attempt, with a test that a gate
  surviving one EOF still processes the next connection. Verified on hardware
  2026-08-11: `systemctl restart signal-cli` under a live gate logged
  `reconnecting` → `Connection refused` → `connected` 8s later, same PID, systemd
  `NRestarts=0` — the gate rode it out rather than dying and being restarted.

- **[ADR 0007](docs/decisions/0007-principals-on-the-control-channel.md) — the
  control channel carries principals, not one owner.** Family should be able to
  use the assistant; doing that by loosening the sender check would be the wrong
  half of the system to loosen, so the invariant is widened deliberately instead:
  *a closed set of known one-to-one conversations, each with a named principal and
  a profile, everything else dropped before dispatch*.

  What lands now is only the shape — the gate says **who** sent a command and
  **under which profile**, and acks into that person's conversation. What a
  profile may do is M4's. The ADR records the two things that must not be traded
  away when it gets there: **no two principals share an agent session** (ADR 0006's
  argument one level up — a shared context puts one person's text next to
  another's credentials), and **a profile holds nothing its principal does not
  already own**, which is what bounds an injection arriving through someone else's
  conversation. Confirmations route to the owner today, per-principal (`self`)
  once profiles carry only their own principal's credentials.

  Also the honest part: the Signal side stops being uniformly trusted. A family
  member forwarding a scam text is attacker-influenced content on the privileged
  wire, which is exactly what ADR 0006 keeps off it from the WhatsApp direction.

### Changed

- **The daemon runs `--receive-mode on-connection`, and it is the difference
  between losing commands and not.** With `on-start` signal-cli pulls from Signal
  whether or not any client is attached, so a message arriving while the gate is
  restarting is acked to the server, dropped for lack of a subscriber, and gone.
  Established deliberately on hardware 2026-08-11 rather than assumed: gate
  stopped, every client detached, one message sent, gate started — it never
  arrived, and it never arrived later either. No error anywhere; the assistant
  simply doesn't answer, which is the failure you least want to meet in
  production.

  `on-connection` makes the daemon fetch only while a client is attached, so
  undelivered messages stay queued on Signal's servers. Same experiment, same
  conditions, after the change: the message landed **one second** after the gate
  reconnected and was accepted normally. A gate restart is now a delay rather
  than a hole.

  The cost is that nothing is received while no client is connected — including
  receipts and typing indicators, which is no loss — and that the daemon is only
  as live as its subscriber. `Restart=always` on `wpa-gate` covers that.
- **`signal-cli.service` runs with `UMask=0007`** so the JSON-RPC socket is
  created `srwxrwx---` and `wpa-gate` can reach it through the `wpa-signal` group.
  The gate deliberately does not run *as* `wpa-signal`: `/var/lib/wpa-signal` is
  the account itself, and the process parsing messages from strangers has no
  business being able to read it. Group membership buys the socket and nothing
  else — the state directory is 0700, and a directory with no group execute bit
  cannot be traversed however its contents are moded.

  **0027 was the obvious value and it is wrong:** `connect(2)` on a unix socket
  needs *write* permission, so a group-readable `srwxr-x---` socket fails with
  EACCES. Verified on hardware 2026-08-11, and the failure is nastier than it
  sounds — it is indistinguishable from "the socket isn't there yet", which is a
  state this gate is built to wait through. It sat in a retry loop looking
  perfectly healthy. The gate now logs every tenth retry rather than only the
  first, so a permanent failure eventually says so instead of going quiet.
- **`config.toml` gained `[[signal.principals]]` and lost `[signal] owner`.** Each
  entry is a uuid and/or a number, a name, and a profile. An empty list is a
  startup refusal rather than a permissive default, the same posture as the chat
  allowlist. The `socket` default was also wrong — it still said
  `/run/user/1000/signal-cli/socket`, which no longer exists.

  **The allowlist keys on the ACI UUID, not the phone number.** Current Signal
  does not share phone numbers by default: real envelopes arrive with
  `sourceNumber: null`, `source` and `sourceUuid` both set to the sender's ACI.
  A number-keyed allowlist matches *nothing* — verified on hardware 2026-08-11,
  where the first two live messages were correctly dropped as `sender` and it
  took reading the captured envelope to see why. The number stays supported as a
  second key because it is the part a human can check by eye. This is the same
  shape of problem as WhatsApp's LIDs in NVB-7: the identifier a person knows is
  not the identifier the wire carries.

  `sourceName` is never consulted. It is a display name its owner chooses, so
  matching on it would be an allowlist anyone can enter by renaming themselves —
  the same reason the chat allowlist keys on JIDs and not group subjects.

  Two related traps found while wiring it up: the Pi's live `config.toml` still
  carried the example's placeholder number (`+447700900000`), so the allowlist
  had a row for somebody who was not the owner. And the ack now goes back to the
  identifier the message *arrived from* rather than one copied out of config —
  with a placeholder in the file, "reply to the owner" would have meant replying
  to a stranger.
- **[ADR 0004](docs/decisions/0004-signal-control-channel.md) is explicit about
  what the dedicated Signal number does and does not buy.** It does *not*
  prevent unauthorized invocation — anyone who learns the number can message the
  assistant, and the ADR previously implied more from the number than it
  delivers. The control that refuses those messages is the sender allowlist, and
  it is the same check on a linked device.

  Added as an invariant: **the agent reads and writes exactly one Signal
  conversation**, with anything from another sender, group, or new thread
  dropped before dispatch. This holds regardless of which account the assistant
  runs on. What the separate account buys is blast radius — key material, and
  how much attacker-controlled text passes through the privileged process — and
  the ADR now says only that.

### Added

- **The Signal control channel is live** (`deploy/systemd/signal-cli.service`,
  `deploy/install-signal.sh`) — signal-cli 0.14.7 as a JSON-RPC daemon on a unix
  socket, running as its own `wpa-signal` user, heap capped at 256MB. The
  assistant has its own Signal account on a dedicated eSIM line per
  [ADR 0004](docs/decisions/0004-signal-control-channel.md), verified in both
  directions. Q3 is answered: ₪19.80/month, bought 2026-08-10.

  The number lives in `/etc/wpa-signal.env`, not in the unit — a phone number is
  not a secret, but publishing a working one in a public repo invites traffic at
  exactly the endpoint that triggers privileged actions. The account state
  directory *is* the account, so it runs under its own user rather than out of a
  human's home directory.
- **A staleness check, because "services are running" is not "messages are
  arriving"** (`src/reader/staleness.py`, `wpa-staleness.{service,timer}`). It
  polls `max(_id)` from the snapshot hourly and fails — non-zero exit, a `<3>`
  line under `journalctl -p err`, the unit in `systemctl --failed` — when the id
  has not moved in 6h. M3 can hang `OnFailure=` off it to push the alert to
  Signal without changing anything else.

  It checks the one fact nothing else did. During the 2026-08-10 freeze every
  other signal looked fine: both waydroid units active, `com.whatsapp` running,
  the timer polling every 30s and correctly reporting no new messages. So the
  check deliberately does *not* ask `waydroid status` — container state was the
  misleading signal, and a frozen container is caught anyway because `max(_id)`
  stalls. Nor does it use the reader's cursor, which only advances for
  allowlisted chats and would report a quiet group as a dead system every night.

  State is one file, `/var/lib/wpa-reader/liveness`: its contents are the last
  id, its mtime is when that id last changed. That only works because the write
  is conditional on the value moving — rewriting it every run would keep the
  mtime fresh forever and the alert could never fire. There is a test for it.

  **The 6h threshold is a guess and the argument in the unit file is the knob.**
  Nobody has measured what a genuinely quiet night looks like on this account;
  the soak is what will say. Erring long is deliberate — the freeze persists
  until a human intervenes, so a late alert costs little and a nightly false one
  trains everybody to ignore the real thing.

  Two things it does not cover. `vcgencmd get_throttled` is not collected: it
  needs the `video` group, and the check keeps ADR 0006's confinement contract
  (`PrivateNetwork`, `ProtectSystem=strict`, `User=wpa-reader`) rather than
  taking privilege for one number — run it by hand during the soak. And an
  unreadable snapshot returns "unknown", not "stale": wpa-snapshot rewrites
  those files every 30s so an hourly reader will eventually catch one mid-copy.
  The cost of that choice is that a *permanently* unreadable snapshot alerts
  nothing here — it shows up as wpa-reader failing every 30s instead.

  Verified on hardware 2026-08-11: status line
  `max_id=45280 quiet=0.0h jsonl_bytes=1084259 mem_available_kb=6688324`, and a
  backdated marker produced the alert and the failed unit as intended. 6.4GB
  available with the container unfrozen, so signal-cli's JVM has room. **The 24h
  soak itself is still outstanding** — one reboot verified the suspend fix, and
  one reboot is not evidence the freeze cannot return another way.
- **The reader runs unattended** (`deploy/systemd/`) — a `.service` + `.timer`
  pair polling every 30s, plus a separate root-run `wpa-snapshot.service` that
  copies the databases to `/dev/shm/wpa-snapshot` and hands them to
  `wpa-reader`. The privileged half is four `install` calls in a shell script;
  the process that reads untrusted text has no privilege at all. Installed with
  `deploy/install-reader.sh`, code deployed to `/opt/wpa` rather than a home
  directory because the reader runs with `ProtectHome=yes` and cannot see
  `/home`.
- **ADR 0006 confinement is now enforced by the OS**, not just documented:
  `PrivateNetwork=yes`, `ProtectSystem=strict`, `ProtectHome=yes`,
  `PrivateTmp=yes`, `NoNewPrivileges=yes`, and no credential environment
  variables. Verified on hardware — see below.
- **Chat allowlist wired up** — `[whatsapp] chats` in `config.toml` is read via
  `tomllib` and applied. **Shipped default is allowlist-only**: the reader
  refuses to start without a config rather than falling back to reading every
  chat, and a config with an empty `chats` reads nothing. It looks broken until
  you fill it in, which is the intent.
- **Message output goes to `/var/lib/wpa-reader/messages.jsonl`**, never to
  journald — other people's messages in the system log would violate
  threat-model R4. Grepped journald for 150 distinct strings taken from real
  message bodies and chat names: zero hits.

### Changed

- **Sender names resolve through `msgstore.jid_map`, taking coverage from 5.3%
  to 49.4% of received messages** (100% in the one allowlisted group, up from
  23.9%). Measured on the live snapshot, 31,237 received messages. Group
  participants are identified by LID, and `@lid` strings do not join to
  `wa_contacts` — `jid_map` is the missing `lid_row_id → jid_row_id` hop, after
  which the phone JID joins to the address book as it always did. Nothing was
  missing from the schema; the join was.
- **1:1 messages get a sender name too.** In a 1:1 chat `sender_jid_row_id` is
  `0` and points at no `jid` row, so those messages resolved to `?` — 4,212 of
  them, 13% of the corpus. Falling back to the chat's own JID runs them through
  the same name lookup.
- **The snapshot now copies `wa.db-wal` and `wa.db-shm`** (`deploy/snapshot.sh`
  and the Python fallback in `snapshot()`). `msgstore.db` had its WAL from the
  start; `wa.db` did not, so contact and LID-name edits sitting in its WAL were
  invisible — the same ADR 0003 staleness bug, one database over. The Python
  fallback also clears the destination before copying, which it never did: a
  leftover `-wal` applied to a newer `.db` returns wrong rows silently.
- **The chat allowlist keys on the chat JID, not the group subject.** Subjects
  are attacker-settable — anyone in a group can rename it — so a name-keyed
  allowlist can be talked into. `read_since(chats=…)` now takes JIDs.
- **The allowlist filters in SQL rather than in Python.** Filtering the returned
  rows was a stall: the caller advances the cursor to the last message it
  received, so a batch containing no allowlisted messages returned nothing, the
  cursor never moved, and the reader re-read the same 500 rows forever. There is
  a test for it.
- Cursor moved to `/var/lib/wpa-reader/cursor`; `deploy/bootstrap.sh` no longer
  creates `/var/lib/wpa`.
- **Dev tooling is [uv](https://docs.astral.sh/uv/)** — `uv run mypy && uv run
  pytest` locally, `uv run --locked` in CI, `uv.lock` committed. The point isn't
  speed: `.python-version` pins 3.11 and uv installs it, so local runs match the
  Pi. They didn't before — the dev venv was 3.12 while CI and the Pi were 3.11,
  which is exactly the gap the CI comment warned about.

### Verified on hardware — 2026-08-11 (signal-cli on arm64)

- **signal-cli logs every received message body to journald by default.** In
  daemon mode it prints envelopes, bodies included, to stdout — and under systemd
  stdout is the journal, so the first test message landed in the system log in
  plaintext. Fixed with `--no-receive-stdout` (messages still reach JSON-RPC
  clients, they just stop being printed) plus `--scrub-log`, which redacts
  identifiers; the account number now appears as `+**********02`.

  Worth stating plainly, because the reader already solved this from the other
  end: its output goes to a file precisely to keep content out of journald, and a
  chatty control channel undoes that. In M4 this channel carries the confirmation
  prompts, which by design describe the action about to be taken with real
  credentials.
- **Two arm64 blockers, both of which fail late.** signal-cli 0.14.7 requires
  **JRE 25**; Bookworm ships openjdk-17 and has no backports configured, so the
  JRE comes from Adoptium. And `libsignal-client-0.99.1.jar` bundles a macOS
  arm64 `.dylib` and a Linux x86_64 `.so` but **no `libsignal_jni_aarch64.so`** —
  the GraalVM native build is x86_64 only. `--version` and `listAccounts` work
  without it, so the failure surfaces at `register`, after a captcha has been
  spent. Solved with the matching prebuilt from `exquo/signal-libs-build` added
  to the jar. **The libsignal version must match the jar exactly, so every
  signal-cli upgrade means redoing it.**
- The runbook's draft unit could not have started: `User=%i` in a non-template
  unit, and `ExecStart` referencing `${ASSISTANT_NUMBER}` with nothing defining
  it. Replaced with a real one in the repo.
- The launcher honours `JAVA_OPTS` and `SIGNAL_CLI_OPTS`, not
  `JAVA_TOOL_OPTIONS` — the heap cap was set on the wrong variable in the draft.
- **Survives a reboot, tested the way that means something**: not "the unit is
  active", but a message sent from the phone *after* the reboot arriving
  unattended, and a reply going back out. Zero `Body:` lines in the journal for
  that boot, so the logging fix holds across a restart. Waydroid came back
  `RUNNING` again, and 6.2GB was available with the JVM and an unfrozen
  container together.
- **`active` is not `ready`.** `Type=simple` marks the unit started when the JVM
  launches; the socket appeared ~19s later on a cold boot, and a first check at
  45s uptime found `/run/wpa-signal/` empty. Anything connecting at boot has to
  retry rather than assume.
- **The receive stream carries typing indicators and read receipts, not just
  messages** — a "someone is typing" arrives as a `receive` notification with no
  `dataMessage` at all. This is a requirement on the M4 agent, not a bug here: an
  agent that triggers on any `receive` envelope is invocable by a typing
  indicator, which anyone who knows the number can produce at will, and checking
  the sender does not help when the envelope carries no command. The daemon
  cannot filter them (`--ignore-*` covers attachments, stories, avatars and
  stickers only), so the consumer must require a non-empty
  `envelope.dataMessage.message`. **No fix is implemented — there is no consumer
  yet**; it is written into runbook 03's trust-boundary rules where the agent
  will be built.

### Verified on hardware — 2026-08-10 (reader on a timer)

- Backfilled 5,082 messages from the allowlisted group in 11 batches at ~0.3s
  per run, then sat idle at head. Survives a reboot: the timer is active and the
  snapshot is rebuilt on tmpfs within ~90s of boot. A message sent to a
  monitored chat was picked up by the timer and written as JSON with nobody
  touching anything.
- **Waydroid freezes the container when no app is displayed, which is always
  true on a headless Pi — and WhatsApp then receives nothing.** Cost 1h40m of
  silence after a reboot before it was noticed, because everything looks
  healthy: both services active, `com.whatsapp` in the process list, the reader
  polling happily. The only symptom is that the newest `_id` in `msgstore.db`
  stops moving. `waydroid status` reports `Container: FROZEN` and
  `IP address: UNKNOWN`.

  Fixed with `waydroid prop set persist.waydroid.suspend false` plus a session
  restart; verified across a reboot. Note `suspend_action = none` in
  `waydroid.cfg` was already set and did **not** prevent it, so the earlier
  "autostart survives an unattended reboot" finding was true about the units and
  wrong about the thing that matters. **"Services are running" is not
  "messages are arriving", and only the second one is worth checking.**
- **`sudo -u wpa-reader curl https://example.com` succeeds, and that is not a
  bug** — `PrivateNetwork=` is a property of the unit's namespace, not of the
  user, so the acceptance test as originally written would have passed while
  proving nothing. Tested inside the sandbox instead: DNS fails, a raw IP fails
  (`curl` exit 7), and `ip addr` shows loopback only.
- **`StateDirectory=` cannot host a `StandardOutput=append:` target.** systemd
  opens stdout before creating the state directory, so a fresh install fails
  with `209/STDOUT`. The installer creates `/var/lib/wpa-reader` instead.
- **`rsync --delete` on deploy silently wiped the live `config.toml`**, and the
  installer then wrote a fresh one with an empty allowlist — a reader that runs,
  succeeds, and reads nothing. Now excluded.
- **systemd's start limit is a silent-death trap here.** Five failures in 10s
  latch a unit into `start-limit-hit` and it stops being retried, which looks
  exactly like "nobody messaged me today". `StartLimitIntervalSec=0` on both
  units; a failed poll is recoverable because the cursor doesn't move.
- A stale `-wal` from a previous poll would be applied to a newer `.db`, so the
  snapshot clears the old parts before copying — including `wa.db-wal`/`-shm`,
  which the reader's own read-only open leaves behind.

- **Reader** (`src/reader/msgstore.py`) — turns new WhatsApp messages into
  fixed-schema JSON lines (`{id, chat, sender, timestamp, excerpt}`). The
  unprivileged half of [ADR 0006](docs/decisions/0006-two-process-privilege-split.md):
  no tools, no network, no credentials. One file, no dependencies.
  Verified against 42,000 real messages — drains in 72 batches and is idempotent
  at head. ([#1](https://github.com/NavehBrenner/whatsapp-pi-agent/pull/1))
- **CI** — `mypy` in strict mode plus `disallow_any_explicit`,
  `disallow_any_unimported`, `disallow_any_decorated` and `warn_unreachable`;
  `pytest`. Runs on Python 3.11 to match the Pi rather than the dev box, so CI
  cannot pass on something the Pi would reject.
- **Branch protection** on `main` — PR required, CI must pass, linear history,
  no force-push, applies to admins.
- **`.local/`** — gitignored directory for machine-local context a new session
  needs but a public repo must not carry (Linear project link, Pi connection
  details). Documented in `AGENTS.md`; never holds secrets.
- **`AGENTS.md`** — conventions for anyone changing this repo: the changelog
  rule, PR flow, CI setup, and the invariants that mean reopening an ADR rather
  than editing code (no WhatsApp write path, no shared context between reader and
  agent, cursor on `_id`, no message content in logs). `CLAUDE.md` points at it.
- Scaffolding: architecture, threat model, detection model, six ADRs, four
  runbooks, and `docs/OPEN-QUESTIONS.md`.

### Verified on hardware — 2026-08-10

The spike passed. Android 13 (LineageOS 20, VANILLA) runs in Waydroid on a
Raspberry Pi 5, companion pairing succeeds, and `msgstore.db` reads from the host
as plain SQLite with no key and no decryption.

- **WhatsApp detects the container** and shows a "custom ROM" warning on first
  launch. It is a warning with an OK button, not a block — detection layer L6 is
  "detected but permitted" rather than avoided.
- **Companion history is bounded to ~6 months**, not the full archive: 41,636 of
  42,000 messages are from 2026. This defines the assistant's maximum context
  depth. ([ADR 0002](docs/decisions/0002-waydroid-companion-device.md))
- Autostart survives an unattended reboot — container, Android (~50s), and
  WhatsApp self-starting via its `BOOT_COMPLETED` receiver.

### Fixed

- **Cursor keys on `_id`, never `timestamp`.** Companion devices deliver messages
  long after they were sent (808 of 812 sampled arrived >60s late; worst observed
  823s) and backfill inserts years-old rows, so rows are *not* inserted in
  timestamp order. A timestamp cursor silently drops late arrivals — intermittent
  message loss that looks like a WhatsApp fault. Encoded as a test so regressing
  it fails CI. ([ADR 0003](docs/decisions/0003-local-db-read.md))
- **Snapshots go to `/dev/shm`, not `/tmp`.** `/tmp` is *not* tmpfs on Raspberry
  Pi OS — it lives on the SD card and is merely cleared at boot by systemd.
  Snapshotting there would rewrite ~60MB to the card on every poll.
- **A broken output pipe no longer advances the cursor.** Messages that never
  reached a consumer are re-read on the next run: replaying is recoverable,
  silently skipping is not.

### Documented

Two undocumented Raspberry Pi 5 blockers that stop Waydroid dead, both with
misleading symptoms, now in [runbook 02 §0.5](docs/runbooks/02-waydroid-whatsapp.md):

- **16KB vs 4KB page size.** Pi OS boots Pi 5 with `kernel_2712.img` (16KB
  pages); Android images are built for 4KB. Android's `/init` segfaults instantly
  and the only evidence is `Child ended on signal Segmentation fault(11)` in an
  LXC debug log you have to know to enable. Fixed with `kernel=kernel8.img`.
- **PSI and the memory cgroup are disabled.** `lmkd` requires PSI; Pi OS ships
  `CONFIG_PSI_DEFAULT_DISABLED=y` and boots with `cgroup_disable=memory`. Android
  boots all the way to the boot animation, then dies after ~15 seconds. Fixed
  with `psi=1 cgroup_enable=memory cgroup_memory=1`.

Corrections to the original design docs, found by running on real hardware:

- Waydroid's Android `/data` is at `~/.local/share/waydroid/data`, **not**
  `/var/lib/waydroid/data` as originally written.
- Most group participants are identified by `@lid`, not phone JIDs (18,287 vs
  9,698), and don't join to `wa_contacts`. Sender-name resolution is only ~4.7%
  ([Q5](docs/OPEN-QUESTIONS.md)).

### Known limitations

- **The remaining ~50% of senders have no name anywhere on the device.** All
  1,464 of them do resolve through `jid_map` to a phone JID — they are simply
  strangers in large public groups who were never in the address book. Their
  pushnames, which the WhatsApp UI does display, are not in `msgstore.db`,
  `wa.db`, `sync.db`, `chatsettings.db`, `status.db`, `media.db`,
  `account_switcher.db` or `companion_devices.db`; `lid_display_name` covers a
  different 1,332 LIDs, and `wa_contact_details` and `integrator_display_name`
  are empty. A `grep -r` of a sample LID and its phone number across the whole
  `com.whatsapp` data directory hits `msgstore.db` and nothing else, and there
  only as JID strings. The name is fetched live and not persisted, so it is out
  of reach behind `PrivateNetwork=yes` (ADR 0006). Those senders stay as bare
  LIDs, which is the accepted interim behaviour.
- The reader must be run by hand — no timer, no confinement yet.
- No Signal channel, no agent. The system reads but cannot yet be talked to.
