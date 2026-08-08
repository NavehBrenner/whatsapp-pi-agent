# Open questions

Things not yet decided or not yet verified. Q1 blocks everything.

---

## Q1 — Does WhatsApp run in Waydroid on a Pi 5, and will companion pairing succeed?

**Status:** UNVERIFIED. **Blocks:** all of `src/`.

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
