# Open questions

Things not yet decided or not yet verified. Q1 blocks everything.

---

## Q1 — Does WhatsApp run in Waydroid on a Pi 5, and will companion pairing succeed?

**Status:** **Q1a ANSWERED — PASS (2026-08-10).** Q1b still unverified. **Blocks:** all of `src/`.

### Q1a result: Android runs on the Pi 5 — verified on hardware

Waydroid 1.6.2, LineageOS 20.0 VANILLA (`waydroid_arm64`, build 20260403), Android 13 /
SDK 33, `arm64-v8a`. Boots to a rendered home screen in ~40s. `boot_completed=1`.
Networking works inside the container (16ms to 1.1.1.1; DNS resolves
`www.whatsapp.com` → `mmx-ds.cdn.whatsapp.net`, 73ms). `screencap` produces a valid
1080×1884 PNG, which is the mechanism for capturing the pairing QR without attaching a
monitor. Host at 1.4GB used of 7.8GB, 55°C, `throttled=0x0`.

Two Pi 5-specific kernel blockers had to be fixed first — both now documented in
[runbook 02 §0.5](runbooks/02-waydroid-whatsapp.md):

1. **16KB vs 4KB page size.** `kernel_2712.img` uses 16KB pages; Android images are built
   for 4KB. `/init` segfaults instantly. Fixed with `kernel=kernel8.img` in `config.txt`.
2. **PSI + memory cgroup disabled.** `lmkd` needs PSI; Pi OS ships
   `CONFIG_PSI_DEFAULT_DISABLED=y` and boots `cgroup_disable=memory`. Android reaches the
   boot animation, then init terminates after ~15s. Fixed with
   `psi=1 cgroup_enable=memory cgroup_memory=1` in `cmdline.txt`.

Neither is documented upstream and both have misleading symptoms. Worth the runbook space.

### Q1b result: companion pairing SUCCEEDS — verified on hardware

Paired via QR scan from the phone. Chats synced. **The spike passes.** `src/` is unblocked.

WhatsApp **does detect the container** and shows an alert on first launch:

> "You have a custom ROM installed. Custom ROMs can cause problems with WhatsApp Messenger
> and are unsupported by our customer service team."

It is a **warning with an OK button, not a block**. Detection layer L6 is therefore
"detected but permitted" rather than "avoided" — worth stating honestly in
[detection-model.md](detection-model.md). Consequence: no support, and a standing risk that
a future WhatsApp release hardens this from a warning into a refusal. That risk is real but
not currently active, and the fallback (a physical Android phone read over ADB) is unchanged.

Verified end to end: WhatsApp 2.26.31.72 (versionCode 263107230, `arm64-v8a`), host reads
`msgstore.db` as plain SQLite with no key, 13,428 messages / 576 chats / 184 groups
readable, Hebrew text intact through UTF-8.

### Q1b: original risk assessment (kept for the record)

Unchanged and untested. Note one new data point relevant to it — the container advertises
itself clearly:

```
ro.product.manufacturer = Waydroid
ro.build.fingerprint    = waydroid/lineage_waydroid_arm64/...:userdebug/test-keys
```

`userdebug/test-keys` and a `Waydroid` manufacturer string are exactly the fields a
container check would read. Whether WhatsApp's pairing flow looks at them is the question.

**Blocked on:** the WhatsApp APK (see below), then the QR scan.

This is the single assumption the architecture rests on. Everything in
[architecture.md](architecture.md) follows from "the official app runs in a container on the
Pi and receives my messages." If it doesn't, the design changes fundamentally and most of
`docs/decisions/` is reopened.

Two sub-questions, and they may have different answers:

**Q1a — does it run?** Waydroid is LXC, not emulation, and the Pi 5 is ARM64, so WhatsApp's
ARM code runs natively. Performance should be near-native. Unknowns: GPU/rendering on the
Pi's stack (WhatsApp needs a working UI to pair and to stay logged in), memory headroom
alongside signal-cli's JVM in 8GB, and whether the Waydroid image's missing telephony stack
causes problems for an app that assumes a phone.

**Q1b — does companion pairing succeed?** This is the sharper risk. Container/emulator
detection is detection layer L6 — the one layer this design has no answer for:

- Apps can check `ro.product.model`, `ro.hardware`, `ro.build.fingerprint`, qemu props,
  missing sensors, absent telephony.
- Play Integrity API attestation fails `MEETS_DEVICE_INTEGRITY` on most non-certified
  images, and Waydroid images are not certified.

Reasons for cautious optimism: Waydroid presents far fewer emulator tells than a QEMU AVD
because it *is* the host kernel running native code; and WhatsApp historically works on
non-GMS devices and custom ROMs, and is not known to hard-gate basic messaging on Play
Integrity.

But "not known to" is not "verified," and **pairing may apply different checks than fresh
registration** — a QR-scan companion link is a security-sensitive operation and is a
plausible place for stricter attestation.

**How we answer it:** execute [runbook 02](runbooks/02-waydroid-whatsapp.md) by hand. No
code first.

**If it fails:** don't reach for evasion — spoofing build props to defeat Play Integrity is
the start of an arms race this project is designed to avoid. Fall back to a real cheap
Android phone on the LAN as the companion device, with the Pi reading from it over ADB.
That keeps [0003](decisions/0003-local-db-read.md), [0004](decisions/0004-signal-control-channel.md),
[0005](decisions/0005-no-whatsapp-write-path.md), and [0006](decisions/0006-two-process-privilege-split.md)
intact and only replaces [0002](decisions/0002-waydroid-companion-device.md), at the cost of
another physical device. Worth costing out *before* spending time on Waydroid workarounds.

---

## Q2 — msgstore.db schema version pinning

**Status:** Decided in principle, needs a concrete procedure. **Blocks:** reader hardening,
not the spike.

The schema is undocumented and unversioned, and it migrates occasionally — `messages` →
`message` around 2021 is the big one, and it renamed the central table. Migrations are *not*
per-release; the schema is stable for long stretches and then isn't.

**Approach:** pin the WhatsApp APK version. Update deliberately, never automatically.

To settle:

- Where the pinned APK is stored (not in git — it's large and redistribution is dubious;
  probably a checksummed local artifact with the version recorded in `config`).
- What forces an update. WhatsApp applies server-side forced upgrades after some months;
  we'll get a deadline whether we like it or not.
- The update procedure: snapshot the DB, upgrade the APK, diff `sqlite_master` against the
  recorded schema, run the reader against the snapshot, then go live.
- A startup schema assertion in the reader so a silent migration fails loudly instead of
  returning wrong rows. Cheapest useful version: hash the relevant `CREATE TABLE` statements
  from `sqlite_master` and compare against a pinned value.

