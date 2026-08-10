# 0002 — Run the official WhatsApp app in Waydroid as a companion device

**Status:** Accepted, **pending spike verification**
**Related:** [0001](0001-reject-whatsapp-cloud-api.md), [0003](0003-local-db-read.md), [detection-model.md](../detection-model.md), [OPEN-QUESTIONS Q1](../OPEN-QUESTIONS.md)

## Context

With the Cloud API ruled out ([0001](0001-reject-whatsapp-cloud-api.md)), we need a real
WhatsApp client running unattended on a Pi 5 that receives my group messages.

Options considered:

| Option | Verdict |
|---|---|
| Reimplemented client (Baileys, whatsapp-web.js) | Meta fingerprints the Noise handshake and maintains a DB of known unofficial clients. Flagged before a message is sent. |
| Headless browser driving WhatsApp Web | WhatsApp reads `navigator.webdriver`; headless Chrome diverges on fonts/WebGL/permissions. ~12 reported bans in 30 days. Also: whatsapp-web.js *is* this, so it's one option not two. |
| Frida / LSPosed hooking the real app | Anti-tamper scans memory maps for frida/xposed, checksums code, detects inline hooks via prologue scanning. Hardest-banned category. Obfuscated names churn every release. And it returns data already in the DB — strictly worse than [0003](0003-local-db-read.md). |
| WhatsApp Desktop on the Pi | No official Linux build, x86 or ARM. Pi-Apps "WhatsApp" is a Chromium wrapper = a linked device again. |
| Windows 11 ARM VM under QEMU, then x64 emulation for Desktop | Two translation layers on a Pi. Unusable. |
| Android emulator (QEMU/AVD) on the Pi | ARM-on-ARM helps, but full-system emulation still costs heavily and presents strong emulator tells. |
| **Waydroid + official APK, companion device** | **Chosen.** |

Waydroid runs Android in an LXC container sharing the host kernel. On a Pi 5 (ARM64) the
guest runs ARM code natively — no instruction translation, near-native performance. It is a
container, not an emulator, so it presents far fewer emulator indicators than a QEMU AVD.

WhatsApp's **companion device** feature (up to 4 devices, QR-scan pairing) lets a secondary
client receive full message flow while the phone stays primary. This is a supported product
feature used as designed.

## Decision

Run the **official, unmodified WhatsApp APK** inside **Waydroid** on the Pi 5, linked to my
phone as a **companion device**. Phone stays primary.

Pin the APK version (see [OPEN-QUESTIONS Q2](../OPEN-QUESTIONS.md)) and update deliberately,
not automatically.

**This decision is gated on a spike** — [runbook 02](../runbooks/02-waydroid-whatsapp.md) is
executed manually before any `src/` code is written. It is unverified whether WhatsApp will
run acceptably in Waydroid on a Pi 5 and complete companion pairing there.

## Consequences

**Accepted:**

- **History is bounded to roughly 6 months.** Measured after pairing (2026-08-10): backfill
  completed at ~42,000 messages across 561 chats, but the distribution shows history
  effectively begins **February 2026** — 381 messages total for 2020–2025 versus 41,636 for
  2026. Companion devices receive a bounded sync window, not the full archive.

  Consequence for the product: the assistant can never answer "what did we agree last
  year." Its context is the last ~6 months, growing forward from the pairing date. If deeper
  history is ever needed it must come from a phone backup export, which is a separate
  mechanism and out of scope here.

  Backfill itself took roughly one hour and then stopped cleanly; after that all inserts are
  live traffic.
- Operational weight: a container to keep running, an Android session that can get logged
  out, a UI that occasionally needs a human (updates, permission prompts, re-pairing).
- Companion devices can be unlinked from the phone, including accidentally. Recovery is a
  manual re-pair.
- Companion sessions expire if the primary device is offline for ~14 days.
- One of my 4 companion slots is consumed permanently.
- Pinning the APK means deliberately running an outdated client, with whatever
  server-side forced-upgrade deadline WhatsApp eventually applies.

**Gained:**

- Avoids detection layers L1, L2, and L3 entirely (see [detection-model.md](../detection-model.md)) —
  by construction, not by evasion. Real client, real handshake, no browser, no hooks.
- Full group message flow, which no API path provides.
- A real Android filesystem on the host, which is what makes [0003](0003-local-db-read.md)
  possible.
- A place to run the notification-bridge APK.

**Open:**

- L6 (container/emulator detection, Play Integrity) is the one detection layer we have no
  answer for. The spike is exactly this question.
