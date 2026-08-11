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

## Q4 — OpenClaw vs. a custom Agent SDK build

**Status:** Open. **Blocks:** [runbook 04](runbooks/04-agent-deploy.md), not the spike.

Two ways to build the agent processes.

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

Lean: **custom Agent SDK build.** Capability shaping is the load-bearing control in this
system and it's not something to inherit from a framework whose defaults are aimed at
general usefulness. Revisit if the amount of Signal/session plumbing turns out to dwarf the
agent logic.