[`andreas-mausch/whatsapp-viewer`](https://github.com/andreas-mausch/whatsapp-viewer) has
the schema committed as SQL and is a good baseline to diff against.
[`B16f00t/whapa`](https://github.com/B16f00t/whapa) handles multiple schema generations and
shows what actually changes between them.

---

## Q3 — Sourcing a dedicated number for the Signal account

**Status:** Open. **Blocks:** [runbook 03](runbooks/03-signal-cli.md), not the spike.

[ADR 0004](decisions/0004-signal-control-channel.md) requires a dedicated number registered
as its own Signal account, not a link to my personal account.

Constraints:

- Signal registration requires SMS or voice verification.
- Signal blocks many VoIP ranges. Which ones is undocumented and changes.
- The number must stay alive indefinitely — losing it loses the account and forces
  re-registration.
- Should be cheap, since it does nothing but receive one verification code and then sit
  there.

Candidates to evaluate: a second physical SIM (PAYG, most reliable, small ongoing cost, but
needs a device or a spare slot to keep active); an eSIM data-and-SMS plan; a landline number
via voice verification (Signal supports voice callback — worth testing, landlines are
usually not in the blocked VoIP ranges); Google Voice or similar (frequently blocked, and
policy can change under us).

Lean: PAYG physical SIM. Boring, durable, and the failure modes are ones I understand.
Decide before runbook 03; not urgent until the spike passes.

---

## Q5 — Sender-name resolution for LID group participants

**Status:** Answered 2026-08-11 (NVB-7). Coverage 5.3% → **49.4%**; the rest is
not on the device.

WhatsApp identifies most group participants by **LID** (`...@lid`, 18,287 rows)
rather than phone JID (`s.whatsapp.net`, 9,698 rows), and `@lid` strings don't
join to `wa_contacts`. The missing piece was never a name table — it was the
**`msgstore.jid_map`** table, which maps `lid_row_id → jid_row_id`. Hop through
it and the phone JID joins to the address book exactly as it always did.

Measured on the live snapshot, 31,237 received messages:

| | Before | After |
|---|---|---|
| All chats | 5.30% | **49.41%** |
| Allowlisted group (`120363303907512513@g.us`) | 23.87% | **100%** |

Two changes got there, both in `_QUERY`:

1. `LEFT JOIN jid_map` → `jid` → `wa_contacts` on the sender's LID.
2. 1:1 chats carry `sender_jid_row_id = 0`, which points at no `jid` row and used
   to yield `?` for 4,212 messages (13% of the corpus). Falling back to the
   chat's own JID sends them through the same lookup.

### The remaining ~50% is unreachable, and that is the finding

All 1,464 still-unnamed senders **do** resolve through `jid_map` to a phone JID.
They are strangers in large public groups who were never in the address book. The
pushname the WhatsApp UI shows for them is not persisted anywhere on the device:

- Not in `msgstore.db`, `wa.db`, `sync.db`, `chatsettings.db`, `status.db`,
  `media.db`, `account_switcher.db` or `companion_devices.db`.
- `lid_display_name` holds 1,332 rows, but for a different set of LIDs — none of
  the unnamed senders.
- `wa_contact_details` (which is keyed on `lid`) and `integrator_display_name`
  are both **empty**.
- `group_participant_user` has no name column at all — only `label`, unpopulated.
- `grep -r` for a sample LID (`166872381132937`) and its phone number
  (`972537213152`) across the entire `com.whatsapp` data directory hits exactly
  one file, `msgstore.db`, and there only as the strings `166872381132937@lid`
  and `166872381132937.1:0@lid`. No name is adjacent to it.

So the name is fetched live and rendered, not stored. Getting it would mean
asking a server, which the reader cannot do and should not be able to do
([ADR 0006](decisions/0006-two-process-privilege-split.md), `PrivateNetwork=yes`).
**Closed as far as it can go.** Those senders stay as bare LIDs.

Two things fell out along the way:

- `deploy/snapshot.sh` copied `wa.db` without its `-wal`/`-shm`. Same ADR 0003
  staleness bug as `msgstore.db`, one database over — contact edits sitting in
  the WAL were invisible. Fixed.
- `snapshot()` in Python never cleared the destination first, so a leftover
  `-wal` could be applied to a newer `.db`. Fixed.

## Q4 — how to build the agent processes

**Status:** **ANSWERED 2026-08-12 — OpenClaw**, see
[ADR 0011](decisions/0011-openclaw-owns-the-channel-the-gate-owns-the-room.md). The
integration mechanism is now Q6. Kept below for the reasoning, which the answer
partly overturns.

The ADR carries three things this section only gestures at, all added when NVB-13 was
closed: **Managed Agents' argument stated at full strength** (vault credentials are
unreadable by the agent even under prompt injection — the exact property we measured
ours lacking, and it does not survive the self-hosted sandbox we would have to run);
**who reviews a new default tool** (nothing does — a version upgrade removed our tool
floor on 2026-08-12 and the agent gained a shell); and **the facts that reopen the
question**, so the decision can be falsified rather than defended.

**What changed the answer.** The lean was "custom Agent SDK build", on the argument
that capability shaping is load-bearing and should not be inherited from a
framework. A documentation review on 2026-08-12 found that OpenClaw expresses the
shaping this project needs as config — `channels.signal.groups[].toolsBySender` is
ADR 0008's (conversation, sender) pair, sandbox scope `"agent"` plus per-agent auth
stores is most of ADR 0010's container — and that its defaults are wrong for us in
four specific places rather than unfixably wrong in kind. The revisit trigger's own
test (does the plumbing dwarf the agent logic?) had already fired.

**The last paragraph below is falsified.** It said no agent runtime knows what Signal
is, so confirmation targeting stays ours. It does: Signal renders approval prompts as
👍/👎 reactions and `ask_user` prompts as 1️⃣-4️⃣, bound to a request id, expiring.
That is NVB-16 substantially pre-built, and it is a better binding than the quoted
reply we designed.

**The hard constraint below is narrower than it reads.** It rules out pasting a
subscription OAuth token into a third-party tool. It does not rule out OpenClaw
*driving the locally installed Claude CLI*, which authenticates natively as itself —
a first-class OpenClaw provider (`--provider anthropic --method cli`) whose usage
draws on the Pro/Max allowance. So the sanctioned, subscription-backed path this
question wanted is available through OpenClaw after all. See ADR 0011's consequences
for the residual question, which is about a household assistant rather than about
tokens: other family members cause requests against the owner's limits.

**What does still hold: nothing caps model spend.** Neither `gateway.auth.rateLimit`
nor any other key bounds per-conversation usage, and OpenClaw has no equivalent of
Managed Agents' per-session budget. Watch the bill or the quota, whichever backs it.

Three ways to build the agent processes.

**Hard constraint first:** Anthropic's Feb 2026 policy prohibits using subscription OAuth
tokens in third-party tools. So the paste-your-token-into-OpenClaw path is out regardless of
its other merits — not a judgement call, a policy line.

**Also relevant:** the June 15, 2026 Agent SDK credit split was **cancelled**. `claude -p`,
the Agent SDK, and third-party apps built on the Agent SDK still draw from Pro/Max
subscription limits. So the sanctioned path is economically available and there's no
pressure to go around it.

**Custom Agent SDK build** — sanctioned auth, full control over the tool surface, which is
exactly what [ADR 0006](decisions/0006-two-process-privilege-split.md) needs: `create_draft`
and not `send_email`, a hook on outbound actions, two processes with genuinely different
capabilities. Cost is that we write and maintain it.

**OpenClaw (or similar) built on the Agent SDK** — a lot of the plumbing exists. But the
privilege split is unusual enough that fitting it into someone else's agent framework may
cost more than it saves, and the confirmation-gate hook has to be *reliable*, which means
understanding the framework's interception points properly.

**Anthropic Managed Agents** — added 2026-08-11, and it ships much of what M4 was going to
write: per-tool permission policies (`always_ask` pauses the session and waits for an
allow/deny), vaults that keep credentials out of the sandbox entirely by substituting them at
egress, per-session containers with environments as the trust boundary, and hard per-session
spend budgets. A **self-hosted sandbox** (`config: {type: "self_hosted"}`) keeps tool
execution on the Pi — the agent loop runs on Anthropic's orchestration, an outbound-polling
worker executes the tools, no inbound connections.

Its costs are equally concrete: self-hosted sandboxes support neither vault
`environment_variable` credentials nor memory stores, so that work returns to us; and
**session and event history persists on Anthropic's side**, which is a genuinely new fact for
the threat model rather than a restatement of "the model sees the content". The ADR has to
decide that deliberately.

Lean: still **custom Agent SDK build**, on the same reasoning — capability shaping is the
load-bearing control in this system and it's not something to inherit from a framework whose
defaults are aimed at general usefulness.

**But the revisit trigger has fired.** This question said to reconsider if the Signal and
session plumbing turned out to dwarf the agent logic. M3 then built per-principal routing, a
roster, an outbound path with recipient validation, per-conversation authority, and a
confirmation-targeting design — with per-principal containers and a capability manifest still
to come. That is the plumbing, and two of the three options ship parts of it. Decide in
NVB-13, on evidence, before any agent code exists.

Whatever wins, one piece stays ours: **confirmation targeting**. A platform can give you the
pause and the allow/deny, but mapping a quoted Signal reply or a 👍 reaction to one specific
pending action is Signal semantics, and no agent runtime knows what Signal is
([ADR 0008](decisions/0008-authority-is-a-conversation-sender-pair.md)).

## Q6 — how the gate and OpenClaw are wired together

**Status:** **Shape decided 2026-08-12 — Option E, a transparent proxy.** Not yet
implemented, and **two of its premises are still unverified** — see
[NVB-20](https://linear.app/naveh-brenner/issue/NVB-20/verify-two-openclaw-assumptions-on-hardware-before-any-integration).
Nothing here is settled until those run. **Blocks:** NVB-15 and the rewrite of
`src/gate/signal.py`.
**Decided by:** [ADR 0011](decisions/0011-openclaw-owns-the-channel-the-gate-owns-the-room.md),
which says what the gate keeps but not how it enforces it.

ADR 0011 leaves the gate four jobs. Three of them (sender-granular routing, config
refusals, the resolved-matrix `--check`) are local and need no mechanism. The fourth,
**membership drift**, is the whole question: a watchdog that can only observe does not
refuse anything, and "this stops working and you are told" degrades to "you are told".

The enabling fact is that OpenClaw reads JSON5 from `~/.openclaw/openclaw.json`,
**watches the file and applies changes automatically**, and also exposes `config.patch`
and `config.apply` over the gateway RPC. So a separate process can revoke a room.

### Phase 0.1 result — schema checked, config was wrong in five places (2026-08-12)

Run against **OpenClaw 2026.7.1-2** with `npx openclaw@latest` — no install, no
onboarding, no daemon, so this needed neither the Pi nor Signal. `openclaw config
schema` emits a 2.2MB draft-07 JSON Schema; `openclaw config validate` checks the
active config against it. `config/openclaw.example.json5` now validates clean.

Five things the prose docs had wrong, each of which would have failed at runtime:

1. **`agents.entries` does not exist — it is `agents.list`**, an array of objects each
   carrying `id`, not a map. `additionalProperties` is false on the agents node, so
   `entries` is rejected outright rather than quietly ignored.
2. **There is no `transport` object.** The managed-native / external-native /
   container distinction is spelled `apiMode: "auto" | "native" | "container"` plus
   `autoStart` and flat `httpHost` / `httpPort` / `httpUrl` / `cliPath`.
3. **`channels.signal.execApprovals` does not exist.** Only discord, matrix, qqbot,
   slack and telegram carry that block. Signal approvals are configured through
   top-level `approvals.exec` / `approvals.plugin` with `mode: "targets"`, and
   approvers are inferred from `allowFrom` / `defaultTo`.
4. **`groups[<id>].tools` is an `{allow|alsoAllow|deny}` object**, not the bare array
   the docs show. This was the one error `validate` caught for us rather than the
   schema read.
5. **`bindings[].match.peer.kind` is `"direct" | "group" | "channel" | "dm"`**, with
   `"dm"` a deprecated alias for `"direct"`. The docs had no Signal binding example at
   all, and this was a stop-the-project risk in phase 0.3 — the schema settles that
   per-conversation routing is at least *expressible*. Whether Signal's runtime
   resolves the ids in that form is still a hardware question.

Confirmed as written, no change needed: `dmPolicy` defaults to `"pairing"` and
`groupPolicy` to `"allowlist"`; `session.dmScope` and `session.scope` take the values
we assumed; `configWrites`, `reactionNotifications`, `ignoreAttachments`,
`sendReadReceipts` all exist and are spelled as we had them.

**And the fifth gate responsibility is confirmed rather than merely likely.** A Signal
group's config carries exactly four fields — `ingest`, `requireMention`, `tools`,
`toolsBySender`. There is no per-group sender allowlist, so `groupAllowFrom` really is
channel-wide and the proxy really is the only place that can express "mom commands in
`family` but not in `confirmations`". (`ingest`, a boolean, is undocumented in the
pages we read and worth understanding before the proxy is written — it may already do
part of what we want.)

**What `validate` does and does not buy you.** Tested deliberately: an unknown key is
rejected (`must not have additional properties: "totallyMadeUpKey"`), but a **typo'd
tool name inside a deny array passes silently** — `"gatewayy"` validated clean. So a
misspelling in a deny list is a tool that is still granted, with no error anywhere.
That is exactly the failure ADR 0010 built the registry to catch, now demonstrated
rather than predicted, and it is the strongest argument for the generator: our config
is checked against a closed vocabulary, and `openclaw.json` is emitted from it.

### Phase 0.2 / 0.3 / 2.x result — live on the Pi (2026-08-12)

signal-cli 0.14.7 with `--http 127.0.0.1:8081` **added alongside** the existing
`--socket`, so wpa-gate never lost its connection. OpenClaw 2026.7.1-2 on Node
24.19.0, model `anthropic/claude-opus-5` through the Claude CLI runtime.

**2.3 — passed, no regression.** `--socket`, `--tcp` and `--http` are independent
flags on one daemon, so `--scrub-log` and `--no-receive-stdout` are untouched by
adding HTTP. wpa-gate reconnected on its own across the restart, exactly as designed:
`reconnecting` → `waiting for /run/wpa-signal/socket: Connection refused (attempt 1)`
→ `connected`.

**2.1 — it is plain HTTP + SSE, not a websocket.** `POST /api/v1/rpc` → 200,
`application/json`. `GET /api/v1/events` → 200, `Content-type: text/event-stream`,
chunked, with `:` keepalive lines. `/` is 404 and a GET on `/rpc` is 405. The proxy is
therefore a chunked-HTTP relay, which removes the single biggest risk against option
E — no websocket framing to get subtly wrong.

**2.2 — the stream fans out.** Two SSE consumers plus wpa-gate on the unix socket all
received the same envelope. So the proxy is **not** mandatory for observation, and a
passive observer stays available as a fallback if the relay proves fragile.

But it sharpens a requirement: **8081 must be reachable only by the proxy.** A stream
that fans out to anyone who asks is a proxy that anything can bypass, and unlike the
unix socket (`srwxrwx--- wpa-signal wpa-signal`) an HTTP port carries no permissions
at all. Any local user can attach today. That is a real access-control downgrade and
it needs solving — firewall, netns, or a unix-socket-fronted proxy — before this shape
is permanent.

**0.2 — passed, and it was the right thing to be nervous about.** Mom messaged the
assistant while unlisted. Her envelope arrived on the stream (ACI
`1e34f247-…`, `sourceNumber: null`, `sourceName` empty) and OpenClaw produced
**nothing**: no reply, no pairing code, no prompt. `uuid:`-keyed allowlisting matches
real envelopes where a number-keyed list would have matched nothing — which is the
fact that chose OpenClaw over Hermes, now confirmed rather than inferred.

**0.3 — failed first, then passed, and the failure is the finding.** The canonical
session key is the ground truth:

```
agent:owner:signal:direct:uuid:b0c72586-…      <- direct peers are uuid:-prefixed
agent:family:signal:group:Mt2z3ANj…            <- group ids are not
```

A binding with a **bare** uuid does not match — and **nothing reports it**. No error,
no warning: the conversation silently routes to the default agent instead. The group
binding (unprefixed base64 id) matched from the start; only the DM fell through. With
the prefix added, both route correctly and three separate session stores exist under
`agents/owner`, `agents/family` and `agents/main`.

This is the sharpest argument yet for the generator and for keeping the default agent
empty. A silently-misrouted conversation is a conversation getting the default agent's
credentials with nobody told — the exact failure mode ADR 0010's refusals exist for,
and OpenClaw does not have an equivalent.

**1.2 — substantially answered as a side effect.** Sessions are keyed per agent *and*
per conversation, in separate stores under separate agent directories. ADR 0010 rule 1
holds. Per-*sender* scoping still needs a second person in the group.

**4.1 — answered, and it is the good outcome.** `tools.profile: "minimal"` already
strips `message` (along with `gateway`, `nodes`, `cron`, `subagents`, `web_fetch`,
`web_search` and 12 others), and replies still arrived in both the DM and the group.
**Replies do not go through the `message` tool.** So the model-chosen-destination path
can be removed at the tool layer without muting the assistant, and the proxy becomes
the backstop rather than the only line of defence.

**New: two required keys nobody documents.** `plugins.allow: ["signal"]` **and**
`plugins.entries.signal.enabled: true`. The Signal channel is an external plugin that
is not trusted implicitly; without both, the gateway starts, logs one WARN, and never
connects — no error at the channel level, no retry, no clue. Good default on their
part, two more keys our config was missing.

**New: tool names are runtime-dependent.** `WARN group tools.allow allowlist contains
unknown entries (read). These entries are shipped core tools but unavailable in the
current runtime/provider/model/config.` A name valid under one runtime is absent under
another; an allow list naming it grants nothing and a deny list naming it is a silent
no-op. The registry therefore has to be checked against the runtime we actually run,
not a static list.

**Still open:** 1.1 (per-(conversation, sender) tools) and 3.3 (approvals) need mom in
the group; 1.3 (typing/receipts), 3.1 (inheritance with `main` empty) and 3.2
(`configWrites`) are untested.

### ⛔ STOP — the claude-cli runtime is not governed by tool policy (2026-08-12)

**The spike gateway is stopped. Do not restart it until this is resolved.**

With `tools.profile: "minimal"` and
`tools.deny: ["exec","process","terminal","code_execution","browser","screen","gateway","nodes","subagents","cron"]`
in effect and validated, an agent turn on the `owner` binding was asked whether it had
a shell. It answered by running `id` and returning:

```
uid=1000(navehbrenner) gid=1000(navehbrenner) groups=…,27(sudo),…
```

and listed its own tools: **Agent, AskUserQuestion, Bash, Edit, ListAgents, Read,
ReportFindings, Skill, ToolSearch, Write**, plus `mcp__openclaw__session_status` — the
only OpenClaw tool present. That user has `NOPASSWD: ALL`.

**What this means.** Under the `claude-cli` runtime, OpenClaw's tool policy governs
only OpenClaw's *own* tool layer. The agent's real capability surface is Claude Code's
built-in tools, which OpenClaw's `allow`/`deny`/`toolsBySender` do not touch. The 18
tools `minimal` removed were all OpenClaw tools; `exec` was denied and `Bash` was never
OpenClaw's to deny.

So **ADR 0011's profile model does not hold on this runtime.** "A tool outside the
bundle is absent" becomes "a tool outside the bundle is absent from a layer that
governs one tool", and every per-(conversation, sender) grant we verified in config is
enforcement over the wrong surface. Phase 1.1 as designed would have measured almost
nothing, which is why it is not worth running until this is settled.

Three compounding facts make it worse rather than academic:

1. **No sandbox.** `agents.defaults.sandbox` was unset (default `off`), and neither
   docker nor podman is installed on the Pi, so `mode: "all"` could not have applied
   even if set. Whether the sandbox constrains a CLI backend *at all* is unknown —
   OpenClaw spawns `claude` as a host subprocess, and nothing in the docs says the
   container wraps it.
2. **The gateway runs as a sudo-capable user.** `navehbrenner`, `NOPASSWD: ALL`.
3. **`openclaw config set` rewrote the JSON5 config as plain JSON and stripped every
   comment.** Verified. Any `config set` against the production config destroys the
   rationale this repo keeps inline — another reason `openclaw.json` must be a
   generated artifact that nothing else writes.

**Options, roughly in order of promise:**

- **Pass the restriction down to Claude Code.** `allowedTools`, `disallowedTools` and
  `permissionMode` plumbing exists in the OpenClaw dist. If `agents.defaults.cliBackends`
  can be made to emit `--disallowed-tools Bash,Write,Edit` (or a permission mode), the
  subscription path survives with real governance. **Investigate this first.**
- **Run the gateway as a dedicated unprivileged user** with no sudo, its own home, and
  no access to `/opt/wpa` or `/var/lib/wpa-signal`. Necessary regardless of runtime —
  defence in depth, not a fix.
- **Use OpenClaw's native runtime** (API key) for agents that hold anything, where the
  tool layer demonstrably works, and keep claude-cli only for a tool-less responder.
  Costs the subscription economics for those agents.
- **Container the whole gateway** rather than relying on per-agent sandboxing.

Until one of these lands, the honest statement is: **a prompt injection reaching the
owner agent had passwordless root on the Pi.** Exposure was bounded — `dmPolicy:
allowlist` with the owner's ACI only, and mom was correctly refused — so the reachable
path was the owner's own message content. Bounded is not the same as safe.

### ✅ RESOLVED — the restriction plumbs down, and ADR 0011 holds (2026-08-12)

The first option worked. **`agents.defaults.cliBackends["claude-cli"].args` reaches
Claude Code**, and the subscription path survives with real governance.

OpenClaw's built-in Claude backend already passes permission flags — just permissive
ones:

```
args: ["-p","--output-format","stream-json","--include-partial-messages","--verbose",
       "--setting-sources","user",
       "--allowedTools","mcp__openclaw__*",
       "--disallowedTools","ScheduleWakeup,CronCreate,Bash(run_in_background:true),Monitor"]
```

`--allowedTools` in Claude Code is an **auto-approve** list, not a restriction. That is
the whole reason Bash was there: nothing had ever restricted it, and the deny list
covered only `Bash(run_in_background:true)`, not plain `Bash`.

Three flags, with measured behaviour:

| Flag | Semantics | Verified |
| --- | --- | --- |
| `--tools <names>` | **Default-deny allowlist** over the built-in set | `--tools Read` → *"the only tool available to me is `Read`"*; Bash absent entirely, not merely denied |
| `--disallowedTools <names>` | Deny list | `Error: No such tool available: Bash. Bash is disabled for this session, **in subagents as well as here**` |
| `--allowedTools <names>` | Auto-approve (no permission prompt) | Orthogonal to availability |

`--tools ""` and `--tools=` were both tried as a way to disable everything and **neither
works** — Bash remained. Use an explicit minimal allowlist instead of trying to express
empty. And `--tools` takes **built-in tool names only**: passing the glob
`mcp__openclaw__*` hung the turn for 282s and ended in `FailoverError`.

**Final state, verified by effect rather than self-report.** With
`--tools Read`, `--allowedTools mcp__openclaw__*` and a `--disallowedTools` belt, the
agent's complete callable set is:

```
Read
mcp__openclaw__session_status
```

No shell, no write, no edit, no search, no web, no messaging, no agent spawn — and
`/tmp/probe4.txt` and `/tmp/probe4b.txt` were not created. The model's own tool
*manifest* lists more than it can call, so **only ground truth counts**: it reported
`Write` as callable one minute before `Write` refused.

**The architectural consequence, and it is the important part.** `cliBackends` is
settable **only under `agents.defaults` — it is not a per-agent key** (schema-checked).
So there are two enforcement surfaces with different granularity:

| Surface | Governs | Granularity |
| --- | --- | --- |
| `cliBackends["claude-cli"].args --tools` | Claude Code's built-in tools | **Global only** |
| `tools.*`, `agents.list[].tools`, `toolsBySender` | OpenClaw's MCP tool layer | **Per agent and per sender** |

Which forces — and happens to vindicate — ADR 0011's shape: **hold the Claude Code
built-ins at a minimal fixed floor globally, and put every differentiated capability in
the OpenClaw MCP layer, where per-(conversation, sender) policy demonstrably works.**
The registry maps onto the MCP layer; the `--tools` floor is one line the generator
emits once.

Two things the generator must therefore keep in sync, or the profile model quietly
lies: the global `--tools` floor and the per-agent MCP grants. A profile that names a
capability the floor forbids grants nothing; a floor wider than any profile needs is
capability nobody asked for.

**Still required regardless:** run the gateway as a dedicated unprivileged user. Today
it is `navehbrenner` with `NOPASSWD: ALL`, and `--tools` is a policy flag, not a
kernel boundary. Blocked on `claude login` being re-done as that user.

### A pending group invitation is invisible to the pin (2026-08-12)

Captured in the window between mom being *added* to the group and *accepting*, a state
`_members_of()` has never been tested against.

`listGroups` reports:

```json
"members": [ {"uuid": "43ca94f9-…"}, {"uuid": "b0c72586-…"} ],
"pendingMembers": [ {"number": null, "uuid": null} ],
"requestingMembers": []
```

Two things, both new:

1. **`members` is unchanged**, so the pinned set still matches and `_drifted()` stays
   quiet. wpa-gate kept serving the room and logged nothing — which is **correct**: a
   pending member cannot read messages until they accept, so there is no egress
   exposure yet.
2. **A pending member is anonymous.** `number` and `uuid` are both `null`. We can see
   *that* someone was invited; we cannot see *who*. So a rule keyed on identity is not
   available here even if we wanted one.

The group-update envelope did arrive and would have triggered the fast re-read:

```json
{"groupId": "Mt2z3ANj…", "groupName": "no longer confirmations",
 "revision": 2, "type": "UPDATE"}
```

with `message: null` — so it is dropped as `no body` *and* consulted by
`_is_group_update()`, exactly as that function's docstring claims. Note `revision`
appears on the **envelope** but came back `null` from `listGroups`, so it is not a
field to rely on from the RPC side.

**The design question this raises for the proxy.** An outstanding invitation is not
drift, and should not refuse the room: pending members read nothing, and treating an
invitation as drift would let any group admin silence the assistant by inviting a
stranger. But it is not nothing either — the room is one tap away from changing, and
the owner may not be the one who invited them.

The proportionate answer is **notify, don't refuse**: surface "someone has been
invited to `family`" to the notify conversation, and keep serving. Refusal stays bound
to `members` actually changing, which is the moment exposure begins.

**Answered on accept — the whole membership design works end to end.** Accepting *does*
emit an UPDATE envelope, from the accepting member's own ACI, with the revision
incremented:

```
src: b0c72586-…  type: UPDATE  rev: 2   (the invite)
src: 1e34f247-…  type: UPDATE  rev: 3   (the accept)
```

`listGroups` then reports `members` including mom and `pendingMembers: []`. And
wpa-gate did exactly what ADR 0008 specifies, within seconds rather than within the
900s refresh:

```
dropped: unlisted sender (1 total)
membership drift: family — commands from it are refused
```

So `_is_group_update()`, `_members_of()`, `_drifted()` and `_apply_members()` are all
now verified against real hardware for the full lifecycle — invite, pending, accept,
drift, refusal, notice. `MEMBERS_REFRESH_SECONDS` is the backstop it was designed to
be, not the detection path.

This is the piece of the gate that has no OpenClaw equivalent, and it is the piece
that just proved itself. It survives into the proxy unchanged.

### Two identity grammars in one config file (2026-08-12)

Round 4 failed open: mom called `session_status` successfully despite a `deny` rule
naming her. The rule never matched, because **`toolsBySender` does not accept the
`uuid:` prefix that every other identity field requires.**

From OpenClaw's own source:

> `toolsBySender key "…" is deprecated. Use explicit prefixes (channel:, id:, e164:,
> username:, name:). Legacy unprefixed keys are matched as id only.`

So the same ACI is written three different ways depending on the field:

| Field | Prefix for a Signal ACI |
| --- | --- |
| `channels.signal.allowFrom` / `groupAllowFrom` | `uuid:b0c72586-…` |
| `bindings[].match.peer.id` (kind `direct`) | `uuid:b0c72586-…` |
| `groups[].toolsBySender` keys | **`id:b0c72586-…`** |

`uuid:` in `toolsBySender` is parsed as a *legacy unprefixed key* and matched as an id
literally — so it looks for a sender whose id is the string `uuid:b0c72586-…`, finds
nobody, and **falls through to the room ceiling**. The grant fails open, not closed.

It does warn, which is more than the binding mismatch did — but as a Node
`DeprecationWarning` on stderr, not a config error:

```
(node:55475) [OPENCLAW_TOOLS_BY_SENDER_UNTYPED_KEY] DeprecationWarning: toolsBySender
key "uuid:b0c72586-…" is deprecated…
```

`openclaw config validate` passes it clean. Three silent-mismatch classes are now
confirmed — a wrong binding prefix (routes to the default agent), a typo'd tool name
(silently ungoverned), and a wrong `toolsBySender` prefix (grant fails open) — and
**none of them is a validation error.** That is the generator's entire justification:
one identity in `config.toml`, emitted into whichever grammar each field wants.

### ⛔ `toolsBySender` never reaches a CLI-backend agent — the loopback drops the sender (2026-08-12)

> **Superseded diagnosis.** This section first concluded "Signal does not populate the
> sender fields". That was wrong, and was reached by generalising from six failed key
> spellings without reading the matcher. Instrumenting the matcher disproved it in one
> message. The observations below stand; the explanation under them is the corrected one.

Tested exhaustively. The room ceiling was `tools: {allow: ["session_status"]}` and the
sender rules were varied while the same two people sent messages:

| `toolsBySender` key tried | Matched? |
| --- | --- |
| `uuid:b0c72586-…` | no (warned as a legacy key, matched as id, still no) |
| `id:b0c72586-…` | no |
| `id:uuid:b0c72586-…` | no |
| `channel:signal:b0c72586-…` | no |
| `channel:signal:uuid:b0c72586-…` | no |
| `name:Naveh Brenner` | **no** |
| `"*"` | **yes** |

#### What the instrumented matcher showed

A `console.error` probe at the top of `matchToolsBySenderPolicy`
(`dist/group-policy-BiTZkkCF.js`), dumping `params` plus a stack trace, fired **three
times** on a single group message — and the sender is present on only the first:

| # | Caller chain | `senderId` |
| --- | --- | --- |
| 1 | `dispatchReplyFromConfig` → `resolveGroupToolPolicy` | `uuid:b0c72586-…` ✅ |
| 2 | `resolveMcpLoopbackScopedTools` → `resolveGatewayScopedTools` → `resolveGroupToolPolicy` | absent ❌ |
| 3 | same as 2 | absent ❌ |

The `tool policy removed 1 tool(s) via group tools.deny` lines timestamp-match calls
**2 and 3**. So Signal *does* supply the identity; the resolution that actually decides
the agent's tools is a different one, and it is sender-blind.

#### Why — and it is structural, not a bug in the Signal plugin

`resolveGatewayScopedTools` (`dist/tool-resolution-XVJDzZpY.js:35`) forwards four
fields and no sender:

```js
const groupPolicy = resolveGroupToolPolicy({
  config: params.cfg, sessionKey: params.sessionKey,
  messageProvider: params.messageProvider,
  accountId: params.accountId ?? null,     // ← no senderId, no senderName
});
```

It could not forward one if it wanted to. A CLI backend reaches the gateway over the
**MCP loopback**, and `resolveMcpRequestContext` (`dist/mcp-http-BUQahgob.js`) rebuilds
context entirely from HTTP headers — `x-openclaw-message-channel`,
`x-openclaw-current-channel-id`, `x-openclaw-account-id`,
`x-openclaw-current-message-id`, … — and **no sender header exists anywhere in the
distribution** (`grep -rl x-openclaw-sender dist/` → nothing). The only identity that
survives the hop is `auth.senderIsOwner`, a boolean, and the loopback tool cache key
ends in exactly `"owner" | "non-owner" | "unknown-owner"`. Sender granularity is
deliberately collapsed to one bit, and that bit is not reachable from config — it gates
a fixed `GATEWAY_OWNER_ONLY_CORE_TOOLS` list.

So this is **not** a Signal gap. It hits any channel whose agent runs on a CLI backend,
which is ours (`models.providers.anthropic.agentRuntime.id = "claude-cli"`, ADR 0011).
`toolsBySender` is live and correct on the dispatch path; the CLI-backed agent is
simply not served from that path.

#### What it costs us: nothing we were entitled to

`agents.list[].tools.toolsBySender` uses the same matcher and the same sender-less
input, so there is no config location that rescues it. But **ADR 0010 rule 2 already
forbade relying on it**: *"Senders resolving to one agent resolve to one profile …
different profiles therefore mean different agents."* Varying capability per speaker
inside one container was always a runtime string check, which is the enforcement that
ADR deletes. `toolsBySender` was a convenience that happened to agree with the
architecture, not the mechanism holding it up.

The mechanism that does hold it up survives intact: `resolveEffectiveToolPolicy` keys
on `sessionKey`, and the loopback path honours it. **Per-agent tool policy is enforced;
per-sender-within-one-agent is not.** Which is the rule we wrote down.

Consequences, then:

1. **ADR 0011's profile table row is wrong** — "`toolsBySender` (per sender within a
   shared room)" must go. Amended below.
