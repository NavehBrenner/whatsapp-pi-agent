# Changelog

Notable changes to this project, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow
[SemVer](https://semver.org/) once there's something to version.

This project is pre-release, so `Unreleased` is where everything lives for now.

Findings verified on hardware are recorded here as well as in the ADRs, because
several of them are the kind of thing that costs an evening to rediscover.

## [Unreleased]

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
