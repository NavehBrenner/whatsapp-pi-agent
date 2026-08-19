# Changelog

Notable changes to this project, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow
[SemVer](https://semver.org/) once there's something to version.

This project is pre-release, so `Unreleased` is where everything lives for now.

Findings verified on hardware are recorded here as well as in the ADRs, because
several of them are the kind of thing that costs an evening to rediscover.

## [Unreleased]

### Added — ask-first approvals, and the first OpenClaw plugin this repo owns (2026-08-19)

`deploy/openclaw-plugins/wpa-approve/` — a `before_tool_call` hook that stops a named
tool until a person answers in Signal. NVB-16, and the prerequisite for NVB-37's deploy
endpoint. It **ships gating nothing**: the mechanism was proven against `web_search` as
a throwaway probe and that entry was removed, because an approval prompt on every search
is a tax nobody agreed to pay and a gate that cries wolf gets tapped through.

Everything below was read out of the shipped `dist` or measured on the box, not taken
from the docs — the NVB-32 habit, and it paid again.

**Most of NVB-16's specification was already satisfied by core.** Targeting, expiry, the
emoji vocabulary, the fail-closed default and the prompt text are all upstream. What
remained ours is policy: which tools ask, what the prompt says, which decisions are
offered. Verified end to end on hardware: 👍 approves, 👎 refuses, a second reaction on a
resolved prompt does nothing, and an unanswered prompt expires.

Reaction bindings are fixed in core and are **not** configurable — 👍 `allow-once`,
♾️ `allow-always`, 👎 `deny`, with skin-tone and variation selectors stripped before
matching. Better than the issue asked for: a closed three-emoji vocabulary rather than
any-emoji-approves. The emoji offered follow `allowedDecisions`, so withholding
`allow-always` withholds ♾️ as well — one setting, both surfaces.

**Four numbers that constrain anything built on this:** `title` caps at 80 characters,
`description` at **512**, the timeout defaults to 120s and is clamped to **600s**. The
512 cap is the one with teeth — it rules out putting a full config diff in a deploy
prompt, which NVB-37 had assumed it could.

**`allow-always` is in the DEFAULT decision set.** Omitting `allowedDecisions` silently
offers a standing grant. Every gated tool must name its decisions, and for anything
privileged that list is `["allow-once", "deny"]`.

**A refusal with no approval route is not fast, and that is the trap.** "No approval
route → the call is blocked" is true in outcome only: the turn waits out the entire
timeout, Signal shows a typing indicator throughout, and the health monitor logs
`stalled_agent_run` every 30s with `recovery=none`. The plugin now checks the route
itself and blocks immediately, failing closed. `route.test.mjs` covers it, run by
`install.sh` because CI is mypy and pytest and cannot see a JavaScript plugin.

That check exists because of a real regression caught before it bit anyone: `web_search`
is in the global `tools.alsoAllow`, so **every** agent holds it, while
`approvals.plugin.agentFilter` named only `owner`. Gating the tool therefore armed a
ten-minute hang for every other person on the box. **`agentFilter` names who can be
asked; the plugin names what gets gated, and nothing upstream checks that the two
agree.**

**A denied tool does not necessarily produce a visible failure.** Blocked mid-turn, the
model answered the question from its own knowledge — confidently, correctly, with
nothing marking that a tool call had been refused. For NVB-37 this is the load-bearing
consequence: whether a deploy happened must be read from the host, never from the
agent's account of it.

**The prompt's layout cannot be changed by a plugin.** An approval is delivered by the
approval forwarder — `handlePluginApprovalRequested` → `deliverToTargets` →
`sendDurableMessageBatch` — a path that never emits `message_sending`; that hook belongs
to the reply/dispatch pipeline. The payload is built by the *channel's* approval renderer
(`resolveChannelApprovalAdapter`). So `Tool:`, `Plugin:`, `Agent:`, `ID:`, `Expires in:`,
`Reply with:` and the "Allow Always is unavailable" footnote are Signal's and core's, and
a `message_sending` rewriter loads cleanly, reports no error and never runs. One was
written, deployed and believed for an hour on exactly that evidence. What we control is
`title`, `description`, `severity` and `allowedDecisions` — nothing else.

The footnote is the price of the safety property, not a defect: it appears *because*
`allow-always` is withheld.

**`onResolution` receives a bare string**, not a resolution object — `"allow-once" |
"allow-always" | "deny" | "timeout" | "cancelled"`. Reading `.decision` off it yields
`undefined`; an earlier version defaulted that to `"timed-out"` and logged two successful
human decisions, an approve and a deny, as timeouts. For a privileged tool that line is
the audit trail, so a wrong default there is worse than no line at all.

**OpenClaw refuses a plugin directory owned by a foreign uid** — "blocked plugin
candidate: suspicious ownership" — which reads exactly like an allowlist problem and is
not one. `install.sh` installs plugins root-owned for that reason.

### Added — `aviv`, the fourth 1:1 agent, live on the Pi (2026-08-19)

The last of the three family agents added in config on 2026-08-14 reaches the box. It
took two attempts, and the second one is the entry worth reading: **the first deploy was
correct by every check this repo had, and the agent was unreachable for twenty minutes
with nothing logged anywhere.**

Live config gains, for one person: two `channels.signal.allowFrom` entries and two
`bindings` — his ACI `uuid:eab7ef34-…` and his number `+972559621616` — plus one
`agents.list` entry and `workspace-aviv` with a hand-written `IDENTITY.md`/`USER.md`.
`openclaw config validate` clean, gateway `NRestarts=0`, `check-agent-auth.sh` green,
own sandbox container, and `[signal] delivered reply to +972559621616` on a real message
from his phone.

Deliberately **not** done, each an independent grant: no `groupAllowFrom` (channel-wide,
so it would admit him to *every* allowlisted group), no gate config change (family DMs
never pass through `wpa-gate`), no `tools` block and no `dms` entry.

#### The channel prefers the phone number over the ACI, and refuses in silence

This project keys senders on the uuid everywhere, for reasons that are written down and
still correct: Signal does not share numbers by default, so `sourceNumber` is null on
real traffic and a number-keyed row matches nothing. **OpenClaw's Signal plugin does the
opposite**, and the two configs on this box now disagree about what a sender *is*:

```js
resolveSignalSender:    sourceNumber checked FIRST -> {kind:"phone"}, else sourceUuid
isSignalSenderAllowed:  phone<->phone, uuid<->uuid; crossing kinds returns false
resolveSignalPeerId:    the e164 for a phone sender -> the BINDING misses too
```

So a sender who shares their phone number is refused by a uuid-only allowlist, and had
the allowlist been fixed alone the binding would still have missed and dropped him on
the default agent — answered, by an agent that is not his, out of a workspace that is
not his. That is the worse of the two failures and it is the one that looks like
success.

**Nothing logged it.** Not journald, not the gateway's own `/tmp/openclaw/*.log`, not
`openclaw status`. The gate logged `dropped: sender`, which is the gate correctly
refusing a conversation it was never given — true, expected, and a red herring.

What actually found it: **reading the shipped plugin**. Three rounds of correlation had
produced a plausible wrong answer (`groupAllowFrom` — everyone who worked was in both
lists, and the one person who wasn't, failed) from three samples. This is the second
time in three days that reading `dist/` beat instrumenting it; NVB-32 said so at the
time and it is now a habit worth having rather than a note.

The evidence that made it certain came from a structural-only envelope tap — sender id,
message kind, body *length*, never a body:

```
sourceUuid: eab7ef34-…  kinds:[dataMessage]  body_len:16  sourceNumber_present: true   <- refused
sourceUuid: b0c72586-…  kinds:[dataMessage]  body_len:5   sourceNumber_present: false  <- answered
```

**Which form arrives is the sender's privacy setting, not ours**, so neither key is
durable on its own and both go in. *Unresolved:* why his envelopes carry a number and
the owner's do not, on one account minutes apart. It may be his Signal "who can see my
number", or it may be that our own `getUserStatus` lookup wrote his number into the
daemon's recipient store — his row had none before that call and had one after. An
earlier draft of this entry asserted the NULL-number behaviour as a finding; it is a
hypothesis, and the honest version is that we do not know which.

Recorded as an invariant in [`AGENTS.md`](AGENTS.md) and in the example config, because
a config that is correct under one of the two rules and silently dead under the other is
exactly the shape of mistake this repo keeps paying for.

#### `aryeh` has never been verified on the wire

Found while debugging: `aryeh` has only ever held an `agent:aryeh:main` session — a CLI
probe — and never a `signal:direct` one, in the three days since he was recorded as live.
He may have the identical defect. **A CLI probe is not the channel path**: it creates
`agent:<id>:main`, exercises the agent and not the binding, and passes while the thing a
user would do fails. The same lesson as the blue PNG read as "green" (2026-08-15), now
with a second victim. One test message settles it and it has not been sent yet.

#### The gateway rewrites `openclaw.json` underneath you

`configWrites: false` does not cover this — that setting stops a *channel* writing
config, not the gateway's own bookkeeping. It stamps `meta.lastTouchedAt` and
re-serialises to its own conventions (literal UTF-8, trailing newline), which is not
Python's `json.dumps(..., indent=2)` default. The second edit's guard caught exactly
this and refused rather than reformatting the file under a running process:

```python
ser = lambda c: json.dumps(c, indent=2, ensure_ascii=False) + "\n"
assert ser(json.loads(raw)) == raw, "formatting mismatch, aborting"
```

Worth keeping on every scripted edit of that file. The failure it prevents is not a
broken config — it is a 400-line diff that hides the three lines you meant to change.

Also observed, and the reason the restart was not skipped: writing the file at all
triggers a hot reload, which logged `bindings` in "config change detected" and **not** in
"config hot reload applied". Whether that is terser logging or a binding that did not
take, an unbound DM falls through to the default agent in silence, so the gateway was
restarted deliberately.

### Added — `deploy/install.sh`, one command for the whole box (2026-08-18)

Deploying had grown to seven artifacts across two locations and no single command that
put them there. The gap showed up twice in two days: the credential checker sat in the
repo for a day before reaching the box, and `wpa-gh-watch` ran a version two commits
behind while we debugged the alarms it was sending.

```bash
cd /opt/wpa && sudo git pull && sudo deploy/install.sh
```

Installs six helpers into `/usr/local/bin` from a table in the script (`wpa-agent-auth`,
`wpa-gh-watch`, `wpa-oc-auth`, `wpa-project-sync`, `wpa-signal-backup`, and
`wpa-outbox-notify` at `0700` because it writes into an outbox owned by the gate),
delegates the tree sync and reader units to `install-reader.sh`, enables all seven
timers, and runs both test suites plus the live isolation check. Idempotent.

Three decisions worth stating:

- **It never restarts a long-running service.** Installing a unit file does not change
  the process already running from the old one, so it diffs the unit files before and
  after and *names* what needs restarting. Restarting the gateway interrupts every agent
  mid-conversation; that is not a deploy script's call. Verified by drifting
  `wpa-gate.service` deliberately and watching it get named — then restored, because the
  install rewrites it from the repo.
- **It does not deploy the gateway config**, and says so. `openclaw.json` lives outside
  the repo and outside git; `config/openclaw.example.json5` documents it rather than
  driving it. A script that claimed to "apply all config" while silently skipping the
  file where agents and tool policy actually live would be worse than no script.
- **`install-reader.sh` now skips the tree sync when it is already running from
  `/opt/wpa`.** In-place is the normal case now, and rsyncing a directory onto itself
  with `--delete` is the exact shape of the command that emptied `/opt/wpa` on
  2026-08-17. Not worth finding out whether it is safe.

Run end to end on the Pi before merging: 10/10 tests, seven timers enabled, isolation
check green.

### Fixed — a busy watcher paged the owner about a watcher that was fine (2026-08-18)

`wpa-gh-watch` fired `OnFailure` twice today. Nothing was wrong with it, and nothing
was wrong with GitHub either — this one was ours.

The chain, from the journal:

1. A room turn was still running when the next tick fired. Turns take minutes; the
   timer fires every 60s.
2. The second tick waited on the gateway and hit the transport timeout at **150s**.
3. `openclaw agent` then fell back, unasked, to an **embedded agent with a fresh
   session** (`gateway-fallback-<uuid>`).
4. That embedded agent runs outside the gateway's environment, so it has no
   `DOCKER_HOST`, could not start a sandbox, and exited 1 — `Cannot connect to the
   Docker daemon at unix:///var/run/docker.sock`.

Step 4 was predicted verbatim by NVB-25's drop-in on the gateway unit: *"Without
DOCKER_HOST the CLI would look for /var/run/docker.sock, which this user can no longer
reach — by design."* The watcher unit never got the matching drop-in.

**The fix is a `flock`, not a `DOCKER_HOST`.** Giving the watcher the socket would make
the fallback *work*, and an amnesiac agent answering in the room about a PR it has no
memory of is worse than a tick that skipped — the wake uses `--session-key` precisely
so the room keeps its memory and its conversation. `openclaw agent` has no flag to
refuse the fallback, so the fix is to never be late enough to trigger it: one wake at a
time, and a tick that finds the lock held exits 0.

Skipping costs nothing now that state is committed only after a wake lands (same
release), so the next tick re-reports whatever the skipped one found. The test suite
covers it: a concurrent tick skips and commits nothing.

**The shape worth keeping:** three alarms in two days, none of them the failure the
alarm describes. Two were GitHub 5xx, one was our own timer racing itself. A monitor
whose false-positive rate is this high stops being read, which is the failure it was
built to prevent.
### Added — `web_search` for the project agent (2026-08-18)

Granted to `code-invariants`, which had every other tool it needed and no way to look
anything up.

**It took two edits, not one.** The global `tools.alsoAllow` already carried
`web_search`, but an agent-level `alsoAllow` **replaces** the global list rather than
merging — the fault runbook 06 already records from when granting the GitHub tools
silently removed this agent's `read`/`write`/`edit` and its ability to write its own
`MEMORY.md`. So the agent list needed it, and the room's `tools.allow` ceiling needed it
too, because a ceiling strips whatever it does not name. Adding it in one place
validates cleanly and does nothing. The other four layers already permitted it:
`tools.deny`, `tools.sandbox.tools.allow`, `tools.web.search` (`enabled`, provider
`grok`), and the built-in sandbox deny.

The example config gains both, and the project room's ceiling with it — it had never
been mirrored there, only the family room's, which is why the layer most likely to
strip a tool was the one with no documentation.

**Worth naming, because it is a real widening.** The family room's comment already says
what `web_search` costs: search results are attacker-influenceable text entering an
agent with durable memory. Here it lands in a room that also holds a GitHub PAT with
issue-write. Still egress-only — an injection reads a page it chose, it cannot pick a
recipient — but this is the first path pulling open-web text into a session that can
post. NVB-18's injection smoke test should target it here, not only in the family room.


### Fixed — `wpa-gh-watch` could mark an event reported and then never report it (2026-08-17)

Two ways the watcher could lose an event for good, both found by asking whether it
catches up after the outage below.

**`note()` appended to the `seen` list the moment it found an event**, before the wake
ran. Anything that killed the script in between — a 5xx on a later endpoint, a failed
wake — left the id recorded as reported and never reported. The un-advanced cursor does
not rescue that: the next run re-fetches the event and `seen` drops it. Ids are now held
in memory and committed only once the wake returns 0, along with the cursor.

**The PR head-sha comparison read `repo-sync.json` before and after the sync, in one
run.** The sync rewrites that file mid-script, so a run that died after it had already
written the new sha left the next run's "before" matching — the push then invisible
forever. Comments and CI runs survive that because they re-query with `since=`; a head
sha had nothing to fall back on. The baseline is now the heads this watcher last
*reported*, in `/var/lib/wpa-gh-watch/heads`.

`deploy/gh-watch.test.sh` covers all of it with stubs on `PATH` — a failed wake commits
nothing, the same event is replayed once the wake succeeds, a landed event is not
repeated, and a moved sha reports again. Checked against the previous version of the
script, where it fails, which is the only evidence that a regression test tests
anything. `WPA_SYNC_BIN` exists so the sync can be stubbed; it defaults to the real path.

Worth recording for the next person who wonders why the room was quiet: **the watcher
watches `NavehBrenner/code-invariants`**, not this repo. A day of activity here produces
no wakes there, correctly.

### Fixed — `wpa-gh-watch` paged the owner about GitHub's uptime (2026-08-17)

`api.github.com` served 401, 503 and 504 six times between 16:28 and 18:23, with
successful runs in between. Each one exited non-zero, tripped `OnFailure` and sent a
Signal message saying the project room would not hear about PRs until it was fixed —
about a watcher that was working. The same outage was visible from a laptop at the same
time.

`--retry 3 --retry-delay 5` on the curl options. curl's default retry set is exactly the
transient class (5xx, 408, 429, connection errors), so a genuinely bad token still fails
on the first try and still pages, which is the behaviour worth keeping. **An alarm that
cries wolf at a third party's uptime gets muted, and then the real failure is silent
too** — the same reasoning that reshaped the credential check in this release.

### Added — ADR 0013: tool credentials live in a per-agent MCP server, never in the auth store (2026-08-17, NVB-32)

The answer to "if `main` can't be kept empty, how do NVB-17/18 mount a calendar and a
mailbox safely". **Keep them out of the auth store entirely.** A credential with a
profile id gets mirrored into `main` on its next refresh and inherited by every agent
lacking it; a credential in an MCP server entry's `env` has no profile id, so there is
nothing to mirror. The design is *immune* to the mechanism rather than defended against
it.

One server per principal, credential in that entry's `env`, and the agent's
`tools.alsoAllow` naming only its own server's tools. **This is already the shape the
GitHub PAT ships in** — verified in the live config: `code-invariants` holds
`github__issue_write … github__pull_request_review_write`, no other agent has any
`github__*` tool, `family`/`liron`/`aryeh` carry no `tools` key at all, and `main` is
`deny: ["*"]`.

**ADR 0012 had already written this** — *"let main hold the model credential only… tool
credentials stay out of main, which is the part that was ever load-bearing"* — as a
fallback, then retired it on 2026-08-14 as unnecessary because `main` was empty at the
time. Three rounds of Q7 went into rediscovering a paragraph we had already written. The
mistake was reading "the invariant currently holds" as evidence it held *for the stated
reason*.

Two things the upstream docs settled that the design now depends on:

- **The tool prefix is derived from the server key, not equal to it.** *"Non-`[A-Za-z0-9_-]`
  characters become `-`, names that do not start with a letter get an `mcp-` prefix, and
  long or duplicate prefixes may be truncated or suffixed."* Two similar server names can
  collide and be silently auto-suffixed, renaming the tool an allowlist pins. Server
  names stay short, ASCII, letter-initial and obviously distinct, and the resolved name
  gets read back rather than assumed.
- **`openclaw secrets audit` does not see MCP credentials.** Run live it flags
  `gateway.auth.token` and lists the six auth stores as legacy residue, and says nothing
  about the PAT in `mcp.servers.github.env`. A clean audit is not "no plaintext tool
  credentials".

What this does **not** buy: kernel separation of the credentials from each other. They
sit in `openclaw.json` and the gateway holds them all. What separates `owner`'s calendar
from `family` is a tool allowlist plus the container that stops `family` reading that
file — policy plus containment, not structure. Recorded plainly in the ADR because
ADR 0010's original promise was stronger, and NVB-22 is where it gets revisited.

`deploy/check-agent-auth.sh` now enforces the rule: any profile id outside the
model-provider prefixes fails the check, **even when every agent holds it** and nothing
is inherited — the case the previous rule passed. `MODEL_PROFILE_PREFIXES` is widened
when a model provider is added, never to quiet an alarm about a tool.

It also stops treating an unreadable store as an empty one. Swallowing a failed
`sqlite3` made the whole check a **silent pass**: no ids read anywhere means no strays
and nothing inherited, so it printed OK and exited 0 having seen nothing. It now exits 2
and names the agent.

That was found chasing a failure mode that does not exist. The theory was that a WAL
database cannot be opened read-only once its `-shm` sidecar is gone, so the
`OnBootSec=2min` run would race the gateway's start. **Tested and false:** with `-shm`
and `-wal` removed *and* the directory `chmod a-w`, SQLite reads the stores fine — the
restriction applies only to a non-empty WAL needing replay. The guard is kept because
"reads nothing, reports OK" is wrong whatever the cause, and permissions or a bad disk
need no theory. `After=wpa-openclaw.service` is kept too, on the honest reason:
read-through resolves at gateway startup, so a boot-time check that beats the gateway up
gives a true answer about a moment that does not matter.

### Changed — the default agent cannot be kept empty, so the check now asserts something else (2026-08-17, NVB-32)

`main` was emptied with the gateway stopped and verified empty. It refilled **36 seconds
after the restart**: gateway ready 16:32:51, `main`'s row written 16:33:17, `family`'s
own row written in the same second, `locks/oauth-refresh/` touched at 16:33:17.458. One
token refresh, two writes.

The second write is `mirrorRefreshedCredentialIntoMainStore`, called with
`agentDir: void 0` immediately after a successful refresh is saved to the owning agent's
own store. It is unconditional — it does not care whether `main` held anything before.
Together with the 08-16 finding below that is the whole lifecycle: **the mirror seeds
`main`, and the owner-resolution keeps it fed.**

**It is intended, and the code says so.** The refresh lock's comment: *"prevents the
`refresh_token_reused` storm when N agents share one OAuth profile (see issue #26322)
… peers can adopt the resulting fresh credentials instead of racing against a
single-use refresh token."* `main` is the rendezvous. Emptying it removes the shared
copy until the next refresh restores it.

**So the upstream issue drafted yesterday was withdrawn unfiled.** It would have
reported intended behaviour as a bug. The correction that matters is not about this
codebase: **profile ids are keyed on the account**, so two agents authenticating as the
same account are not separable by this store, and no amount of tidying `main` changes
that.

**ADR 0011's rule — "the default agent holds no credentials" — is not a state this
system can be in.** Today it costs nothing: `main` holds only `xai:navegerc@gmail.com`,
which every agent uses anyway. It bites at NVB-17/18, where `owner`'s calendar token
would be mirrored into `main` on its first refresh and inherited by every agent lacking
it at the next gateway start.

`deploy/check-agent-auth.sh` therefore asserts a different thing: *for every profile id
in the default agent's store, every other agent holds that id itself* — the exact
condition under which nothing is inherited, and one that can actually hold. The old
rule 1 would have alarmed on every run forever, which is how a monitor becomes
furniture. `deploy/check-agent-auth.test.sh` covers both directions on a fixture; it is
a shell script beside the thing it tests rather than a pytest, because it needs the
`sqlite3` CLI, which CI does not have.

### Added — the project agent can leave a formal PR review (2026-08-17)

`github__pull_request_review_write` granted to `code-invariants`, so it can submit a
review with `REQUEST_CHANGES` rather than a plain comment that cannot mark the merge
box. Four layers updated — the server's `--tools` argv, `mcp.servers.github.
toolFilter.include`, the agent's `tools.alsoAllow` and the room's `tools.allow`
ceiling — and the tool count moved 7 → 8.

**The PAT already had `Pull requests: write`.** Verified before changing anything, by
posting a review to a closed PR and reading the status: `403` would have meant the
permission was missing, and a `200` came back instead. The pending review it created
was deleted immediately; a pending review is only ever visible to its author.

**`create_pull_request_review` is a phantom name.** `list-scopes` advertises it,
`--tools=` accepts it, and nothing registers — the same silent non-grant as
`create_issue`, which is now two instances and therefore a rule: on this server a
grant is verified by tool count, never by the absence of an error. Runbook 06 carries
a credential-free `tools/list` recipe for checking a name before touching live config.
The narrow verb ADR 0010 asks for does not exist here either: the real tool bundles
create, submit, delete-pending and thread resolution.

**REQUEST_CHANGES is not yet verified, and the smoke test is why.** GitHub rejects
`REQUEST_CHANGES` and `APPROVE` on your own PRs, and the agent's PAT is the owner's
identity — so the test PR, opened by the owner, could never have exercised it. The
agent fell back to `COMMENT` and said so, which is the failure reporting itself
correctly rather than a gap in the grant. The runner's PRs are authored by
`nvb-opencode[bot]` and take the full set, so the next real one settles it. Recorded
because a probe whose identity is wrong looks exactly like a working feature.

What the agent found in the planted test file is worth keeping as a baseline: both
deliberate bugs (`startsWith` matching `"src/gen"` against `"src/generated"`, an empty
pattern matching everything) plus two nobody planted — no path normalisation, and a
module wired to nothing.

Widening tool policy did **not** need the session store cleared. The documented trap
is that policy binds at session creation and *tightening* leaves existing sessions
alone; after a gateway restart the room session had the new tool without further
intervention.

### Fixed — `/opt/wpa` was emptied by a hand-written deploy, and nothing reported it (2026-08-17)

An `rsync -a --delete` from a staging directory whose own transfer had not finished
deleted the contents of every subdirectory of `/opt/wpa`, including `src/` and
`config/`. The recovery is written up in
[runbook 04](docs/runbooks/04-agent-deploy.md#optwpa-is-not-a-scratch-directory); what
is worth recording here is the shape of it.

**It did not look like an outage.** `wpa-gate` had its code and config in memory from a
restart the previous day and carried on serving. `systemctl is-active` said `active`,
`NRestarts=0`, no unit failed, the journal was clean. The damage would have surfaced at
the next unplanned reboot. Service state is not evidence about the tree it was loaded
from; `find /opt/wpa -type f | wc -l` is.

**The only copy of `config/config.toml` was in the directory that got wiped.** It is
gitignored — correctly, it names real people and real group ids — so the repo could not
restore it. It came back only because unrelated OpenClaw work the night before had left
a copy in `~/openclaw-snapshots/`. That is luck, and it is now
`wpa-config-backup.path`: a copy per change into `/var/backups/wpa-config`, keeping 20.
On change and not on a timer, because the file had been edited the day before it was
lost and weekly retention would have restored a version that predated the edit.

**`deploy/install-reader.sh` already did this correctly.** Its rsync carries
`--exclude config/config.toml` and it re-applies `root:wpa-config 0640` afterwards. The
loss came entirely from bypassing it with an ad-hoc command. `AGENTS.md` said "deploy
with `git pull` or `rsync -a --delete`", which invited exactly that; it now names the
installer and says why.

Three smaller findings, each of which cost a round trip:

- **rsync leaves in-progress directories at mode `700` and fixes permissions only at
  the end.** So a staging tree at `700` with link count `2` is a transfer that died
  partway, and syncing onward from it propagates the emptiness. Both directories looked
  plausible in `ls`.
- **`pin-group.py` prints the block, it does not write it.** Restoring a config whose
  pinned membership is one person short does not error — the gate drops that
  conversation as membership drift and the room just goes quiet.
- **The reader is the canary, not the gate.** Restoring the config as
  `root:wpa-gate 0640` let the gate start cleanly and broke `wpa-reader`, which is in
  `wpa-config` and re-reads the file every 30s. A long-running process holding a file
  in memory cannot tell you whether that file is still readable.

### Added — the project agent can read a pull request, not just `main` (2026-08-17)

The room could be told a PR existed and could read `main`; it could not read the PR.
`wpa-project-sync` now also fetches `refs/pull/*/head` and lays out one detached
worktree per **open** PR at `/workspace/pr-<N>/`, with `git diff origin/main...pr/<N>`
written beside it and each head sha recorded in `repo-sync.json`.

**The PAT did not change.** Pull refs need no credential on a public repo, so this is
still `Issues: read/write`, no contents — which is what won it over the GitHub MCP's
PR tools, where the same feature costs `Pull requests: read` + `Contents: read`, a
token rotation, a gateway restart, and returns a truncated diff through a 5000-char
window anyway.

The diff file is not a convenience. The agent has no `exec`, so it cannot run
`git diff`; a checkout alone would leave it comparing two trees by eye.

Cleanup is driven by `GET /pulls?state=open`, not by git. `main` is protected as
linear history so PRs land squashed, which means **a merged PR's head is never an
ancestor of `main`** and `git branch --merged` reports that nothing has ever merged —
the obvious implementation of "delete the worktrees of merged branches" would have
deleted nothing, forever. The open set also catches a PR closed without merging.

### Added — the head sha is a wake trigger, and `updated_at` turned out not to be broken (2026-08-17)

`wpa-gh-watch` now reports any PR whose head commit moved, read out of
`repo-sync.json` before and after the sync.

**This was built on a claim that measurement then disproved.** The design assumed
`updated_at` does not move for a plain push to a PR branch, making "opencode pushed a
fix" invisible to the existing `issues?since=` poll. An empty commit to PR #9 settled
it the other way: `updated_at` went from `2026-08-16T04:17:18Z` to
`2026-08-17T09:14:57Z`, two seconds after the push, with zero comments. The existing
poll does catch a push. The assumption was recorded as fact in the first version of
this entry and in runbook 06 before anything had tested it — worth remembering as the
shape of the mistake, since the feature was verified end to end while its stated
justification was never checked at all.

What the head sha is actually worth, having measured:

- **It is not author-filtered.** The `issues?since=` loop drops events whose author is
  the token owner, so a PR **Naveh raised by hand** produces nothing — five of the
  first seven PRs on that repo. `refs/pull/*` does not care who opened it. This is the
  real gap, and it is not the one the feature was designed for.
- **It names the commit.** The wake carries the sha and the checkout path, so the
  agent is told what to read rather than that something changed, and its dedupe id
  carries the sha so a re-push is a new event while a re-read is not.
- **It is derived from the mirror on disk**, so the agent is never woken about a PR
  whose worktree is missing.

Both triggers fire for a bot-authored push, which is why the verification reported
PR #9 twice. The `seen` list keeps each to one event.

The watcher runs the sync inline on every tick rather than only on PR events — the
worktree has to exist before the agent is told about it, and the comparison needs the
fetch to have happened. That reasoning is independent of the above and still holds.

A failing sync appends a stale-mirror warning to the wake instead of aborting it:
comments and CI failures still have to get through, and an agent told its checkout may
be stale is better off than one not woken at all. `flock` serialises the watcher's
inline run against the timer's, which otherwise race on `index.lock` and turn into an
`OnFailure` DM about nothing. `wpa-gh-watch.service` moves to `TimeoutStartSec=300`
to stay above 90s of sync plus the agent's `--timeout 120`.

Checked while doing this and worth recording as a *non*-finding: the author filter at
`gh-watch.sh:89` drops events whose author is the token owner (`NavehBrenner`), and
the opencode runner authors its PRs as `nvb-opencode[bot]`, so runner PRs are not
being filtered. The concern was that the two shared an account; they do not.
### Fixed — Q7 answered: the token refresh writes the credential into `main`, by design (2026-08-16, NVB-32)

Two rounds of this question ended by naming the last thing that changed before a quiet
period, and both were wrong. This one reads the code instead, and the mechanism turns
out to be ordinary, documented behaviour doing exactly what it was written to do.

Traced in the shipped `dist` of `2026.7.1-2` (`0790d9f`):

1. `resolveAgentDir(agentDir)` is `agentDir ?? resolveDefaultAgentDir({})`. **Undefined
   means `main`** — every write below inherits that default.
2. `resolvePersistedAuthProfileOwnerAgentDir` picks the store a credential update is
   persisted to, and returns `void 0` — main — for an agent's **own** profile whenever
   `shouldUseMainOwnerForLocalOAuthCredential` agrees.
3. That function agrees when both credentials are OAuth, the identities are adoptable,
   and **`main.expires >= local.expires`**.

So once `main` holds a copy of the same identity — here `xai:navegerc@gmail.com`, shared
by every agent because the profile id is the *account*, not the agent — every agent's
token refresh lands in `main` rather than in its own store. Main's copy is then always
the freshest, the expiry comparison stays true, and **the condition re-arms itself on
every refresh.** Nothing in the path asks whether anyone is inheriting.

Every recorded observation falls out of that one mechanism:

| observed | why |
|---|---|
| main's row at 20:06, `owner`'s frozen at 13:58 | the 13:58 token came due ~19:58 and the refresh went to main, so owner's own row never moved |
| refilled with every agent holding its own profile | the mechanism never consults whether anyone lacks one — which is why the 2026-08-15 hypothesis died |
| emptying main "held" for twenty hours | with main empty the check fails on its first line (`main?.type !== "oauth"`), so refreshes stay local. Emptying **is** the lever |
| bisection never caught the writer | it fires on token expiry, not on anything a person was doing at the time |

**A frozen per-agent row beside a moving `main` row is the signature.** Watch the rows,
not the file — a WAL checkpoint touches the sqlite file without touching the row, which
is how `owner` appeared to have been written at 19:30 when its row said 13:58. That
mistake was made and caught inside this same investigation.

Ruled out empirically en route: `mergeOAuthFileIntoStore` reads
`$STATE_DIR/credentials/oauth.json`, and no such file exists on the Pi. The earlier
round was right that there is no flat file to re-import from — the source was never a
file.

**The re-seed is answered too — see the 2026-08-17 entry above.** The suspect named
during this round (`maybeSyncPersistedExternalCliAuthProfiles`) was wrong.

An instrumented copy of `dist/sqlite-B1ze-fre.js` was prepared to catch the write
live, and was never needed — reading the code answered both halves. It was staged and
not installed; the live `dist` is untouched.

### Added — the isolation check finally runs on a schedule (2026-08-16, NVB-32)

`deploy/check-agent-auth.sh` shipped on 2026-08-15, reached the box on 2026-08-16, and
until now ran only when someone remembered. Given the mechanism above fires on token
expiry, "run it after a login" was never going to catch it.

`wpa-agent-auth.{service,timer}` plus `wpa-agent-auth-failed.service`, following the
`wpa-oc-auth` pattern: boot + hourly, Signal message to the owner on violation.
`OnBootSec=2min` is load-bearing rather than tidy — read-through resolves at **gateway
startup**, so what `main` holds at boot decides the whole uptime.

### Added — a third family DM agent, and placeholder names in the example configs (2026-08-16)

A fourth person now has a 1:1 agent on the Pi. The recipe is the one written down
after the last one, and it worked without incident: resolve the ACI, add it to
`channels.signal.allowFrom` and `groupAllowFrom`, add an agent entry and a `direct`
binding carrying the `uuid:` prefix, scaffold the workspace with an IDENTITY.md,
restart the gateway. Config validates, gateway came up clean, `NRestarts=0`.

`listContacts` again did not name the sender, despite 60 `dropped: sender` lines in
the gate journal over the preceding hour. `getUserStatus` against a phone number
answered immediately and needs no message to have been sent — but see below, because
what it answered with was wrong.

The group half followed once the owner added them (they are the group's only admin,
so nobody else can): `deploy/pin-group.py` regenerated the gate's pinned member set to
four, `--check` reported `family (group, 4 pinned members)`, and the gate restarted
without the drift refusal. The agent holds its own `xai` profile and answered a
neutral CLI probe.

### Fixed — `getUserStatus` returns a PNI for a stranger, in a field called `uuid` (2026-08-16)

The example configs now say so, in both places that tell you how to capture an id.

The number resolved to `1cebe820-…`, and that id went into `allowFrom`,
`groupAllowFrom` and the binding. An hour later, after the person had been added to
the family group, **the same call on the same number returned `c3a2d5c1-…`**. The
daemon's `recipient` row settles it: `aci = c3a2d5c1-…`, `pni = PNI:1cebe820-…`. The
first answer was the phone-number identity, and `getUserStatus` labels both `uuid`.

Nothing would have reported this. A PNI-keyed allowlist matches no envelope, so the
agent would have been deployed, validated, restarted, and silently deaf — which is
the same failure as the number-keyed allowlist found on 2026-08-11, wearing a uuid's
clothes. It surfaced only because the group's member list disagreed with the config,
and `listGroups` returns ACIs only.

Two usable tells, both now in the example configs: an id that is real but absent from
`listGroups` membership is suspect, and the `recipient` table stores `pni` with a
literal `PNI:` prefix while `aci` is bare — the one place the two can be told apart by
eye. `getUserStatus` is still the right first call; it is a lead, not an answer,
until the account knows the person.

The example configs now use `alice`/`bob`/`carol` rather than real first names, with
placeholder uuids as before. Nothing about the shape changed — but a public example
that names the household is a small, permanent disclosure for no benefit, and the
names were doing no explanatory work that a placeholder cannot do.

### Fixed — nothing, and that is the finding: `main` refilled itself with every agent holding its own profile (2026-08-16)

The rule recorded on 2026-08-15 — *the default agent stays empty only while every
other agent has its own profile; an agent with none is the cause* — is **false**, and
it failed exactly the test it named for itself.

`deploy/check-agent-auth.sh` reported `main` holding one profile. A snapshot taken
before that day's config change puts the row's `updated_at` at **2026-08-15 20:06**,
and the last agent to be given a profile of its own was written at **00:30** the same
day. Twenty hours of every-agent-has-one in between, and the next new agent did not
exist until 23:59. So the second hypothesis dies the same way the first did: it named
the last thing changed before a quiet period, not a cause.

What survives is smaller and unglamorous. Read-through resolves at gateway startup;
a non-empty `main` is a real grant to any agent lacking its own profile at that
moment; and nobody has identified the writer across two attempts. Treat a non-empty
`main` as **expected to recur** rather than as a regression, keep the check on a
schedule rather than running it after logins only, and do not mount a tool credential
on the assumption that `main` is empty. The ⚠️ on NVB-17/18 stands and is now on
firmer ground than when it was written.

The script also was not on the box — it shipped in the repo on 2026-08-15 and
`/opt/wpa/deploy/` never received it, so the invariant went unchecked for a day for
the dullest possible reason. It is installed now.

### Removed — the gate's `ack <timestamp>` reply (2026-08-15)

An accepted command is now recorded and answered with nothing. `_ack` and its only
caller `_recipient_of` are gone, with a test asserting an accepted command sends
nothing back — the cheapest way for this to return is somebody restoring a helper
that looks unused.

It was scaffolding for ADR 0008's quoted-reply confirmations: the timestamp was the
handle a later `YES` would quote, so NVB-16 could match a confirmation to its pending
action. ADR 0011 replaced that whole mechanism with OpenClaw's reaction approvals,
which bind a YES to a specific delivered message rather than to text a person
re-quotes, and it already listed "the ack path" among the things that become
duplicated work once OpenClaw owns the channel. This finishes that migration rather
than changing a decision.

What made it visible was the room working: OpenClaw answers the sender itself, so
every turn carried two messages and the first one said `ack 1786473936544`.

The test needed a new fixture — `_serve` closes the connection as soon as it has
written, which is exactly what would hide an unwanted reply, so `_serve_capturing`
stays open and records what the gate sends.

### Fixed — deleting the refresh key unregistered the whole provider on the runner (2026-08-15)

`deploy/push-opencode-auth.sh` now blanks the refresh token (`.refresh = ""`)
instead of deleting the key. Found by another session, which got past the App-token
failure and straight into the next one.

opencode's OAuth credential schema declares `refresh: Schema.String` — **required**,
unlike the `Schema.optional` fields beside it — and the loader is
`Record.filterMap(data, v => Result.fromOption(decode(v), () => undefined))`, which
*drops* a credential that fails to decode with no error, no warning and no log line.
So `del(.refresh)` unregistered the `xai` provider entirely on the runner, and
`getModel` then built its suggestions from the static catalog:

```
Model not found: xai/grok-4.6. Did you mean: grok-4.6, grok-4.6-fast?
```

A model-not-found error, naming the model that was requested, two layers from a
credential shape. The model string was never wrong. Verified against opencode
v1.18.18, `packages/opencode/src/auth/index.ts`.

An empty string satisfies the schema and is exactly as useless to a runner as an
absent key — the containment is unchanged.

#### Unflattering: the guard enforced the bug

The original didn't just strip the key, it **asserted the key was gone** and refused
to publish otherwise. So the check written to protect the credential was pinning the
broken shape in place, and anyone who fixed the transform would have been stopped by
the guard with a message insisting they had leaked a token.

The guard now asks the question that survives the change — does the real refresh
token appear anywhere in the payload — plus a check that the local store has one at
all, since an empty needle matches every string and would otherwise refuse forever.

That is three failures today from the same family: a grant that appears to grant and
does not (`--tools=create_issue`), a policy layer that silently replaces another
(`alsoAllow`), and now a credential silently dropped for failing a schema. All three
were invisible in config that validated clean, and all three surfaced as an error
about something else entirely.

### Fixed — the LAN resolver cannot serve Go, and "DNS blip" hid it for a day (2026-08-15)

Every statically linked Go binary on this Pi was failing to resolve `api.github.com`.
Measured against the GitHub MCP server: **14 of 15 lookups failed** with
`dial tcp: lookup api.github.com on 10.10.0.138:53: no such host`, while
`getent ahosts` succeeded 10 of 10 and `curl` returned HTTP 200 against the same
name. Pointing the box at a public resolver takes the identical test to 15 of 15.

`deploy/networkmanager/90-wpa-dns.conf` sets `dns=none` and `/etc/resolv.conf` is
hand-written with public resolvers first, the router last.

**`GODEBUG=netdns=cgo` was never a fix here, and had been sitting in the MCP server's
env pretending to be one.** It selects a resolver that is not compiled into a
`CGO_ENABLED=0` build, and `github-mcp-server` is statically linked. The same flag
*does* genuinely fix dockerd, which is dynamically linked — one box, one symptom, two
binaries, and only one of them could take that cure. It has been removed rather than
left implying a mitigation that does not exist. **Check `ldd` before reaching for
it.**

Two shape details, both found by getting them wrong first: per-connection `ipv4.dns`
is not enough because NM merges DNS from every active connection, so configuring
eth0 left wlan0 putting the router back at the top and the retest went straight back
to 9 of 10 failing; and the fix must be applied with `systemctl reload
NetworkManager` rather than `nmcli con up`, because SSH to this box arrives over
wlan0 and reactivating that connection would cut the session doing the work.

#### Unflattering: three sightings before anyone measured it

This surfaced as an agent saying "DNS blip talking to GitHub — retrying", twice, and
was noted both times as something to watch. A paraphrase that sounds transient is how
a 93% failure rate hides. The measurement took four minutes once anyone made one, and
the same lesson has now appeared three times today: check the artefact, not the
report about the artefact.

### Added — the project agent can inspect CI itself (2026-08-15)

`actions_get`, `actions_list` and `get_job_logs` are granted read-only, so the agent
pulls a failing job's log and reports the actual error instead of relaying that
something went red. Verified against the real opencode failure: asked what run
31894403463 did, it independently produced `Failed to parse JSON` and
`undefined is not an object (evaluating 'p.rest')`, and flagged its guess at the
cause as a guess.

`actions_run_trigger` is the fourth tool in that toolset and is deliberately
withheld: re-running a workflow is a write, and nothing about diagnosing a failure
needs it. The Issues-scoped PAT reads all three on a public repo — checked before
granting, so no credential was widened.

### Added — subagents, for both the owner and the project agent (2026-08-15)

`sessions_spawn` and `subagents` are granted to `owner` and `code-invariants`. A
subagent inherits the parent's own tools and **not** its MCP tools: a child gets
`read`, `write`, `edit`, `apply_patch` — plus `web_search`, `image_generate` and
`video_generate` where the parent has them — and is stripped of `github__*` as
non-inheritable and of the spawn tools as a recursion guard. So a subagent can read
and map the repo mirror or run a wide search; it cannot post to GitHub as the owner.
`agents.defaults.subagents.maxConcurrent` (8) bounds the fan-out.

**This corrects the entry below.** That one records subagents spawning and dying with
"No callable tools remain", concluded to be fail-closed platform behaviour. It was
neither: the child's allowlist is derived from the parent's **effective** allowlist,
and the parent's had no file tools in it because of the `alsoAllow`-replaces-global
fault. The same one-line fix that restored the parent's `read` restored subagents.

Two things worth carrying forward from that. A platform limitation was inferred from
a symptom that was local misconfiguration, and the inference was confident enough to
revert the feature — the experiment was only re-run because the input to it had
demonstrably changed. And the inherited set follows the **session**, not the agent: a
child spawned from a `--agent` CLI probe reports a narrower list than one spawned in
the room or the DM, which is the third time today a CLI-session probe has answered a
different question from the one being asked.

### Added — the project agent can read the code, without gaining a capability (2026-08-15)

`wpa-project-sync.timer` keeps a shallow mirror of `main` at `/workspace/repo` inside
the agent's sandbox, which it reads with the `read` tool it already had. New:
`deploy/sync-project-repo.sh` and three units; `/etc/wpa-project.env` now holds the
project's coordinates for both this and the watcher, so the repo slug has one home.

**Nothing was granted.** The alternatives were exec plus a network in the sandbox —
undoing NVB-14/23/25 for a `git fetch` — or GitHub repo tools, which read one file
per call against a 5000-char window and need a wider PAT. A directory appearing in a
workspace the agent already reads costs neither.

**The agent still decides when.** It cannot run the sync, but writing any text into
`/workspace/repo-sync.request` makes the next tick sync immediately and consume the
file. That is ADR 0009's shape applied to a second problem: the agent expresses an
intent, the host runs a fixed command with no agent-controlled arguments, so there is
no argument to smuggle anything through. Verified end to end — the agent wrote the
file, the next tick logged `reason: "request"`, the file disappeared.

`repo-sync.json` carries the commit, its subject and the fetch time, outside the
checkout so `git clean` cannot remove it. A mirror that merely looks current is worse
than none, so the agent is told to state which commit it is reasoning from. It reads
both correctly: `code-invariants-workspace` and `f1f6bc3f…`, each checked against the
real file rather than taken on the model's word.

#### Unflattering: granting a tool silently removed four others

An agent-level `tools.alsoAllow` **replaces** the global one rather than merging with
it. Adding the GitHub tools to `code-invariants` therefore stripped `read`, `write`,
`edit` and `apply_patch` from that agent — including its ability to maintain its own
`MEMORY.md` — and it had been that way since the MCP work landed earlier the same
day. Nothing reported it: `config validate` passed, the room's `tools.allow` ceiling
still named those tools, and the tool-policy log lists removals per layer without
saying that a layer was *overridden*.

It surfaced only because the mirror gave the agent something to read, and it answered
"No `read` tool is available in this session" while listing the four it did have. The
lesson matches the `--tools=create_issue` one from the same afternoon: the artefact
worth checking is the tool list the agent actually holds, not the config that implies
it.

### Added — a project room: its own agent, GitHub issues, and a watcher that wakes it (2026-08-15)

A Signal group bound to one repository, served by a `code-invariants` agent that can
file and comment on that repo's issues and is woken when the repo moves. New:
[runbook 06](docs/runbooks/06-the-project-room.md), `deploy/gh-watch.sh`,
`deploy/outbox-notify.sh`, three units, and the config templates.

**It is a separate agent because `load_config` refuses one agent in two
conversations, and that refusal earns its keep here.** The room receives issue bodies
and PR titles from a *public* repo — text a stranger wrote, arriving at an agent with
durable memory. A shared session would carry a poisoned issue into the private chat;
a shared workspace would keep it in the same `MEMORY.md`. The cost, no shared context
with the DM, is the intended trade.

The agent's write reach is one repo's issues, through a stdio MCP server narrowed
three times: `toolFilter.include`, the server's own `--tools`, and a fine-grained PAT
with `Issues: read/write` and no contents. The PAT is the floor, and the only one
that holds if the other two are wrong.

#### `create_issue` does not exist, and nothing says so

GitHub's own `list-scopes` advertises it, `--tools=create_issue` is accepted without
complaint, and nothing registers. The real tool is `issue_write`. Only the **tool
count** in `mcp probe` exposed it — the same silent-non-grant failure ADR 0011
records for OpenClaw tool lists, appearing in the vendor's server this time. It also
costs ADR 0010's narrow verb: `issue_write` can close and relabel, and there is no
create-only tool, so containment falls back to the PAT's scope.

Worth adding to the deploy checklist: `mcp probe` returns tools and no diagnostics
against a **dead credential**, because listing tools does not authenticate.

#### Six layers can strip a tool, and one of them is invisible

Granting a tool to this agent meant passing `tools.profile`, global `tools.deny`,
the agent's `alsoAllow`, the room's `tools.allow` ceiling, `tools.sandbox.tools.allow`
— and a **built-in `sandbox tools.deny` that is not in our config at all**. The
tool-policy log names the layer every time, which is the only reason this took
minutes rather than an evening; it should be the first thing read when a granted tool
"is not available".

That invisible layer is why the agent has no `cron`: upstream denies scheduling to
sandboxed agents. Granting it would mean emptying an unenumerable default to unmask
one entry — the NVB-23 shape, and declined on those grounds.

#### Unflattering: two verification failures, both mine

**A probe whose success value equalled its failure value.** Asked for a count of open
issues, got `0`, called it working. The repo genuinely had zero, so a dead credential
passed the check. The 401 only surfaced when the agent was asked in the room and
answered at length. The fix that worked was hashing `/proc/<pid>/environ` against the
token file — an objective check with a model nowhere in it.

**A wake mechanism assumed rather than tested.** The watcher was designed around
`openclaw system event --mode now`, which enqueues an event and returns `ok` without
running a turn, because the heartbeat that consumes the queue is suppressed by a
comments-only `HEARTBEAT.md`. `--expect-final` also returns `ok`. The working
primitive is `openclaw agent --session-key … --deliver`. Compounding it, the gateway
logs no `delivered reply` line for a CLI-initiated turn, so the log that looked like
evidence of failure was evidence of nothing.

#### A rotated token needs a restart, not a reload

`openclaw mcp reload` disposes cached runtimes but does not re-read a changed env.
The MCP child is spawned per turn from the gateway's *in-memory* config, so after a
token change the file and the config agree while a live child holds the old value and
returns 401. The symptom points at the credential; the cause is the process holding
it.

#### Things that will bite the next person

- **Subagents spawn and inherit nothing.** The child gets only the spawn-related
  tools, which a subagent-specific deny then strips, so it dies with "No callable
  tools remain". Fail-closed, so no escalation risk — and no use either. Left denied.
- **The MCP server is gateway-global, not agent-bound.** Only `code-invariants` is
  granted the tools, but that is tool policy rather than an absent credential, which
  is the distinction ADR 0010 exists to draw. Per-agent MCP servers are not supported
  on `2026.7.1-2`.
- **The watcher's cursor advances on wall-clock, not on events.** Anchoring to the
  newest event freezes the window on a quiet repo; with `per_page=50` and no
  pagination that drops events off the end later. A quiet week would have silently
  broken the first busy day.

### Fixed — pinned membership guarded the wrong direction (2026-08-15)

`drain` now takes the drifted-room set and drops outbound addressed to a drifted
group, with a `membership` refusal counter alongside the others.

The mechanism itself was never broken: `_drifted` handles the cases that matter —
a matching set refuses nothing, a member carrying both a uuid and a number counts
once, `isMember: false` is drift rather than absence, a one-to-one has nothing to
drift — and `Membership.closed()` starts by refusing every group so the window
before the daemon answers fails closed. Six tests, all passing.

**It was wired to ingress only.** `decide` got `membership.refusing`; `drain` never
did. Under the pre-OpenClaw design that was coherent — refuse the command, no reply
is generated, nothing for a new member to read. It stopped being coherent twice
over. ADR 0011 moved inbound to OpenClaw, so the gate refusing in its own ledger
does not stop the agent answering; and an entry written with **no inbound command
behind it** — a notification, a scheduled report — has nothing to refuse on the way
in at all. ADR 0011 states the purpose plainly: a newly added member "reads every
reply it puts in the room". That is a claim about egress, and egress was the
unguarded half.

#### Unflattering: the first version of the fix would have destroyed messages

Checking `recipient.id in refusing` and consuming the entry is the obvious patch and
it is wrong, because `Membership.closed()` starts with **every** group refused. In
the window between the gate connecting and the daemon answering `listGroups`, every
group-addressed entry would have been unlinked and counted as a refusal — and
delivery is at-most-once by design, so the file is the only copy. A notification
written before a restart would have been destroyed rather than delayed.

Ingress and egress cannot share a failure posture here. A refused *command* is
retried by the person who sent it; a refused *send* is gone. So `Membership` grew a
`known` flag, set on the first answer, and a group entry is **held rather than
consumed** until then — the one case where an entry survives a drain cycle. A
one-to-one is never held: a private chat has no membership to drift, and making it
wait on `listGroups` would turn a slow daemon into a silent assistant.

#### The stub daemon reported no groups, and nothing had noticed

`_serve_answering` answered `listGroups` with `result: []` — a permanently drifted
room — and every test that expected a group send passed anyway, because outbound
never consulted membership. Wiring egress to the pin turned that into a failure, and
`_serve_answering` now takes the member set, defaulting to none so the connect-time
drift test keeps the behaviour it actually asserts.

Worth recording how it surfaced: **the full suite passed locally and failed in CI on
identical code.** The race is whether the `listGroups` response is applied before the
drain, and it resolved differently on the two machines. A green local run is not
evidence here, which is the same lesson as the rest of today pointed the other way —
check the artefact that matters, and for this the artefact is CI.

### Added — coding work is dispatched to GitHub, and the Pi keeps the token alive (2026-08-15)

The assistant can now be the front end for coding work on the owner's other
repos without becoming a coding agent itself. `/oc` in a GitHub issue comment
runs opencode as grok inside an Actions runner, which plans, branches and opens a
PR; the owner approves the plan in Signal and merges on GitHub. New:
[runbook 05](docs/runbooks/05-opencode-ci-token.md), `deploy/push-opencode-auth.sh`,
`deploy/oc-auth-notify.sh` and three units.

**The design question was answered by the box, not by preference.** The obvious
route — hand the agent a GitHub PAT — is dead on arrival here: `tools.deny`
carries `exec`, `process`, `terminal` and `code_execution`, and the sandbox runs
`network: "none"` with `readOnlyRoot` and `capDrop: ALL`. There is no way to
spend a credential from inside that, which is exactly what NVB-14/23/25 were for.
So the Pi keeps the control surface and GitHub gets the execution environment,
and the only long-lived credential the arrangement needs is the xAI one. Per-repo
code access is the runner's own `GITHUB_TOKEN`, scoped by GitHub and dead when
the run ends — there is no PAT for it to leak.

#### The credential is a subscription token, and that is the whole cost

An API key was the easy answer and was declined: the subscription is already
paid for, and ADR 0011 records that Q4's constraint rules out *pasting* a
subscription token into a third-party tool rather than using one at all. What
the subscription costs is that **its refresh token rotates on use** — verified
2026-08-15 by backdating `.xai.expires` and diffing the store, where `access`,
`expires` *and* `refresh` all changed. Rotation means whichever machine refreshes
invalidates every other copy, so an ephemeral runner that refreshes once kills
the login on the Pi.

The Pi is therefore the sole refresher, held by four rules that each fail quietly
on their own: force a refresh every run (opencode refreshes only near expiry and
has no "refresh now", so the script backdates the expiry and makes one throwaway
call); **prove the refresh took** before publishing, since a silent failure would
otherwise put a nearly-dead token in the secret and 401 CI hours later on another
machine; strip the refresh token from the published copy, so a runner *cannot*
rotate ours; and run at 4h against a ~6h token, so a runner never reaches the skew
where it would try. The third is the containment and the fourth is its fallback —
the worst case becomes one CI run returning 401 rather than a dead login.

Two details that decide whether the checks work at all. `expires` is in
**milliseconds** (13 digits, verified on hardware), so a validation written
against a seconds clock would fail always — and a check that always fails is a
check somebody deletes. And the strip is **asserted**, not assumed: a `del()`
that silently matched nothing would publish a live refresh token into a public
repo's secret store.

The published value is **raw JSON, not base64**. Publisher and workflow have to
agree, and briefly they did not — two boxes were publishing in different formats,
which fails intermittently rather than outright. One publisher now: the Pi, which
is the box that is always up.

#### Unflattering: the first design put the write credential in the runner

The initial sketch had the runner write its refreshed token back to the repo
secret, which needs a PAT with `Secrets: write` sitting in a runner that executes
an agent driven by public issue text. That is a credential able to rewrite the
repo's secrets, handed to the least trusted process in the system, to solve a
problem the always-on box already solves by pushing. The direction was wrong, not
the mechanism.

The same instinct nearly repeated with a self-hosted runner: it fixes rotation
completely and is the right answer for private repos, but GitHub's own guidance
is not to point one at a public repo, and most of these repos are public.

#### Unflattering: a guessed hardening list, caught before it shipped

`ProtectSystem=strict` was written first and would have made the whole hierarchy
read-only, including the home directory opencode writes to. It is `full` with a
`ponytail:` note rather than `strict` with a guessed `ReadWritePaths=` list,
because a guessed allowlist fails at 04:00 on a timer rather than in front of
someone. The failure path was then fired on purpose before being trusted — the
Signal DM arrived, `wpa-gate` logged `sent agent=owner profile=owner-full`, and
the outbox drained.

#### Things that will bite the next person

- **Two hand-maintained lists.** `OC_REPOS` in `/etc/wpa-oc.env` and the PAT's
  repository list on GitHub. Adding a repo to one only shows up as a `gh` 404,
  which reads like a typo'd name rather than a permission never granted.
- **opencode picks its own default model, and it has picked a *video* model.**
  The run fails with "not available on this endpoint", which reads like an auth
  fault. `OC_MODEL` is not optional.
- **`XAI_API_KEY` must not be set in the workflow.** Precedence is env → config →
  auth store, so it would silently override the restored OAuth and the run would
  succeed against the wrong credential.
- **The plan/build split is a shell expression on the comment body**, so if
  opencode renames its primary agents it fails open to `build` — a plan request
  would write code.

### Fixed — inbound images never reached the agent (2026-08-15)

Reported as "the agent doesn't see photos I send it". **Two independent causes, either one
sufficient**, which is why it looked like one broken feature rather than two settings:

| | was | now |
|---|---|---|
| `channels.signal.ignoreAttachments` | `true` — channel discards before any agent sees it | `false` |
| `agents.defaults.imageModel` | unset, so nothing handled a turn OpenClaw thought the primary could not take | `{primary: "xai/grok-4.3"}` |
| `channels.signal.mediaMaxMb` | unset on the live box | `8` |

signal-cli was never the problem: the reported image was on disk at
`/var/lib/wpa-signal/attachments/`, 96KB, timestamped to the minute it was sent. Everything
after that point discarded it, and **nothing logged the discard** — the same silent-refusal
shape as the `dmPolicy` and mention paths.

#### The second cause is a wrong catalog entry, not a model limitation

**`xai/grok-4.5` reads images perfectly well.** OpenClaw's catalog says `Input: text`, and
that metadata is wrong — xAI documents 4.5 as multimodal, and it was confirmed by hand.
Everything written here on 2026-08-14 about 4.5 "giving up image input" took that column as
ground truth and is corrected in place.

It still *matters*, because **OpenClaw routes on the metadata rather than on what the model
can do.** Believing the primary cannot take images, it looks for `imageModel`, found nothing,
and the turn had nowhere to go. Setting `imageModel` satisfies the wrong belief and images
start working — on grok-4.3. Measured since the change: **16 turns on grok-4.5, exactly 1 on
grok-4.3**, and that one at 00:45:09 matches the photo on disk at 00:45.

So the line is a functioning workaround for stale catalog data, not the intended
arrangement. Putting images back on 4.5 means correcting the entry under
`models.providers.xai.models` — **not attempted**, because writing that key overrides the
plugin's provider registration and it is still unestablished whether a `models` array
extends the stock catalog or replaces it. Replacing it would take `grok-imagine-*` with it
and silently kill media generation. One scratch-config experiment with
`OPENCLAW_CONFIG_PATH` settles it; until then, images on 4.3 is the working state.

Related: the same listing has reported both 500k and 200k context for 4.5 on the same box.
Nothing in that column should be treated as load-bearing without checking xAI's own docs.

`ignoreAttachments` was **not** a documented threat-model control. Every attachment mention
in the ADRs is about *outbound* generated media (0006, 0012); this was hygiene. It is now a
decision, judged the way ingestion paths are judged here: an inbound image is
attacker-influenceable content entering a context with durable memory, so a photo carrying
instructional text is the same shape as a poisoned `web_search` result — already accepted,
for the reason that still holds (nothing outbound to pivot to, no WhatsApp write path). It
is a *wider* surface than search, because a person can be talked into forwarding a picture
far more easily than into pasting a link. `mediaMaxMb` stops being cosmetic at the same
moment and was unset on the box.

#### Verified end to end — and the probe that said otherwise was the bad evidence

**Confirmed working on a real Signal send**, by the model routing above: one grok-4.3 turn
at 00:45:09 against a photo received at 00:45, answered correctly.

The interesting part is the probe that preceded it and pointed the wrong way. `openclaw
agent` has no attachment flag, so the Signal path cannot be exercised from the CLI; the
substitute was a valid 96×96 solid-**blue** PNG (RGB 30/90/220, confirmed with `file` and by
parsing the IHDR) placed in the workspace, with the agent asked to `read` it and name the
colour. It said **"green"**, and the journal showed grok-4.5 serving that turn.

That was read as "the model is blind because it is text-only". Both halves were wrong: the
model is not text-only, and the `read`-tool path is not the channel attachment path — it
evidently never handed the model an image at all. **A proxy for the path you actually
changed is not a test of it**, and a wrong result from one is worth less than no result,
because it invites a confident wrong diagnosis. The real test took one photo.

What does survive: **a model that is not given the image guesses rather than erroring**, so
any check asking "did it answer?" instead of "did it answer *correctly*?" will pass while
blind. Verify with an image whose answer is known in advance.

### Deployed — `liron` and grok-4.5 are live on the Pi (2026-08-15)

Applied to `~openclaw/.openclaw/openclaw.json`: the model bump, one agent (`liron`), one
`allowFrom` uuid, one binding. Snapshots at `~/openclaw-snapshots/2026-08-15-liron-grok45/`
(config + all sqlite) and `openclaw.json.bak-preliron`. Diff against the backup was exactly
those four changes; `openclaw config validate` clean; gateway restarted with `NRestarts=0`
and logged `agent model: xai/grok-4.5`.

Verified on hardware:

| | |
|---|---|
| identity | answers `Pi` |
| tools | `apply_patch, edit, image_generate, read, session_status, video_generate, web_search, write` — identical to `owner`, no `exec` |
| model | `[model-fetch] response provider=xai model=grok-4.5 status=200`, `url=https://api.x.ai/v1/responses` |
| isolation | own container `openclaw-sbx-agent-liron-…` alongside owner's and family's |
| workspace | `workspace-liron`, bootstrapped, `IDENTITY.md` carrying `Name: Pi` |

`aryeh` and `aviv` stay templates in the example config and are deliberately **not** on the
Pi: with no ACI, a binding for them would be a live rule pointing at a placeholder uuid.

Two divergences between the example config and the box, both pre-existing and now stated in
the example: the live config omits `workspace` (OpenClaw derives `workspace-<id>`), and
`liron`'s ACI lives in `.local/pi.md` rather than the public example, which keeps
placeholders.

#### ⚠️ The default agent is not empty, and a new agent working is how we found out

`liron` was deployed with **no auth profile of her own** and answered correctly on the first
turn. She should have failed. `openclaw models --agent liron status` explains it:

```
xai effective=profiles:~/.openclaw/agents/main/agent/openclaw-agent.sqlite
```

`main`'s auth store holds an `xai` OAuth profile, `store_key=primary`, written **2026-08-14
19:54:17** — nine seconds before `owner`'s at 19:54:26.

This contradicts the invariant [ADR 0011](docs/decisions/0011-openclaw-owns-the-channel-the-gate-owns-the-room.md)
rests its credential isolation on — *"the default agent holds no credentials… an agent cannot
inherit what does not exist upstream"* — verified empty on 2026-08-12 and false since the
following evening. Nothing looked wrong for a day, because every agent needs inference
anyway and inherited inference is indistinguishable from working correctly.

#### Resolved: `main` is empty again, and what keeps it empty is rule 2

**Final state, verified across a gateway restart:** `main` holds nothing, `owner`, `family`
and `liron` each hold their own `xai` profile and resolve to their own store, and every
agent answers. `deploy/check-agent-auth.sh` asserts both halves and gates a deploy.

The intermediate conclusion recorded below — that the invariant was *not enforceable* on
2026.7.1-2 — was **wrong**, and the correction is the useful part. Emptying `main` failed
the first time because one agent (`liron`) had no profile of its own and was depending on
read-through. Once she had her own login, `main` was emptied again and stayed empty through:
45s idle, `models status`, `models list`, `models auth list`, agent turns by `owner` and
`liron`, a deliberately failing turn by `main` itself, and a full gateway restart.

So the two rules are not independent, and only one of them is actionable:

> **The default agent stays empty only while every other agent has its own profile.**

Checking "is `main` empty" alone would have reported success right up to the moment it
mattered. `deploy/check-agent-auth.sh` therefore checks both, and treats an agent with *no*
profile as a violation in its own right — that is the condition that causes the refill, not
merely a tidiness issue.

**The writer was never identified**, and that is recorded rather than papered over.
Bisection ruled out idle time, the read-only CLI paths, ordinary agent turns and the
no-credential failure path; there is no `auth-profiles.json` flat file to be re-imported
from; no config key disables read-through (confirmed against the schema, as ADR 0011 said).
The one observed refill happened in a window where an agent was inheriting. Because the
cause is unproven, the script **detects the state rather than preventing it** — the honest
shape for a fault whose mechanism is unknown. If `main` is ever found non-empty again with
every agent holding its own profile, that hypothesis is dead and the investigation reopens.

#### The failed first attempt, kept for the record

The obvious fix was tried and **failed**, and the failure is more interesting than the bug.
Gateway stopped, `auth_profile_store` and `auth_profile_state` emptied, gateway started
(snapshot at `~/openclaw-snapshots/2026-08-15-empty-main/`). Then:

| time | what | evidence |
|---|---|---|
| t+0 | main empty, gateway restarted | `Providers w/ OAuth/tokens (0)` |
| t+0 | `owner`, `family` still work | both answer `ping` from their own stores |
| t+0 | **`liron` correctly denied** | `FailoverError: No API key found for provider "xai"` |
| t+~2min | **main is full again**, 1681 bytes | `updated 2026-08-15 00:15:55` — `owner`'s store untouched at 19:54:26 |
| after restart | **`liron` inherits again** | `effective=profiles:…/agents/main/…`, answers `ping` |

Two things follow, both verified rather than reasoned:

- **Read-through is resolved at gateway startup, not per turn.** With main empty at boot,
  `liron` was denied even after main was repopulated mid-run; one restart later, with main
  populated at boot, she was granted. So the state that matters is main's contents *at boot*.
- **Normal operation repopulates main.** Nobody ran a login. Some path writes the resolved
  profile back into the default agent's store — `owner`'s own store was not touched in the
  same window, so it is not a refresh of the calling agent's credential. **We did not
  identify the writer**; `resolveDefaultAgentDir` is used on several auth paths in `dist`,
  but the one that fired here was not pinned down, and guessing would be worse than saying so.

So the original explanation in this entry — "someone omitted `--agent`" — is **insufficient**.
It may be how the profile first appeared, but it is not why main is non-empty now, and a
discipline fix ("always pass `--agent`") would not have prevented this and will not hold it.

At the time this read as "main cannot be kept empty on 2026.7.1-2". It is better read as
what it was: the refill happened *while an agent was depending on read-through*, and the
attempt was made before that dependency was removed. Fixing the dependency fixed the symptom.

One conclusion survives the correction unchanged and is the reason any of this matters:
**NVB-17/18 must not mount a tool credential on the assumption that `main` is empty.** The
invariant now holds, but it holds conditionally, on a mechanism nobody has traced — which is
a fine footing for inference on a shared subscription and a poor one for a calendar token.

Possibly still worth filing upstream, on the same reasoning as
[openclaw/openclaw#123815](https://github.com/openclaw/openclaw/issues/123815): a documented
isolation boundary that normal operation silently reopens.

The one-line check belongs in the deploy runbook regardless — `openclaw models --agent <id>
status` prints the effective store path, and a credential in the wrong store is invisible
from `openclaw.json`.

### Changed — default model moves to `xai/grok-4.5` (2026-08-14)

`agents.defaults.model.primary` goes from `xai/grok-4.3` to `xai/grok-4.5`. One key, no
provider declaration needed: the model is already in the installed plugin's catalog and
already authorised on our OAuth.

**`openclaw models list` lied about that, and the flag is the whole lesson.** The bare
command lists only *configured* models, so it showed four xAI models with no 4.5 among
them — which reads exactly like "this OpenClaw is too old for that model". Grepping `dist`
agreed, because the catalog is not a string literal in the bundle. `--all` shows the truth:

```
xai/grok-4.5    text        500k    auth yes
xai/grok-4.6    text        500k    auth yes
xai/grok-4.3    text+image  1000k   auth yes   default,configured,alias:Grok
```

The near-miss is worth recording: the next step would have been declaring the model by hand
under `models.providers.xai.models`, which the schema does accept — a duplicate catalog
entry, hand-maintained, for a model the provider already knew about. **Use `--all` before
concluding a model is missing.**

Tested against a scratch copy of the live config via `OPENCLAW_CONFIG_PATH`, so nothing was
written to the running gateway to find this out.

**What 4.5 gives up: image input and half the context** — `text+image`/1000k becomes
`text`/500k. Neither costs anything today, and both are one setting away from costing
something. Image input is moot only because `channels.signal.ignoreAttachments` is `true`;
if attachments are ever enabled, `agents.defaults.imageModel` must be set to a
vision-capable ref in the same commit, or the agent silently cannot see pictures people
send it. Media *generation* is unaffected — `imageGenerationModel` / `videoGenerationModel`
are separate refs on `grok-imagine-*`.

### Added — a 1:1 agent per family member, in config only (2026-08-14)

Three more agents — `aryeh`, `aviv` and `liron`, one per family member's private chat
with the assistant. Config in both authoring surfaces; **not applied to the Pi yet**,
because it needs their ACIs and three interactive OAuth logins.

`config/openclaw.example.json5` gains three `agents.list` entries, three `bindings`, three
`allowFrom` uuids and three `dms` entries. `config/example.config.toml` gains the matching
conversations and a `family-dm` profile. No code changed: the whole feature is config, and
the generator that would have written it (`deploy/render-agents.py`, NVB-14) still does not
exist — three hand-edits are smaller than the thing that would avoid them.

**Three agents rather than one shared family-DM agent, and the reason is memory, not
tools.** `session.dmScope: "per-channel-peer"` already gives each peer its own session, so
one agent looked sufficient. It is not: a **workspace is per agent**, so three DMs on one
agent put three people's `MEMORY.md` in one directory that all three sessions read and
write. Durable memory is exactly what [ADR 0012](docs/decisions/0012-the-runtime-is-the-one-the-sandbox-can-reach.md)
restored, so the sharing that used to be theoretical is now the default outcome. It is ADR
0010 rule 1 arriving through the back door of a feature added since that rule was written.

**None of the three carries a `tools` block.** "Everything except exec" is already the
global policy — `exec` is denied there — so the requested surface is what an agent with no
override inherits: `read`, `write`, `edit`, `apply_patch`, `session_status`, `web_search`,
`image_generate`, `video_generate`. An empty per-agent block would be an invitation to widen
it later without re-asking the question every tool has to answer.

**A 1:1 agent is not admission to the family room.** `allowFrom` gains three uuids;
`groupAllowFrom` gains none. They are two grants and the example config now says so, because
that list is channel-wide — a uuid added to it "for consistency" is admitted to *every*
allowlisted group, which is ADR 0011's fifth gate responsibility in one line.

**Approval targets deliberately stay the owner's.** A prompt raised inside a family
member's DM is answered by the owner out of band. The approval exists because the action
needs a judgement, and the person whose message triggered it is the one an injection is
speaking through.

#### Onboarding someone requires their ACI, and the obvious way to get it does not work

Verified end to end on 2026-08-14 by having an unlisted person DM the assistant. Six
messages arrived. What the system said about who sent them:

- **The gate: a reason and a counter, no identifier.** `dropped: sender (18 total)`. That
  is the "no message content in logs" invariant applying to the sender as well as the body,
  and it is correct behaviour — but it means watching the journal proves only that a
  stranger arrived.
- **The gateway: nothing at all.** Six refused DMs produced one unrelated line in twenty
  minutes. A `dmPolicy: "allowlist"` refusal is silent at normal log level.
- **`listContacts`: nothing either, and this one is a trap.** It returns address-book
  contacts, not everyone the account knows. The sender had a `recipient` row, an
  `identity`, an open `session` and a fetched profile name, and still did not appear. An
  earlier draft of this entry recommended it as *the* method; it is not, and an empty
  result must not be read as "they never messaged".

Two things that do work, cheapest first:

- **`getUserStatus` needs no message at all**, resolving a phone number straight to an ACI:
  `{"recipient": "+1…", "uuid": …, "isRegistered": true}`. A lookup against Signal's
  servers, so it discloses to Signal that the assistant asked about that number.
- **The daemon's own recipient store**, once they have messaged:
  `SELECT aci, number, profile_given_name FROM recipient` in
  `/var/lib/wpa-signal/data/<id>.d/account.db`. Snapshot `-wal` and `-shm` with it and read
  the copy — same rule as msgstore ([ADR 0003](docs/decisions/0003-local-db-read.md)), and
  this file *is* the account. Cross-check against `identity` / `session`: the `recipient`
  table also carries PNI-only rows that never sent anything, so the ACI list alone is
  ambiguous and those two tables disambiguate it. Delete the snapshot afterwards.

The envelope tap in `.local/pi.md` answers it too and is the worst of the three: it writes
plaintext message bodies to disk to learn one uuid.

Known cost, unresolved until it is tried: this makes **five** device-code OAuth logins on one
xAI subscription. If it refuses, the fallback is ADR 0012's — `main` holds the model
credential only, and tool credentials stay out of it.

### Changed — signal-cli runs at the gateway uid, and the media tools go on (NVB-26 + NVB-27, 2026-08-14)

`image_generate` and `video_generate` are enabled for both agents. They were never broken:
they registered, generated correctly in ~90s inside the sandbox, and finished cleanly. What
failed was the last hop — OpenClaw hands signal-cli a **path** under
`~openclaw/.openclaw/media/outbound`, and signal-cli ran as `wpa-signal`, which could not
traverse that `0700` chain. No group grant survives it: OpenClaw re-asserts `0700` on `media`
on every generation (`0710` before a run, `0700` after), so the only process that can read a
generated attachment is one running as `openclaw`.

So `signal-cli.service` now runs as `openclaw`. [ADR 0006](docs/decisions/0006-two-process-privilege-split.md)
is amended with the narrowed split and what would reverse it.

**Only `User=` moved. `Group=` stays `wpa-signal`,** which the issue's own plan did not
anticipate and which removes half the cost it budgeted for. `RuntimeDirectory` and
`StateDirectory` follow `User:Group`, so the socket is `openclaw:wpa-signal` and `wpa-gate`
reaches it through the supplementary group it already had — **`wpa-gate.service` did not
change at all**, and `StateDirectoryMode=0700` still leaves the group no execute bit, so the
account directory is exactly as unreadable to the gate as it was before. The planned
`SupplementaryGroups=openclaw` on the gate was not needed and was not applied.

#### Filed upstream: attachments travel as a path, not as bytes

[openclaw/openclaw#123815](https://github.com/openclaw/openclaw/issues/123815). NVB-27 promised
to file this whatever we decided locally, because path-based delivery silently assumes the
gateway and the transport share a uid — and the failure surfaces at the last hop, after
everything appears to work.

Checked first, as it should be: no existing issue covers it, and `send.ts` on `main` still
does `attachments = [resolved.path]`, so it is not fixed in an unreleased branch either. The
suggested fix needs no signal-cli change — its own `--help` documents that `--attachment`
accepts "either a file path **or a data URI**" per RFC 2397, with an optional `filename=`. So
the bytes can travel over the JSON-RPC connection that is already open. Proposed as opt-in,
since base64 costs about a third in size and video runs against `mediaMaxMb`.

#### Unflattering: the firewall rule protecting the control channel did not exist

NVB-27's checklist said to *confirm* that `tcp dport 8081 meta skuid { 0, 991 }` was still
correct after the uid change. There was no such rule. There was no rule on that port at all —
the only nftables table on the box is `inet lxc`, and `iptables-legacy` was empty.

`signal-cli.service.d/10-http.conf` — the NVB-20 spike drop-in that opened the port — flagged
this in its own comment ("an HTTP port is reachable by ANY local user. Revisit before this is
permanent") and it was never revisited. So since the spike, **any local uid could send as the
assistant and read the entire inbound stream**, with no credential, on the machine's most
trusted channel. Measured after the fix: uid 1000 gets a dropped SYN and a 6s timeout, uid
991 gets an HTTP response in 2.6ms.

This also changes the honest accounting for the uid change itself. Two of the three costs the
issue listed were **already paid**: the gateway has always seen every inbound envelope (it is
a JSON-RPC client of the same daemon), and sending as the assistant was open to every local
uid until today. What actually moves is the account **key material at rest** — which is the
durable half, and the half worth writing an ADR amendment about.

The rule ships as `deploy/nftables/wpa-signal-8081.nft` plus a oneshot unit, rather than as a
live-only change. Three things in it are load-bearing and each fails silently the other way:
**no `flush ruleset`** (it would take Waydroid's lxc bridge with it), the **output** hook
(`meta skuid` is only available on locally generated packets — the same rule on input matches
nothing and allows everything), and **`oif "lo"`** (without it, every other uid loses
outbound connections to port 8081 on any host).

`--http 127.0.0.1:8081` also moved out of the spike drop-in and into the unit. It is not a
spike any more; it is the control channel, and a drop-in that silently wins over the unit's
`ExecStart` is a bad place for it.

#### What the change needed on the box

- `chown -R openclaw:wpa-signal /var/lib/wpa-signal`, **explicitly**. systemd sets ownership
  on a `StateDirectory` it creates but does not recursively chown a pre-existing one, so the
  unit alone leaves the daemon unable to read its own account.
- Stop the daemon *before* the chown. The window between changing ownership and restarting is
  where an inbound message meets `EACCES` on the account store.
- `/etc/wpa-signal.env` regrouped to `openclaw`. Cosmetic — systemd reads `EnvironmentFile`
  as root before dropping privileges — but "who can read this" should have one answer.
- `ProtectHome=yes` on the unit is *not* in the way, because openclaw's `HOME` is
  `/var/lib/openclaw`. That is the same directive that cost NVB-25 an afternoon in the other
  direction, where the target was under `/run/user`.

#### Config, and the layer that hides

Six paths, all through `openclaw config set --batch-file` (the live file is plain JSON and a
hand edit is stripped on the next write): the two generation models, `timeoutSeconds: 600`,
and `image_generate`/`video_generate` added to `tools.alsoAllow`, `tools.sandbox.tools.allow`
and **the family group's `tools.allow` ceiling**. That last one is the layer that hid a broken
memory path in NVB-23 — every agent-level check reads correct while the only path production
uses resolves narrower.

`timeoutSeconds: 600` is part of the media settings, not a general timeout. Generation is
async and the default run timeout cuts the completion run off mid-flight, which trips an
auth-profile cooldown that breaks *later*, unrelated turns — a symptom that appears nowhere
near its cause.

Session stores were archived and cleared afterwards, because tool policy binds at session
creation.

#### Verified on hardware, 2026-08-14

| Criterion | Evidence |
|---|---|
| Daemon runs at the gateway uid | `ps -o user` → `openclaw`; socket `srwxrwx--- openclaw wpa-signal` |
| The gate is unaffected | `wpa-gate.service` unchanged; `Accepted new client connection 0: UnixDomainPrincipal[user=wpa-gate…]` after restart |
| 8081 is uid-restricted | uid 1000 → dropped, 6s timeout; uid 991 → HTTP 415 in 2.6ms |
| Both agents have the tools | `owner` and `family` both report exactly `apply_patch, edit, image_generate, read, session_status, video_generate, web_search, write` |
| Generation works | `IMG-OK /var/lib/openclaw/.openclaw/media/tool-image-generation/red_bicycle---….jpg` |
| **Delivery works — the whole point** | a **real Signal DM** asking for a picture returned the image in the chat; `run image_generate:2ac96d79…:ok ended with stopReason=stop`. The CLI cannot prove this hop, it has no channel attached |
| Delivery works in the family group too | `@Pi make a picture of a red bicycle` → `Here's a red bicycle:` + attachment, `stopReason=stop`, no delivery error — the room the tools were mostly enabled for |
| `video_generate` end to end | a real Signal DM returned a video; `run video_generate:eb9cd0a9…:ok`. This is what `timeoutSeconds: 600` is for |
| `--receive-mode on-connection` intact | signal-cli stopped for 55s, a DM sent into the gap showed undelivered on the sender's client, and was delivered and answered ~15s after the daemon returned. A restart costs latency, not commands |
| `web_search` did not regress | `SEARCH-OK` from the family agent after the config batch |
| Durable memory survived the session clear | wrote a token through the sandboxed file tools, read it back in a **fresh** session, file present in the real host workspace |
| NVB-25's sandbox invariants hold | both containers `user=0:0 mem=536870912 pids=256 ro=true caps=[ALL] net=none`; the rootful socket still refuses `openclaw` |
| `exec` still denied | probe answers `NO_SHELL_TOOL` |
| `web_fetch` still absent | not in either agent's tool list |
| Survives a cold boot | power-cycled: signal-cli back as `openclaw` with the socket at the right mode, the nftables rule re-applied by its unit, rootless Docker back under linger, gateway `ready`, Waydroid `RUNNING` on the same IP, tool surface unchanged |
| Nothing else broke | Waydroid `RUNNING` on 192.168.240.112; reader, gate, gateway, sandbox containers all active |

#### The outage test passed, and showed the gate missing a message the gateway got

Stopping signal-cli for 55 seconds and sending a DM into the gap did what runbook 03 promised:
the sender's client showed it undelivered, and it was delivered and answered about fifteen
seconds after the daemon came back. `--receive-mode on-connection` is intact under the new uid.

But **the gate never logged that message**, and the gateway did answer it. Two independent
clients attach to signal-cli — the gate on the unix socket, the gateway over HTTP — and the
daemon starts fetching as soon as *any* of them attaches. The gate reconnected 8 seconds after
the HTTP server came up, and the message had already been dispatched. Its `commands.jsonl` has
a hole in it, its drop counters undercount, and ADR 0008's membership-drift refusal cannot
refuse what it never sees. Nothing unauthorised ran — OpenClaw remains the enforcement point —
but "a restart costs latency, not commands" turns out to be true of the gateway and not of the
gate's record. Filed as [NVB-31](https://linear.app/naveh-brenner/issue/NVB-31), with the
mechanism marked inferred: signal-cli logs socket client attach and says nothing about HTTP
clients, so the ordering is the best explanation rather than an observed one.

Also worth keeping, from signal-cli's own startup log:

```
WARN HttpServerHandler - HTTP server has no authentication; Host header is pinned to [localhost, ::1, …]
```

It warns about exactly the hole this change closed. Host-header pinning is not an access
control — the nftables rule is.

#### `requireMention` means the agent's name as text, and a native Signal mention is invisible

Turning `requireMention` on made the group stop answering entirely, which read like the media
change having broken something. It had not.

Signal sends a mention as U+FFFC in the body plus a `mentions` array. The plugin substitutes
it — `renderSignalMentions` replaces the placeholder with `@<uuid>`, the target's **ACI, not
their name** — and gating then matches `\b@?<identity.name>\b` (flag `"i"`) against that text.
`@b0c72586-…` contains no "Pi", so the mention is read and then fails to match. `@Pi …` typed
as ordinary text works.

The miss logs `reason: "no mention"` at **verbose only**, so at a normal log level the gate
says `accepted` and the gateway says nothing at all — the third silent no-op this project has
met, and worth adding to the list of shapes to recognise. Tracked as
[NVB-30](https://linear.app/naveh-brenner/issue/NVB-30); the workaround costs three characters.

**Corrected the same day.** The first version of this entry said the plugin "never reads
`dataMessage.mentions`" and that it discarded the metadata outright. That was wrong, and the
cause is worth recording because it will happen again: `grep "dataMessage.mentions"` does not
match `dataMessage?.mentions`, and optional chaining is what the code uses. A regex whose dot
silently ate the `?` produced a confident negative, and a negative from a search is only as
good as the pattern. The real mechanism is narrower and less damning than the one first
published.

**And it is already fixed upstream**, which the first version also did not check:
`fix(signal): detect native bot mentions in group gating` (#96738, 2026-07-13) adds
`resolveSignalMentionFacts`, comparing each mention against the bot's own account. Present in
`v2026.7.2-beta.7`, absent in `v2026.7.1-2` (ours) and `v2026.6.34`. So nothing was filed
upstream for it — it arrives with an upgrade we will do for other reasons.

**`requireMention` was turned on, and then deliberately back off.** The live family group
carried `false` while the example config argued for `true`, so it was flipped to `true` before
testing media in that room. That is what surfaced the mention finding above. It is now `false`
again, on purpose: that room exists only to talk to the assistant, so every message in it is
addressed to it and a mention requirement is friction with no disclosure benefit. The example
config now states both the default and this deployment's exception, rather than quietly
disagreeing with the box.

#### Unflattering, again: the known-phrase grep found a real leak, and it is not ours

Runbook 04's "no message content in logs" check is the last item on the pre-live list and it
is usually a formality. Run properly this time — with a phrase we knew was in a real message,
because the message asked for a picture of something specific — it found the gateway logging
the assistant's **reply text in plaintext** to journald:

```
openclaw[13262]: Here's a clean pfp for Pi — sleek π + claw vibe in neon.
openclaw[13262]: Attachment: /var/lib/openclaw/.openclaw/media/tool-image-generation/image-1---4a8b….jpg
```

`signal-cli` and `wpa-gate` are both clean — the two units this project hardened do their
job. The one it adopted does not. This is **pre-existing**, dating to the ADR 0012 runtime
switch, and unrelated to the media tools; the media work only supplied the test case, because
"grep the journal for a known phrase" needs a phrase you actually know. Filed as
[NVB-29](https://linear.app/naveh-brenner/issue/NVB-29) rather than fixed here: the schema has
no switch for it, and the plausible workaround (`consoleLevel: "warn"`) would also take
`[gateway] ready`, the tool-policy lines and the model-fetch errors with it.

Two smaller leaks in the same family, recorded there: **generated filenames are derived from
the prompt** (`red_bicycle---<uuid>.jpg`, so any log line naming a media path carries a
fragment of the request — NVB-27's original `AttachmentInvalidException` did), and
`openclaw agent -m "…"` prints the reply into the gateway's journal while `sudo` logs the
command line, so **probes on this box must use neutral text**.

The lesson is about the check, not the bug: this item had been ticked before on the strength
of there being nothing obvious in the log, which is not the same as having looked for
something specific.

**The Pi's WiFi dropped repeatedly during this work**, taking SSH and the uplink with it. It is
worth recording for two reasons. First, signal-cli's failure mode looks alarming and is not:
the startup account check exits `3/NOTIMPLEMENTED` with `Error while checking account …:
Closed unexpectedly`, and `Restart=on-failure` retries until the link returns. The same line
appears in the journal for 2026-08-11 17:26, the minute the backup's DNS also failed.

Second, the diagnosis is worth keeping, because "the WiFi is flaky" was wrong twice before it
was right. The box was idle throughout (load 0.00, 5.8 GB free, no swap, `throttled=0x0`,
48.8 °C, no OOM, no `brcmfmac` errors) — so it was not memory pressure starving `sshd`, which
was the second guess. What `iw` actually showed: **power save on**, associated on **2.4 GHz**
(ch 4) despite the radio supporting 29 channels above 5 GHz, `-61 dBm`, `tx failed: 880`, and
a **receive rate pinned at 1.0 Mbit/s** against 65 Mbit/s outbound. That combination explains
the odd signature exactly — single ICMP pings returning at 26 ms while a TCP handshake times
out completely. Other devices on the same SSID were fine because a mesh AP had steered them to
5 GHz.

Resolved by plugging in **eth0**, which had been down since the box was built. It is now the
default route (metric 100 against wlan0's 600), and the uplink went to 12 ms / 0% loss. Worth
doing on principle: this is a server that never moves, running Waydroid, a JVM, Docker and a
gateway, and its link had been the least reliable thing about it.

### Changed — the sandbox daemon runs as the gateway now (NVB-25, 2026-08-14)

The `openclaw` uid is **out of the `docker` group**, and the rootful daemon is stopped,
disabled and masked. Docker still backs `sandbox.mode: "all"` — it is just a **rootless**
daemon owned by `openclaw` itself, so reaching its socket grants exactly what the gateway
already had and nothing more. ADR 0006's uid split is no longer bypassable from the
process that parses untrusted Signal input.

What the move actually needed, all of it on hardware:

- **Debian's own `docker.io` ships the rootless installer** at
  `/usr/share/docker.io/contrib/dockerd-rootless-setuptool.sh`. No third-party apt repo,
  no `docker-ce`. `rootlesskit`, `slirp4netns` and `fuse-overlayfs` are in bookworm main.
  The installer must be run with that `contrib` directory **on `PATH`** — it looks up its
  sibling `dockerd-rootless.sh` by name and otherwise aborts. It then fails at its last
  step (`$BIN/docker version`, and there is no `docker` in `contrib`) *after* the daemon
  is up, so `systemctl --user enable docker` has to be run by hand.
- **Storage is `overlay2`, not `vfs`.** Kernel 6.12 does unprivileged overlayfs, so the
  rootless daemon gets native overlay2 and no full-copy layers on an SD card.
  `fuse-overlayfs` is installed as a fallback and is not being used.
- **cgroup delegation was already correct.** `user-991.slice` carries `cpu memory pids`,
  so `sandbox.docker.memory: "512m"` still binds — `docker info` prints no "No memory
  limit support" warning, and the container reads `memory.max = 536870912`. The
  controllers that are *not* delegated are `cpuset` and `io`, neither of which this
  config uses.
- **The socket had to move off `$XDG_RUNTIME_DIR`.** `wpa-openclaw.service` sets
  `ProtectHome=yes`, which makes `/run/user` inaccessible, and a `BindPaths=/run/user/991`
  hole punched back through it **did not survive** — the gateway saw `EACCES` on a socket
  that exists. Weakening the hardening to reach the socket would invert the point of the
  issue, so the daemon listens on `/var/lib/openclaw/docker.sock` instead, via a drop-in
  on the user-level `docker.service`. That path is outside every `Protect*` the unit sets.
- **`sandbox.docker.user: "0:0"` is new, and is only safe because we went rootless.** The
  sandbox image runs as `sandbox` (uid 1000); under rootless that maps to a subuid with no
  claim on the workspace, so the bind mount was writable in name only —
  `touch /workspace/x` → `Permission denied`. Container uid 0 maps to host uid 991, which
  *is* `openclaw`, so files land owned by the gateway user exactly as before. Under the
  rootful daemon the same line would have been real root on a host bind mount.
- **Waydroid is untouched, and now structurally rather than by configuration.** The
  rootful daemon's `/etc/docker/daemon.json` (`iptables: false`, `ip6tables: false`,
  `bridge: none`) was what kept dockerd's firewall rules away from Waydroid. A rootless
  daemon does its networking inside its own netns, so it cannot touch the host firewall at
  all. Verified after the move: `FORWARD` still `ACCEPT`, no `docker0`, Waydroid `RUNNING`
  on 192.168.240.112. `daemon.json` is kept for the masked daemon, in case it is ever
  unmasked.
- **The rootful daemon is masked, not purged.** `docker.io` is one package holding both
  client and daemon, and OpenClaw shells out to `docker` on `PATH` — removing the package
  would remove the interface the gateway needs.

**Survives a reboot** — tested rather than assumed, since linger and a user-level unit are
exactly the parts that come back wrong. After a cold boot: the rootless daemon is active
under `user@991.service`, the socket is there, the gateway reaches it, an agent turn
creates its container, and Waydroid is `RUNNING` on the same IP with the reader, gate and
signal-cli all active.

One trap the reboot exposed, which will otherwise be mistaken for a regression: an
`openclaw agent` invocation made **before the gateway finishes starting** (~50s after
boot) falls back to an embedded local run, and that process has no `DOCKER_HOST` in its
environment. It fails closed — *"Sandbox mode requires Docker"* — but the error names
`/var/run/docker.sock`, i.e. the rootful socket that is deliberately gone. Nothing is
broken; the gateway simply was not up yet. **Read the socket path in that error before
concluding anything**: `/var/run/docker.sock` means the caller was not the gateway.

NVB-23's acceptance was re-run against the rootless daemon and passes unchanged:
container created per agent (`openclaw-sbx-agent-{owner,family}-*`), `user=0:0`,
`memory=536870912`, `pids=256`, `ReadonlyRootfs=true`, `CapDrop=[ALL]`, `network=none`;
a file written by the agent appeared in the real host workspace owned by uid 991 and read
back in a **fresh** session; the `exec` probe answers `NO_SHELL_TOOL`; the two agents get
separate containers with separate workspace binds. `sudo -u openclaw docker ps` against
`/var/run/docker.sock` now fails with `permission denied`, which is the whole point.

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

**`image_generate` and `video_generate` are NOT enabled, and the reason is the uid
split rather than anything about the tools.** Both work: they register, generate
correctly in ~90s inside the sandbox, and the completion run ends with
`stopReason=stop`. The failure is the last hop, found by a real Signal request:

```
Signal RPC -32603: Failed to send message: …/media/outbound/image-….jpg
  (Permission denied) (AttachmentInvalidException)
```

OpenClaw hands signal-cli a *path* under `~/.openclaw/media/outbound`. Our signal-cli
runs as `wpa-signal` under our own unit (runbook 03, for `--scrub-log` and `UMask=0007`)
and cannot traverse that `0700` chain. The file itself is world-readable; the path is
not.

**A group grant does not survive.** Granting `wpa-signal` traverse-only on `.openclaw`
and `media` plus read on `outbound` works for exactly as long as it takes to generate
one more image: **OpenClaw re-asserts `0700` on `media` on every generation** — measured
directly, `0710` before a run and `0700` after. Any chmod is undone before the send, and
a watcher would race it. Reverted rather than left in place, since traversal into the
agent state directory with no working capability is pure downside.

So OpenClaw assumes it owns the Signal transport at its own uid, and
[ADR 0006](docs/decisions/0006-two-process-privilege-split.md)'s split is what makes
that untrue here. Shipping the tools anyway would mean shipping something that always
fails at the last step, in a family group, after appearing to work. Tracked in NVB-26.

Two settings recorded there for whenever it is solved: media generation needs
`agents.defaults.timeoutSeconds` raised (it is async, and the default cuts the
completion run off, which trips an auth-profile cooldown that breaks *later*,
unrelated turns), and the media models must be named or the tools never register.

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

The same mistake then repeated on the media tools, which is why it is worth writing
down twice. `image_generate` was reported here as broken, with a confident cause
(`400 Could not decrypt the provided encrypted_content`). That error fired once and never
reproduced, including in a control built to trigger it. The real behaviour was mundane —
async task, ~90s, cut off by the default run timeout — and **the test method was breaking
the thing under test**: restarting the gateway between probes killed detached tasks
mid-flight, and the probe expected a synchronous answer from a tool the shipped docs
describe as asynchronous. Two wrong causes were published before the boring one was
found. **An error seen once is a lead, not a diagnosis.**

Also found: **the two `alsoAllow` keys fail in opposite directions.**
`pickSandboxToolPolicy` unions against a wildcard, so `tools.sandbox.tools.alsoAllow`
with no `allow` becomes `["*", …extra]` — "allow everything plus these". The shipped
config uses an explicit `allow` there instead.

**Top-level `tools.alsoAllow` is safe**, checked afterwards because we depend on it for
the file tools: `mergeAlsoAllowPolicy` returns the policy *unchanged* when no allow list
exists, and otherwise appends — never a wildcard. `mergeConfiguredSubagentAllow` agrees
(`allow && alsoAllow ? union : allow`). So the global policy is exactly as narrow as it
reads: `profile: "minimal"` resolves to an allow list and `alsoAllow` appends to it, which
the observed six-tool surface confirms.

Same key name, same file, opposite failure direction. That asymmetry is the thing to
remember, not either behaviour on its own.

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
  **Superseded the same day by NVB-25 above: the daemon is rootless and the group
  membership is gone.**

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