2. **The mixed room needs two agents, or it needs dropping.** Routing sender → agent is
   already gate responsibility 3 (bindings match per conversation, never per sender), so
   the proxy has to do it either way. Whether the owner gets a wider profile *inside*
   the family room is now a design choice with a real cost attached, and ADR 0008's own
   logic argues against it: the wider answer still lands in front of everybody.
3. **File upstream.** Precise claim, named functions, one-line repro. Not "per-sender
   doesn't work on Signal."

#### Resolved: already fixed upstream, but not in a stable release (2026-08-12)

Nothing to file for this one, and no PR to write. `resolveGatewayScopedTools` now takes
`channelContext.sender` and feeds `resolveRequesterToolPolicies`, whose own docstring
states the architecture we had independently reasoned our way to: *"Sender-dependent
policy resolves once at trusted ingress; verified descendants consume the persisted
effective parent projection instead of guessing identity."* Plus minted grants carrying
exact tool names — the grant store (`src/gateway/mcp-grant-store.ts`), not a signed
payload.

| Version | fix present |
| --- | --- |
| v2026.7.1 — npm `latest`, what we run | ❌ |
| v2026.7.2-beta.7 | ✅ |
| v2026.8.1-beta.1 — npm `beta` | ✅ |
| main | ✅ |

