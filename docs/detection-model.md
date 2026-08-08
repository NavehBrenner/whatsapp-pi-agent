# Detection model

How Meta detects WhatsApp automation, layer by layer, and where this design sits relative
to each layer.

This is not a document about evasion. It's a document about **not being in the detected
category in the first place** — which is a different and much more durable position. Every
technique that requires winning an arms race is a technique we rejected.

## The layers

### L1 — Protocol / client fingerprinting

WhatsApp's transport is a Noise Protocol handshake carrying a proprietary binary XMPP-like
protocol. The handshake includes client version, platform, and build metadata, and its exact
byte-level shape differs between implementations.

Meta fingerprints this and maintains a database of known unofficial clients. A reimplemented
client — Baileys, whatsapp-web.js, and their derivatives — is identifiable **during the
handshake, before a single message is sent.** Library authors track cat-and-mouse changes
in their issue trackers as a routine part of maintenance; that's the shape of an arms race
you lose eventually.

**This design:** runs the official APK, unmodified. The handshake is the real client's
handshake because it *is* the real client. Nothing to fingerprint.

### L2 — Web-client automation detection

WhatsApp Web is a normal web app and applies normal bot detection:

- `navigator.webdriver` is set by WebDriver-controlled browsers and is directly readable.
- Headless Chrome diverges from headful on font enumeration, WebGL renderer strings,
  `Notification.permission`, plugin arrays, and a long tail of similar surfaces.
- Behavioural signals: perfectly uniform message timing, no idle periods, instant
  read-receipts, 24/7 presence with no diurnal pattern.

Reported bans in the ~12-per-30-days range for headless-browser setups.

Worth stating explicitly because it collapses two "alternatives" into one: **whatsapp-web.js
is itself Puppeteer driving WhatsApp Web.** "Use a headless browser instead of the library"
and "use the library" are the same thing wearing different hats, and both land in L2.

**This design:** no browser is involved anywhere.

### L3 — Anti-tamper inside the app

WhatsApp's Android client actively defends its own process:

- Scans `/proc/self/maps` and open file descriptors for `frida`, `xposed`, `substrate`,
  and gadget libraries.
- Checksums its own code sections.
- Detects inline hooks by scanning function prologues for trampolines.
- Verifies APK signature and installer package at runtime.

Modified and hooked clients are the **hardest-banned category** — this is where account
bans are most aggressive, because it's unambiguous.

There's also a plain engineering argument against hooking, independent of detection: it
targets obfuscated symbol names that churn every release, so it breaks constantly. And what
it returns — message content — **is already sitting in `msgstore.db` in plaintext.** It is
strictly more fragile, strictly more detectable, and returns strictly nothing extra.

**This design:** nothing touches the WhatsApp process. The APK is stock and unsigned-by-us.
Reading the database happens entirely outside the app, from the host, while the app is
oblivious. The `NotificationListenerService` bridge is a *separate app* using a documented
Android API; it never interacts with WhatsApp's process at all.

Note that L3 is why "just extract the encryption key" is the wrong instinct: it's an
in-process operation against an anti-tamper target, to obtain access to data that requires
no key when read from the host.

### L4 — Account behaviour

Server-side heuristics on account activity:

- Message volume and send rate.
- Ratio of outbound to inbound.
- Unsolicited messages to non-contacts.
- Bulk/identical message content.
- Reports from recipients — a strong signal, and the one that turns a suspicion into a ban.
- New-account or newly-registered-number patterns.

**This design:** sends nothing. Ever. Outbound volume is zero, unsolicited messages are
zero, and the recipient-report signal is structurally unreachable because there are no
recipients. The account looks like a person with a phone and a linked device, because that
is what it is.

This is the layer that [ADR 0005](decisions/0005-no-whatsapp-write-path.md) buys outright,
and it's the reason "no write path" is worth more than any amount of careful rate limiting.

