# 0003 — Read msgstore.db directly from the host

**Status:** Accepted
**Related:** [0002](0002-waydroid-companion-device.md), [architecture.md](../architecture.md)

## Context

WhatsApp is running in a container ([0002](0002-waydroid-companion-device.md)). The messages
need to reach host-side code. How?

**The key fact:** the live on-device database is **plain, unencrypted SQLite**. The
`.crypt14` encryption that dominates every search result applies to *backups* — the files
written to `/sdcard/WhatsApp/Databases/` and uploaded to Google Drive. The working database
at `/data/data/com.whatsapp/databases/msgstore.db` has no application-layer encryption at
all.

The only thing protecting it on a normal phone is the Android app sandbox — UID-based
filesystem isolation. In an LXC container whose rootfs the host owns, that protection is
bypassed by reading the file as root. There is nothing to break and nothing to extract.

This kills the "extract the encryption key" instinct on two counts: there is no key to
extract for the live DB, and any key-extraction technique is an in-process attack against
WhatsApp's anti-tamper (detection layer L3) to obtain access we already have.

Alternatives considered:

- **ADB pull from the container** — works, but requires a debuggable path into app-private
  storage and adds a moving part. Direct filesystem read is simpler.
- **Accessibility service scraping the UI** — lossy, brittle, fires only on visible chats,
  and touches WhatsApp's UI surface. Strictly worse.
- **Notification content as the feed** — see below; rejected as a data source.
- **Frida hooking the message pipeline** — L3, and returns exactly what the DB already has.

## Decision

Read the container's filesystem directly from the host, **read-only**:

**Verified on hardware 2026-08-10.** The real path is under the *session user's home*, not
`/var/lib/waydroid` as originally written:

```
~/.local/share/waydroid/data/data/com.whatsapp/databases/
```

(`/var/lib/waydroid` holds images, rootfs and overlays; Android's `/data` is bind-mounted
from the user's home. Confirm with `waydroid shell -- df | grep /data` if it moves again.)

| File | Use |
|---|---|
| `msgstore.db` (+ `-wal`, `-shm`) | messages, chats, JIDs |
| `wa.db` → `wa_contacts` | contact names, joined on JID |

Verified contents on a live companion-paired install: 13,428 messages, 576 chats (184 of
them groups), 1,563 contacts. Schema generation is `message` (not the pre-2021 `messages`).
Opens read-only with no key.

Two implementation requirements, both non-optional:

**1. Contact names require `wa.db` — and that is not sufficient.** `msgstore.db` stores JIDs,
not names. Names live in `wa.db`'s `wa_contacts`, joined on JID. But measured on real data,
**modern WhatsApp identifies most group participants by LID** (`...@lid`), not by phone JID:

| jid server | rows |
|---|---|
| `lid` | 18,287 |
| `s.whatsapp.net` | 9,698 |
| `g.us` | 248 |

LIDs do not join to `wa_contacts.jid`. `msgstore.lid_display_name` (keyed on
`lid_row_id` → `jid._id`) maps some of them, and `wa_contacts.wa_name` (profile name) is
better populated than `display_name` (1000 vs 411 rows) — but combined coverage is only
**~4.7% of received messages** (50% if restricted to `s.whatsapp.net` senders).

Sender-name resolution for LID participants is unsolved — see
[OPEN-QUESTIONS Q5](../OPEN-QUESTIONS.md). It affects output quality, not feasibility.

**2. Read WAL-aware.** WhatsApp holds the DB open with WAL journaling; recent messages live
in `msgstore.db-wal`, not yet in the main file. Either open read-only with the `-wal`/`-shm`
files present and let SQLite replay the log, or snapshot all three files together and read
the copy.

`immutable=1` is **wrong** here — it tells SQLite to ignore the WAL and silently returns
stale data. That failure looks like "the agent is a few minutes behind" and is unpleasant
to diagnose.

Never open the live DB read-write. Never let the reader hold a write lock on a file
WhatsApp depends on.

**3. The cursor MUST be `message._id`, never `timestamp`.** Measured on live data
2026-08-10, and this one will bite silently if got wrong.

Companion devices receive messages substantially later than they were sent — **808 of 812**
messages with a populated `received_timestamp` arrived more than 60s after their
`timestamp`. Observed worst case in a 20-minute window: 823s (nearly 14 minutes).

Because of that, **rows are not inserted in `timestamp` order**:

| `_id` | `timestamp` (sent) | `received_timestamp` |
|---|---|---|
| 45210 | 16:25:10 | 16:38:53 |
| 45209 | 16:38:53 | 0 |

The highest `_id` has an *older* `timestamp` than the row before it. A reader using
`WHERE timestamp > last_seen` would process 45209, then silently drop 45210 forever —
intermittent, unreproducible message loss that looks like a WhatsApp fault.

`received_timestamp` is not a usable cursor either: it is `0` on most rows.

Use `WHERE _id > cursor ORDER BY _id` — SQLite's rowid, monotonic with insertion. Keep
`timestamp` for display and ordering *within* a batch only.

**Notifications are a doorbell, not the feed.** A `NotificationListenerService` APK inside
Waydroid tells the host *that* something happened; the host then queries the DB for
everything after its cursor. Notification payloads are never used as content, because
Android collapses them under volume ("3 new messages"), muted chats may not fire at all,
and the text is truncated. The DB is ground truth; a slow periodic sweep covers missed
doorbells so correctness never depends on one arriving.

## Consequences

**Accepted:**

- **Schema is unversioned and undocumented.** It migrates occasionally (`messages` →
  `message` around 2021) — not per-release, but without warning. Mitigated by pinning the
  APK and updating deliberately. See [OPEN-QUESTIONS Q2](../OPEN-QUESTIONS.md).
- Reading app-private storage from the host means the reader runs privileged enough to read
  the container rootfs. Confined at the systemd level (no network, read-only mounts, no
  credentials) — see [threat-model.md](../threat-model.md).
- Media is stored as files referenced by path, not inline. Out of scope for v1; text only.
- Deleted-for-everyone messages leave a tombstone row. We may see the fact of a deletion.
- Real message content — including other people's — lives on the Pi. Handled as an
  obligation in [threat-model.md](../threat-model.md) R4.

**Gained:**

- Nothing touches the WhatsApp process. Zero exposure to detection layer L3.
- Complete, structured, exact message history — better data than any scraping or
  notification-based approach would give.
- No key extraction, no decryption, no cryptography in this project at all.

## Prior art

- [`B16f00t/whapa`](https://github.com/B16f00t/whapa) — WhatsApp forensics toolkit; the
  msgstore/wa.db parsing is directly relevant.
- [`andreas-mausch/whatsapp-viewer`](https://github.com/andreas-mausch/whatsapp-viewer) —
  has the msgstore schema committed as SQL, useful as a reference point when pinning.