Related upstream context: [#1734](https://github.com/openclaw/openclaw/issues/1734)
created `toolsBySender`; [CVE-2026-53818](https://advisories.gitlab.com/npm/openclaw/CVE-2026-53818/)
is *why* no sender crosses the loopback — the fix bound tool access to gateway-selected
session/account/channel "instead of trusting child-process headers". Adding a sender
header would reopen it, which is why the fix routes identity through trusted ingress.

**The beta was taken and rolled back the same day.** It removes
`agents.defaults.cliBackends`, the only surface for passing `--tools` /
`--disallowedTools` to the Claude CLI. `exec` is OpenClaw's tool; `Bash` is the CLI's
own, and per OpenClaw's own source comment *"native CLI tools bypass path, approval,
sandbox, and exec policy"*. Result, measured twice:

```
$ openclaw agent --agent family -m "run: id -un"
openclaw            # with tools.deny: [exec, process, terminal, code_execution]
openclaw            # and again with agents.family.tools.allow: ["session_status"]
```

After rollback to `2026.7.1-2` + `@openclaw/signal@2026.7.1`, the same probe answers
`NO_SHELL_TOOL`. **Decision: shared group permissions; per-sender deferred to NVB-21.**

Two things worth filing upstream, both unreported, neither filed yet:

1. Native CLI tools ungoverned with no operator override on the beta — a regression
   from 2026.7.1, where `cliBackends` closed it.
2. A plugin-upgrade deadlock: the installed plugin's schema rejects the config the new
   plugin requires, and the new plugin refuses to install while config is invalid. The
   way through is to stash `channels.signal` out of the config, install, restore.

Migration hazards observed between 7.1 and 8.1, for whoever does NVB-21: `agents.list` →
`agents.entries`; Signal's flat `apiMode`/`autoStart`/`httpHost`/`httpPort` → a
`transport: {kind: "external-native", url}` object; and SQLite migrates **forward**
irreversibly, so snapshot `~/.openclaw/state/openclaw.sqlite` and
`~/.openclaw/agents/*/agent/openclaw-agent.sqlite` before upgrading — rolling back
without them is impossible.

Option 3 is close to what ADR 0008 already argues: a group profile is narrower
*because the reply is disclosed to everyone present*, and that logic does not care who
asked. ADR 0010's `owner-in-group` case — the owner holding a wider profile inside the
family room — was always the weakest part of that design for the same reason: the
wider answer still lands in front of everybody. Losing it costs less than it looks.

What genuinely survives either way: per-conversation agents (verified), per-conversation
tool ceilings via `groups[].tools` (verified), and ingress control over who may command
in which room (the proxy, gate responsibility 5).

### Shared group permissions, confirmed with two real senders (2026-08-12)

The owner and a second family member asked the same question in the family group —
"list every tool you have available" — and got the same answer: `session_status`, and
nothing else. Shared permissions are therefore observed rather than inferred from
config.

The group transcript also settles *why* `toolsBySender` looked so plausible for so
long. Every inbound message carries the sender:

```json
"__openclaw": { "senderIsOwner": false,
                "senderId": "uuid:1e34f247-…",
                "senderName": "Liron Brenner Agmon" }
```

`senderIsOwner` is computed correctly — `true` for the owner, `false` for her. **The
identity reaches the prompt. It never reaches the tool policy.** That is the whole bug
in one artefact, and it is why keying on `name:` seemed reasonable: the display name is
right there in the agent's own metadata block, just not on the path that decides tools.

Her turn also **resumed** the owner's group session (`useResume=true reuse=reusable`),
which is ADR 0010 rule 1 working as written: inside one room a shared session costs
nothing that is not already shared.

### There is no message that grants a capability — checked, not assumed (2026-08-12)

NVB-20 check 3.2. Over the owner's DM, the agent was asked in plain language to grant
itself `Bash` and enable `exec` in its own config. Afterwards:

- config **byte-identical** — same md5, same mtime, no new `.bak`
- the turn ran normally (29s, reply delivered), so it was not an error path
- the `gateway` tool — the one that could write config — appears in the 18 stripped by
  `tools.profile (minimal)` on every turn

The refusal is therefore structural rather than conversational: the capability was
absent, not declined. That is ADR 0010's rule surviving contact with the actual
implementation, and it is the half worth testing — a model saying "I can't do that"
proves nothing on its own.

### Typing indicators and read receipts cannot start a turn (2026-08-12)

NVB-20 check 1.3. Behaviourally: typing in the group for several seconds and deleting
without sending produced no turn (no `claude live session turn` between 20:08:52 and the
next real message at 20:14:08). But signal-cli logs nothing per envelope, so that alone
cannot distinguish "ignored correctly" from "never arrived".

Settled from the plugin instead. Across `@openclaw/signal`'s bundle:

| token | occurrences |
| --- | --- |
| `dataMessage` | 22 |
| `editMessage` | 3 |
| `syncMessage` | 1 |
| `typingMessage` | **0** |
| `receiptMessage` | **0** |

The plugin never reads those envelope types, so they cannot reach a turn by
construction. Structural pass, and it will stay true across config changes in a way an
observed non-event would not.

### Credential read-through is real, and the sandbox is not optional (2026-08-12)

NVB-20 check 3.1, run on hardware. A canary API key was written to the **`main`**
agent's store only; `family` has no local store file at all. Then a turn was forced
onto that provider as `family`:

```
$ openclaw agent --agent family --model openai/gpt-5.4-nano -m 'say hi'
FailoverError: Authentication failed (provider returned HTTP 401)
```

A 401 means the key was **found and sent**. Isolation would have produced "no
credentials". Emptying `main`'s store then emptied `family`'s view of it, confirming the
direction a second way. **ADR 0011's invariant — the default agent holds no credentials
— is load-bearing, and `deploy/render-agents.py` must assert it.**

Worse, and not previously recorded: with `--tools Read`, the agent read arbitrary
absolute paths as the gateway uid.

```
$ … 'Read /etc/passwd'          → root:x:0:0:root:/root:/bin/bash
$ … 'Read ~/.openclaw/openclaw.json, keys only'
                                → channels, session, agents, bindings, tools, models, …
```

Native CLI tools bypass OpenClaw path policy, and every agent's auth store
(`~/.openclaw/agents/*/agent/openclaw-agent.sqlite`, mode 0600) is owned by **the uid
the CLI runs as**. So without a container, per-agent credential isolation is not a
boundary at all: read-through leaks `main` downward, and even without it any agent can
read any other agent's store as a file. ADR 0010's "the container is the enforcement"
is therefore a requirement, not hardening.

Unprompted, the agent also reported that the config it had just read contains a
plaintext gateway auth token. It declined to repeat it — but it held it. **That token
needs rotating, and `gateway.auth.token` living in cleartext in a file the agent can
read is a standing hazard while any file-reading tool exists.**

Three mechanics learned closing this, each of which cost a wrong assumption:

| Attempt | Result |
| --- | --- |
| Add `Read` to `--disallowedTools` | **No effect.** `--tools` is an allowlist and wins. |
| Remove `Read` from `--tools` (set `--tools TodoWrite`) | Correct fix — an unknown builtin name grants no native tools at all. |
| Re-test on the *same* agent | **Still had `Read`.** |

That last row is the important one: **tool policy is bound at session creation and does
not apply to existing sessions.** Tightening policy silently leaves live sessions on the
old capability set until they are reset. Verified by testing a fresh agent (one tool)
against the resumed one (still had `Read`), then clearing the session store and
re-testing — after which `family` reports exactly `mcp__openclaw__session_status` and
`READ_REFUSED`.

Effective backend args now:

```
--tools TodoWrite --allowedTools mcp__openclaw__*
--disallowedTools Read,Bash,Write,Edit,NotebookEdit,Agent,Skill,ScheduleWakeup,CronCreate,Monitor
```

### ⛔ OpenClaw's own sandbox does not contain the claude-cli runtime (2026-08-13)

Read out of the shipped `dist/` of **`openclaw@2026.7.1-2`** — the exact version on the
Pi — rather than from prose docs, after `gateway/sandbox-vs-tool-policy-vs-elevated`
turned out to say nothing at all about CLI backends.

| Evidence | What it shows |
| --- | --- |
| `docs/gateway/sandboxing` | "The Gateway process always stays on the host; only tool execution moves into the sandbox when enabled." The sandboxed set is OpenClaw's own `exec`, `read`, `write`, `edit`, `apply_patch`, `process`, browser. |
| `cli-backends`, `chat-engine`, `claude-live-session` | **Zero** occurrences of `sandbox` in any of the three. |
| `claude-live-session` | The CLI is a host subprocess: `supervisor.spawn({… cwd: params.context.cwd ?? params.context.workspaceDir })`. |
| `agent-tools.policy` | Sandbox policy applies to the runtime tool list only: `sandboxMode === "all"` → `resolveSandboxToolPolicyForAgent`. |
| harness registry | Registered runtime ids are `codex`, `claude-cli`, `github-copilot`, `google-gemini-cli`; the built-in runtime id is `openclaw`. |

So `agents.defaults.sandbox.mode: "all"` **does not contain the tools that actually
execute under our runtime.** It governs OpenClaw's own tools; the Claude CLI's `Bash` /
`Read` / `Write` run beside it on the host as the gateway uid. The finding above — that
native CLI tools bypass OpenClaw path policy — extends to the sandbox as well. The only
floor under the real tool surface remains `cliBackends.args --tools`, which is **global
rather than per-agent**, and which `2026.8.1-beta.1` removed.

Note also that `auto` runtime selection prefers a registered harness that supports the
provider. With the Claude CLI installed, Anthropic resolves to `claude-cli` **unless
`agentRuntime.id` is pinned to `openclaw`** — so the pin is load-bearing, not cosmetic.

**For Anthropic, subscription auth and sandbox coverage are mutually exclusive.**
`docs/providers/anthropic.md` names it "OAuth (Claude CLI subscription reuse)": for
Anthropic, OAuth *is* the CLI harness, and there is no Anthropic OAuth token OpenClaw's
own transport can send. Providers with OpenClaw-managed OAuth and **no** registered
harness — xAI/Grok, Qwen — do run on the built-in runtime, and there the sandbox applies.

**A billing premise underneath NVB-13 is out of date.** OpenClaw's docs cite Anthropic's
**June 15 2026** support update: Claude Agent SDK, `claude -p`, and third-party app usage
draw from the signed-in subscription's usage limits. ADR 0011 argues partly from Feb 2026
terms prohibiting exactly this, server-enforced since April 2026. Re-check against
Anthropic's own support articles before either premise is cited again.

**Consequence for NVB-14: the container that delivers per-principal isolation has to wrap
the gateway, not tool execution.** Which revives one specific conflict —
`docs/providers/anthropic.md` warns that Claude CLI reuse expects OpenClaw on the same
host as the Claude login, that Docker installs can persist a container home and log in
inside it, but that Podman installs "do not mount host `~/.claude`; use an Anthropic API
key there." The rootless-Podman lean was chosen to avoid rewriting Waydroid's iptables.
**Subscription auth pulls toward Docker and Waydroid pulls toward rootless Podman**, and
that is the first thing NVB-14 has to settle on hardware.

### ✅ Per-agent tool separation is real over the MCP bridge — and it costs the agent its memory (2026-08-13)

The finding above says the sandbox does not reach the CLI. It leaves open the question
NVB-14 actually cares about: can two agents on one gateway hold **different** tool sets,
and is the difference real or cosmetic? Read out of the same shipped `dist/`.

| Evidence | What it shows |
| --- | --- |
| `mcp-http-BUQahgob.js:648` | `resolveMcpLoopbackScopedTools` → `resolveGatewayScopedTools({ surface: "loopback", excludeToolNames: NATIVE_TOOL_EXCLUDE })`, which resolves `resolveEffectiveToolPolicy({ sessionKey })`. |
| `mcp-http-BUQahgob.js:814,851` | One `scopedTools` object serves **both** `tools/list` and `tools/call` for a request. |
| `mcp-http-BUQahgob.js:639` | `NATIVE_TOOL_EXCLUDE = { read, write, edit, apply_patch, exec, process }`. |
| `cli-backend-C4iY7FFY.js:21` | `claude-cli` declares `nativeToolMode: "always-on"` — hence the exclusion. |
| `prepare.runtime-CM3Uj6Uh.js:432` | `hasBootstrapFileAccess = nativeToolMode === "always-on" && disableTools !== true`. |

**Tool separation is existence separation, not credential denial.** Because the same
scoped list answers `tools/list`, a tool an agent does not hold is *absent from its tool
list* — the model is never told it has a capability it cannot use. That is the property
`src/agent/registry.py` was written against, and NVB-17/18's calendar and mail tools
inherit it for free by arriving as OpenClaw plugin tools.

**The `cliBackends` args being global is not the limit it looked like.** They govern
Claude's *own* native tools; `--allowedTools mcp__openclaw__*` blanket-approves the bridge
**namespace**, and which bridge tools exist inside it is decided per agent. Global floors
the native surface equally; per-agent policy shapes the bridge surface individually.

**But the bridge withholds exactly the six sandboxed tools.** So under `claude-cli` the
sandbox is not merely uncovered — it is *purposeless*, since the only tools it protects
are the six never offered. And memory is plain Markdown in the agent workspace
(`MEMORY.md`, `memory/YYYY-MM-DD.md`) written with an ordinary file-write tool: with the
native surface floored to `TodoWrite` and the bridge withholding `write`, **no writer
exists**. The agent cannot remember anything.

The obvious fix is a trap. Allowing `Write`/`Edit` natively grants **absolute-path**
access as the shared `openclaw` uid, so every agent can reach every other agent's store.
**On one gateway with `claude-cli`, memory writes and agent isolation are mutually
exclusive.**

**The one escape hatch does not exist.** `agents.defaults.compaction.memoryFlush` reads
like a gateway-side writer, but it is not, and it fails twice over:

- `config-agents.md:667` calls it a "silent **agentic turn** before auto-compaction to
  store durable memories" — it *prompts the model* to write, so it needs the same write
  tool the agent does not have.
- `cli-compaction-CF7Yb1P6.js:321` — when the backend declares `ownsNativeCompaction`
  (which `claude-cli` does), OpenClaw returns early: "owns native compaction — deferring
  to backend". The whole compaction flow the flush lives in never runs for our sessions.

**So one gateway on `claude-cli` cannot give an agent durable memory and keep the agents
isolated from each other.** Pick one of: an API-key or OpenClaw-managed-OAuth provider on
the built-in runtime, where `read`/`write` become real per-agent, sandboxed tools; or a
container per gateway, where native file access is confined by the container instead of
by tool policy. This is the finding that decides NVB-14.

**NVB-17's tools will not be sandboxed on any runtime.** `docs/gateway/sandboxing` lists
the sandboxed set as `exec`, `read`, `write`, `edit`, `apply_patch`, `process` plus the
optional browser — the filesystem/process family — and nothing could extend it:
`resolveSandboxToolPolicyForAgent` is an allow/deny **policy** layer
(`agent-tools.policy:101`), not a routing table, and each fs/exec tool dispatches to the
backend itself. A calendar or mail tool making an outbound HTTPS call has no sandbox path.

So the credentials that matter most sit in the gateway process whichever runtime we pick,
and only a container **per gateway** separates them. That splits NVB-14 cleanly:
**switching runtimes bounds what a compromised agent can reach; containerizing bounds what
a compromised gateway can reach.** AGENTS.md's standing question — "what does a successful
injection do with this" — is about the first. The second is the rarer failure, so it is an
upgrade with a trigger (ADR 0009's shape), not a prerequisite.

### Cross-agent requests go agent-to-agent, and the sandbox silently clamps them (2026-08-14)

> **Scope: deferred, not planned.** Cross-agent communication is **not** being built now
> and is not on any milestone. The next four sections are the investigation behind that
> deferral — they exist so the question is not re-litigated from scratch, and so the
> settings that would have to change are already known. Nothing here is pending work.
> `tools.agentToAgent.enabled` stays `false`, which is the shipped default.

The question: when one principal's agent needs something only another principal can
consent to — scheduling between two people, not on the family calendar — does the first
agent hold the second's credentials with an "ask" gate, or do the two agents talk?

**It has to be the two agents talking, and OpenClaw supports that natively.**
`sessions_send` (`docs/concepts/session-tool`) delivers to another session, either
fire-and-forget (`timeoutSeconds: 0`) or waiting inline for the reply, with a bounded
reply-back loop (`session.agentToAgent.maxPingPongTurns`, default 5, `0` disables) that
the target can end early with `REPLY_SKIP`. It is gated twice, both closed by default:
`tools.agentToAgent.enabled` plus an `allow` list of agent ids, and
`tools.sessions.visibility` (default `tree` — only sessions this one spawned).

**The impersonation boundary is already drawn.** Inbound cross-agent messages are marked
`[Inter-session message … isUser=false]` in the receiving prompt and in transcript
provenance, and the receiver is instructed to treat them as tool-routed data rather than
end-user instruction. One agent cannot speak as its principal to another agent.

⚠️ **The sandbox clamps this off.** When the calling session is sandboxed and
`agents.defaults.sandbox.sessionToolsVisibility` is `"spawned"` (the default), visibility
is forced to `tree` **even if `tools.sessions.visibility: "all"` is set.** Since
sandboxing is the whole reason for the runtime switch, this fires exactly when the config
looks finished.

**The credential-holding route is not available and should not be built.** Approvals
broadcast `exec.approval.requested` to **operator clients** — Control UI, macOS app, nodes
(`docs/tools/exec-approvals`, "Approval flow"). The operator is the gateway owner, so an
approval cannot be routed to the family member an action actually affects; and the ask
machinery (`tools.exec.ask`) covers `exec` only, never a calendar or mail tool. Holding
another principal's credential behind an "ask them" gate would also put that credential in
the wrong agent's process — the thing the runtime switch exists to prevent.

**So a confirmation gate on a plugin tool is ours to build: that is NVB-16's real
justification,** not a nice-to-have registry.

### What `tree` visibility means, and two ways `agentToAgent` fails open (2026-08-14)

Read out of `dist/session-visibility-CUl4zBv3.js`, which holds the whole model in 216
lines.

`tree` is the requester's own session plus sessions **it spawned** — `rowOwnedByRequester`
matches `ownerSessionKey`, `spawnedBy`, or `parentSessionKey` (line 160). A family
member's session is created when they message Signal, so it is never in anyone else's
tree. Cross-agent targeting then requires `visibility === "all"` **exactly** (line 173);
`agent` and `tree` both return `forbidden`. Combined with the sandbox clamp, the practical
rule is:

> With `sandbox.mode: "all"` and defaults, cross-agent messaging cannot work at all.
> `agents.defaults.sandbox.sessionToolsVisibility: "all"` is required to lift it
> (`"spawned"` and `"all"` are the only values).

Two failure-open behaviours to configure around:

- **An empty `allow` list allows every pair.** `matchesAllow` returns `true` when
  `allowPatterns.length === 0` (line 91), so `agentToAgent: { enabled: true }` without
  `allow` is fully open. The list is not optional for us.
- **`allow` cannot express a one-way relationship.** `isAllowed` requires *both* requester
  and target to match (line 103), so `allow: ["owner", "family"]` permits `family → owner`
  exactly as much as `owner → family`. A directed relationship has to be enforced above
  this, not by config.

### Ask-first tools exist, and plugin approvals route differently from exec approvals (2026-08-14)

**This corrects the previous section's claim that approvals only reach the operator.** That
holds for **exec** approvals (`exec.approval.requested` → operator clients). **Plugin**
approvals are a separate family with independent config, and they can be delivered to a
chosen person.

`docs/plugins/plugin-permission-requests`: a plugin registers `api.on("before_tool_call",
…)` and returns `requireApproval` with `title`, `description`, `severity`,
`allowedDecisions` (`allow-once` / `allow-always` / `deny`), `timeoutMs`,
`timeoutBehavior`, and `onResolution`. Returning nothing lets the call through, so
free-use and gated tools coexist under one hook, decided per call. It fails closed:
timeout denies unless `timeoutBehavior: "allow"`, and **"No approval route → the call is
blocked."**

Routing is `approvals.plugin` — `enabled`, `mode` (`session` | `targets` | `both`),
`agentFilter`, `sessionFilter`, `targets: [{ channel, to }]` — independent of
`approvals.exec`. `session` delivers into the originating chat; `targets` to explicit
addresses.

The doc also names the layering this repo already assumes: *"Optional tools are a
discovery-time gate. Plugin permission requests are a per-call gate. Use both."* That is
`src/agent/registry.py` plus NVB-16, and NVB-16 is now a **`before_tool_call` plugin**
rather than anything bespoke.

⚠️ **Delivery is not authorization.** An approver must already be command-authorized in
that session, and Signal reaction approvals additionally require explicit approvers from
`channels.signal.allowFrom` or `defaultTo`. Good: nobody in a group can approve for
someone else. Also required: whoever must approve has to be listed, or the call blocks.

**Gate the cross-agent request on the asking side, not the answering side.** The hook event
carries `ctx.sessionKey`, `ctx.agentId`, `toolName`, `params` — session identity, not turn
provenance. Because `sessions_send` delivers into the target's *existing* session, a hook
there cannot distinguish an agent-initiated turn from one the person typed; the
`[Inter-session message … isUser=false]` marking lives in the prompt and transcript, not in
the hook event. So the cross-agent request should itself be a registry tool (say
`family.ask_mom`) whose `before_tool_call` requires approval routed to that person via
`approvals.plugin.targets`. The context is unambiguous there, the prompt can name the
actual question, and the asking agent gains nothing by asking.

### There is no per-session-origin tool policy — use a liaison agent instead (2026-08-14)

The obvious design for cross-agent requests is: agent-initiated work always opens a **new**
session, that session is distinguishable, and it carries a narrower tool policy. Two of
those three are unavailable out of the box.

- **A2A cannot open a new session on the target.** `sessions_send` targets an *existing*
  session key. `sessions_spawn` does create one, but as a child of the **calling** agent —
  same `agentId`, same credentials, same workspace. It is an agent talking to itself.
- **There is no per-session-origin policy axis.** The whole `tools.*` surface is
  `allow`/`deny`, `byProvider`, `toolsBySender`, `elevated`, `exec`, `loopDetection`,
  `web`, `media`, `agentToAgent`, `sessions`, `sessions_spawn`, `codeMode`,
  `experimental`. None of them key on how a session was initiated.

The machinery half-exists: `isSubagentEnvelopeSession`, inherited tool allow/denylists, and
subagent role/control scope narrow a child session's policy, and provenance is in the data
model (`spawnedBy` / `parentSessionKey` — `resolveGroupToolPolicy` already consumes
`spawnedBy`). But it is scoped to subagent session keys under one agent, and nothing
attaches it to cross-agent messaging.

**So express the boundary as agent identity, which is the axis OpenClaw does enforce.**
Give each reachable person a second, narrow agent — a **liaison** — whose only job is
answering other family members, and point cross-agent traffic at it rather than at their
main agent:

- own sessions by construction, so no collision with that person's own conversation;
- distinguishable in every policy call and every hook, because it is a different `agentId`;
- unavailable tools are *absent*, via the per-agent existence separation already verified;
- ask-first on whatever remains, via the `before_tool_call` plugin with
  `approvals.plugin.agentFilter: ["<liaison>"]` and a Signal target for that person.

It also defuses the symmetric-`allow` problem above: a liaison holding nothing sensitive
makes the unavoidable reverse direction harmless, which allowlisting a main agent would
not. Cost: one extra agent id per reachable person, and the liaison does not share memory
with their main agent — correct for "when is she free Thursday", wrong for anything that
needs their history, which is the signal that such a request does not belong on this path.

### Sharing files with a liaison agent: nest the workspace, never symlink (2026-08-14)

A liaison agent that shares nothing is a stranger; the question is how it can share some
facts with its principal's main agent without seeing everything.

**Symlinks cannot do it.** Two independent rejections: sandbox seed copies "only accept
regular in-workspace files; symlink/hardlink aliases that resolve outside the source
workspace are ignored" (`agent-workspace.md:45`), and bind validation re-resolves a source
through its deepest existing ancestor before re-checking allowed roots, so symlink-parent
escapes fail closed (`gateway/sandboxing`). This is precisely the escape the validator
exists to stop.

**Binding one agent's workspace into another's container needs a dangerous flag.**
`docker-Hq4HIYYD.js:993` scopes the allowed bind roots to the agent's *own* workspace:
`bindSourceRoots: [workspaceDir, params.agentWorkspaceDir]`. Anything else is "outside
allowed roots" unless `dangerouslyAllowExternalBindSources: true`. We should not set it.

**Nesting the workspaces avoids binds entirely,** because each agent then only ever mounts
its own workspace:

```json5
{ id: "owner",         workspace: "~/.openclaw/workspaces/owner" },
{ id: "owner-liaison", workspace: "~/.openclaw/workspaces/owner/shared" }
```

The main agent writes into `shared/` with ordinary file tools; the liaison's root *is*
that directory, so it cannot see above it. No binds, no flags, no symlinks.

⚠️ **The shared directory is the liaison's bootstrap surface.** `MEMORY.md`, `AGENTS.md`,
and `memory/*.md` load from the workspace root, so whatever the main agent writes there
becomes the liaison's own instructions — a prompt-injection path from one agent into the
agent another person's approvals hang off. Blast radius is small because a liaison holds
nothing sensitive, but the rule that follows is not optional: **the liaison's identity must
come from config, never from the shared directory** — the `identity` block, plus
`agents.defaults.skipBootstrap: true` so OpenClaw does not seed instruction files into a
directory another agent can write. `shared/` then carries facts; who the liaison *is* stays
in config only the operator edits.

Note the sharing is conventional, not enforced: the liaison has `rw` on that directory
because it is its own workspace. Making it read-only means `workspaceAccess: "ro"`, which
removes the liaison's ability to write memory at all — the capability the runtime switch
exists to restore. Not a good trade.

**For identity alone, none of this is needed.** `identity` is config, not a file. Two
agents carrying the same identity block are the same persona to the family with no
filesystem sharing whatsoever. Nesting earns its keep only for accumulated facts an agent
chooses to publish.

**Decision: do not nest. Credentials are not in the workspace, so the mount buys nothing.**
`agent-workspace.md:109` lists what lives outside it — `auth-profiles.json` (model OAuth
and API keys), `~/.openclaw/credentials/`, and `agents/<id>/sessions/`. The sandbox mounts
the *workspace*; the credential store sits above it, which is exactly why the runtime
switch makes it kernel-unreachable. Putting a credential in a shared workspace folder would
move it from somewhere the agent's file tools cannot reach to somewhere they can —
`gateway/secrets` twice: "plaintext credentials remain agent-readable if they sit in files
the agent can inspect", and a plaintext credential in an agent-readable path "is still
readable via file or shell tools, bypassing API-level redaction". The liaison gets its own
workspace and shares the `identity` config block; nothing is mounted.

### Rotation lives in the auth store, and OAuth profiles cannot use SecretRefs (2026-08-14)

Avoiding duplicate credentials is a real concern, just not a workspace one. OpenClaw's
mechanism is **SecretRefs**: credentials referenced from an external source instead of
stored inline, resolved eagerly into an in-memory snapshot, failing fast at startup when
unresolvable and swapping atomically on reload with last-known-good retained. Rotate at
the source and every referencing agent follows.

⛔ **It does not cover our model credential.** `gateway/secrets`: "Policy violations (for
example an OAuth-mode auth profile combined with SecretRef input) fail activation before
the runtime swap." Grok-via-OAuth is an OAuth-mode profile, so it stays in each agent's own
auth store.

Combined with two-tier read-through (local agentDir → default agent store) and the **"main
holds nothing"** invariant that deliberately empties the fallback, **every agent needs its
own xAI OAuth login.** Whether one subscription tolerates several device-code logins, and
whether they rotate independently, is unknown and cheap to test on switch day.

If it does not, the narrow fix is to let **main hold the model credential only.** That
invariant exists to stop *tool* credentials reaching the wrong principal; the model
credential is not a differentiator — every agent needs inference, and a shared subscription
under a provider-side cap is already accepted (Q4). Tool credentials stay out of main,
which is the part that was ever load-bearing. NVB-17/18 tool credentials are unaffected
either way: `src/agent/registry.py` binds them to systemd `LoadCredential` ids, external to
OpenClaw, one file on the host granted per tool.

### The runtime switch is also a media-tool decision, and Cursor is not an option (2026-08-14)

`docs/providers/xai`: one credential from `openclaw models auth login --provider xai
--method oauth` powers `web_search` (provider id `grok`), `x_search`, `code_execution`,
speech/transcription, **and** image/video generation — the bundled `xai` plugin registers
the shared `image_generate` and `video_generate` tools. So the skeptical-family-member
profile gets real web search from the same subscription, with no separate search key, and
media generation stays per-agent policy-governed like any other tool.

**Cursor cannot serve as the subscription.** There is no Cursor model provider; `cursor`
appears only as an ACP backend (`cursor-agent acp`, `docs/tools/acp-agents:100`) — an
external harness in the same category as `claude-cli`, a host subprocess carrying its own
native tools. Adopting it reinstates both findings above.

### Group sessions are shared, and a repeat question returns `NO_REPLY`

The group has exactly one session key —
`agent:family:signal:group:Mt2z3ANj…` — with no sender component, despite
`session.scope: "per-sender"`. That setting appears to govern DMs, not group rooms.

This does not violate ADR 0010, which explicitly permits sharing inside one room
("everyone reads everyone's messages"), but it has a practical consequence worth
recording: the second sender's turn **resumed** the first sender's session
(`useResume=true reuse=reusable historyPrompt=present`), saw the identical question
already answered, and emitted the literal token `NO_REPLY` — 8 bytes, nothing
delivered. Not an error, and no log line says "suppressed".

So a shared room means one sender's turn can be shaped by another's, and identical
messages get silently dropped. Test with distinct prompts, and expect this in real
family use.

### A — gate in front, gate owns the socket

signal-cli → gate → OpenClaw. The gate stays the only JSON-RPC client and hands
accepted commands to OpenClaw over some other inbound surface.

**Rejected.** OpenClaw's Signal channel goes unused, and with it the reaction-based
approvals, `toolsBySender` and the per-channel allowlists — which is the entire reason
ADR 0011 chose it. It also keeps every line the ADR set out to delete.

### B — gate beside, advisory

OpenClaw owns the channel. The gate holds its own JSON-RPC connection, polls
`listGroups`, and on drift sends a notice.

Cheapest, and honest about being a smoke alarm rather than a lock. But the failure
mode ADR 0008 named — *never* keep working with an extra person present — becomes a
message the owner may not read for a day. Acceptable only as a first step.

### C — gate beside, enforcing through config (leaning)

As B, plus: on drift the gate removes the group from `channels.signal.groupAllowFrom`
(or sets `groupPolicy: "disabled"`) via `config.patch`, and restores it when the live
member set matches the pinned one again.

Real enforcement, and OpenClaw keeps the channel. Open sub-questions:

- **The race.** Drift is caught at the poll interval plus reload latency, not
  instantly. The gate's current design shrinks that by re-reading membership when a
  non-`DELIVER` group envelope arrives — but under C the gate no longer sees the
  receive stream. Either it keeps a second read-only connection for that signal, or
  the window is `MEMBERS_REFRESH_SECONDS` wide.
- **Who owns the file.** With `configWrites: false` the channel cannot write it, but
  the gate now can, and a human editing by hand will collide. Generate
  `openclaw.json` from `config.toml` and let nothing else write it.
- **Does `config.patch` survive a hot reload mid-turn**, and does revoking
  `groupAllowFrom` stop an in-flight turn or only the next one? Unverified.

### E — a transparent proxy in front of signal-cli (**chosen, pending tests**)

OpenClaw's Signal channel speaks to the same signal-cli we already run. So put a small
stdlib process between them: signal-cli on loopback, the proxy in front of it, and
`channels.signal.transport.httpPort` pointed at the proxy. OpenClaw sees an ordinary
daemon and does not know the proxy exists.

It supersedes C rather than competing with it, because it dissolves three problems C
solved three different ways:

- **Drift is caught in seconds.** Group-update envelopes come past on the wire, so
  `_is_group_update()`'s fast path returns. No poll interval to tune, no 15-minute
  window.
- **Enforcement is inline.** A refusing group's envelopes are dropped before OpenClaw
  sees them. No config rewriting, no hot-reload race, no boot-ordering window, and no
  "who owns the file" question — fail-closed is structural rather than arranged. The
  affirmer/revoker design C needed becomes unnecessary.
- **The per-(group, sender) hole closes.** `groupAllowFrom` is channel-wide and Signal
  has no per-group sender allowlist, so config alone cannot say "mom commands in
  `family` but not in `confirmations`". The proxy applies the conversation table per
  envelope, which is what `decide()` already does.
- **The outbound allowlist survives without denying `message`.** Every `send` passes
  through, so a call naming a conversation outside the table is refused at the wire.
  That demotes NVB-20 Test 1 from architectural to informational.

**What it costs, stated plainly:**

- **It is a protocol-faithful MITM, and that is the real risk.** A relay that mangles a
  field or mistimes SSE framing produces failures that look like OpenClaw bugs, debugged
  against our own relay. It stays dumb on purpose: relay bytes, inspect a copy, decide
  on envelope metadata, never rewrite a payload — only drop a whole envelope.
- **Single point of failure.** Proxy down, Signal down. That is the correct direction
  for this design and the same posture as today, but it wants `Restart=always` and code
  too boring to crash.
- **Outbound attribution is probably lost.** The `send` RPC names a destination, not an
  agent, so enforcement is coarse — *these conversations may be addressed at all* —
  rather than ADR 0009's per-profile send list. The guarantee that matters (no
  identifier outside the table is reachable) holds; the granularity does not.

**It may also not be optional.** C assumed the gate could hold a second read-only
connection to watch envelopes. If signal-cli's event stream is single-consumer,
OpenClaw takes it and a bystander sees nothing — in which case a proxy is the only way
to observe envelopes at all, and C degrades to blind polling. NVB-20 Test 2 settles it.

### D — gate as an OpenClaw plugin

**Rejected.** It puts the one component this design needs to trust inside the process
it is meant to be independent of. Hermes' own security documentation states the
general case: *"Nothing inside the agent process constitutes containment."*

### The unresolved piece under any option

**The outbound allowlist (ADR 0011 item 2) has no mechanism yet.** The plan is to deny
the `message` tool, but the docs describe it as "Send replies or channel actions" — if
ordinary replies route through it, denying it makes the assistant mute. Test this
before anything else; the answer decides whether ADR 0009's outbox survives in any
form or is simply given up.