### L5 — Connection metadata

- Datacenter IP ranges (AWS, GCP, Oracle, Hetzner, DigitalOcean) are flagged at the
  connection level. A residential consumer account connecting from an ASN that belongs to a
  cloud provider is anomalous on its face.
- Geographic inconsistency between primary and companion devices.
- Device/registration churn.

**This design:** a Pi on my home broadband. Residential IP, same city and ASN as my phone,
stable over time.

This is exactly why the free-cloud-VM option was rejected. Oracle Always Free looked
attractive on cost, but a datacenter IP reintroduces a connection-level flag that the rest
of the design goes out of its way to avoid — and it would be the *only* anomalous signal in
an otherwise clean profile, which makes it more conspicuous, not less. (Oracle also halved
Always Free to 2 OCPU / 12GB with scarce ARM capacity, so the practical case evaporated
too.)

### L6 — Container / emulator detection by the app

Does WhatsApp itself detect that it's running in Waydroid?

Android apps can check for emulator/container indicators: `ro.product.model`,
`ro.hardware`, `ro.build.fingerprint`, the presence of `qemu` props, missing telephony
stack, absent sensors. And Play Integrity API attestation is the industrial version of the
same question, with `MEETS_DEVICE_INTEGRITY` failing on most non-certified images.

**This is the one layer where our position is unverified.** Two mitigating facts:

- Waydroid is a *container*, not an emulator — it shares the host kernel and runs ARM code
  natively on ARM hardware. It presents far fewer emulator tells than QEMU-based setups.
- WhatsApp historically registers and runs on non-GMS and custom-ROM devices. It is not
  known to hard-gate on Play Integrity for basic messaging.

But "not known to" is not "verified." Companion-device pairing specifically may apply
different checks than fresh registration, and we haven't tested it.

**This is the spike.** See [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) Q1 and
[runbook 02](runbooks/02-waydroid-whatsapp.md). No code gets written until it's answered.

## Summary

| Layer | Signal | Our exposure |
|---|---|---|
| L1 protocol fingerprint | reimplemented client | **none** — official APK |
| L2 web automation | webdriver, headless tells | **none** — no browser |
| L3 anti-tamper | frida/xposed/patched APK | **none** — nothing touches the process |
| L4 account behaviour | send volume, reports | **none** — sends nothing |
| L5 connection metadata | datacenter IP | **none** — residential |
| L6 container detection | emulator props, Play Integrity | **UNVERIFIED — the spike** |

Five of six layers are avoided by construction rather than by evasion. The sixth is a
binary unknown that gets tested by hand before anything is built on it.

## Rejected approaches and which layer kills them

**WhatsApp Cloud API** — not a detection problem, a capability problem. Groups API caps at
8 participants; gated to businesses with 100k+ monthly conversations; Meta banned
open-ended AI assistant bots on the Business Platform in Jan 2026; service messages inside
the 24h window become billable Oct 1, 2026. Structurally cannot do this.
→ [ADR 0001](decisions/0001-reject-whatsapp-cloud-api.md)

**Baileys / whatsapp-web.js** — L1 (and L2 for web.js, which is Puppeteer underneath).

**Headless / agentic browser** — L2.

**Frida / LSPosed hooking WhatsApp** — L3, the hardest-banned category, for data already
available unencrypted from the host.

**WhatsApp Desktop on the Pi** — doesn't exist: no official Linux build, x86 or ARM. The
Pi-Apps "WhatsApp" entry is a Chromium wrapper around WhatsApp Web, i.e. a linked device
again, plus L2.

**Windows VM on the Pi** — Win11 ARM under QEMU, then x64 emulation for the WhatsApp
Desktop binary. Two layers of translation on a Pi. Unusable before detection is even
relevant.

**Free cloud VM (Oracle etc.)** — L5.

Do not revisit these. Each was evaluated and each fails for a structural reason, not a
tuning reason.
