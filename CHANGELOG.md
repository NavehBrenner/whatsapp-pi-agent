# Changelog

Notable changes to this project, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning will follow
[SemVer](https://semver.org/) once there's something to version.

This project is pre-release, so `Unreleased` is where everything lives for now.

Findings verified on hardware are recorded here as well as in the ADRs, because
several of them are the kind of thing that costs an evening to rediscover.

## [Unreleased]

### Added

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

- Sender names resolve for ~5% of group messages; the reader falls back to the
  LID rather than failing.
- The reader must be run by hand — no timer, no confinement yet.
- No Signal channel, no agent. The system reads but cannot yet be talked to.
