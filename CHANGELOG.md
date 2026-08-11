# Changelog

Notable changes to this project, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow
[SemVer](https://semver.org/) once there's something to version.

This project is pre-release, so `Unreleased` is where everything lives for now.

Findings verified on hardware are recorded here as well as in the ADRs, because
several of them are the kind of thing that costs an evening to rediscover.

## [Unreleased]

### Changed

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
