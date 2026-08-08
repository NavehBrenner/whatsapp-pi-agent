# whatsapp-pi-agent

A personal AI assistant that **reads** my WhatsApp group chats for context and that I
**control over Signal**. It drafts emails, manages my calendar, does web research, and
sends me reminders.

It never writes to WhatsApp.

## Status

**Pre-spike.** Docs and decisions are written. No `src/` yet — deliberately.

The entire architecture rests on one unverified assumption: that the official WhatsApp
Android app runs acceptably in Waydroid on a Pi 5 **and** will complete a companion-device
link. Play Integrity / container-detection behaviour in that combination is unknown to us.

So the order of work is:

1. Execute [`docs/runbooks/02-waydroid-whatsapp.md`](docs/runbooks/02-waydroid-whatsapp.md)
   manually on the Pi. ← **you are here**
2. If it passes, write `src/`.
3. If it fails, the architecture changes fundamentally and most of `docs/decisions/` is
   reopened. Don't write code against it before then.

See [`docs/OPEN-QUESTIONS.md`](docs/OPEN-QUESTIONS.md).

## Shape of it

```
phone (primary)  ──companion link──▶  WhatsApp in Waydroid on the Pi
                                              │
                                     msgstore.db (plain SQLite)
                                              │
                                       unprivileged reader  ── JSON ──┐
                                       (no tools, no net)             │
                                                                      ▼
        me ◀──── Signal (signal-cli) ────▶  privileged agent  ──▶  Gmail draft
                                            (fresh session,        Calendar
                                             per command)          web
```

Two processes, no shared context. The reader sees untrusted WhatsApp content and has no
capabilities. The agent has capabilities and is only ever triggered by me.

Full design: [`docs/architecture.md`](docs/architecture.md).
Why it's shaped that way: [`docs/threat-model.md`](docs/threat-model.md) and
[`docs/detection-model.md`](docs/detection-model.md).

## Quickstart

Nothing to run yet. To get to the spike:

```bash
# on the Pi (64-bit Pi OS, Pi 5, 8GB)
git clone <this repo> ~/whatsapp-pi-agent
cd ~/whatsapp-pi-agent
./deploy/bootstrap.sh          # installs Waydroid + prerequisites, idempotent
```

Then follow the runbooks in order:

| Runbook | What |
|---|---|
| [01-pi-base-setup](docs/runbooks/01-pi-base-setup.md) | OS, storage, ssh, unattended upgrades |
| [02-waydroid-whatsapp](docs/runbooks/02-waydroid-whatsapp.md) | **The spike.** Waydroid + WhatsApp + companion link |
| [03-signal-cli](docs/runbooks/03-signal-cli.md) | Dedicated number, signal-cli daemon |
| [04-agent-deploy](docs/runbooks/04-agent-deploy.md) | Agent processes + systemd (post-spike) |

## Development

Repo is canonical on WSL (`~/projects/whatsapp-pi-agent`). Windows-side mounting was
rejected — UNC paths didn't work with Cowork. `core.autocrlf` is set to `input`; the
`.gitattributes` pins `eol=lf` so shell scripts survive the round trip to the Pi.

Deploy to the Pi via `git pull` or `rsync -a --delete` over SSH.

## Prior art

- [`B16f00t/whapa`](https://github.com/B16f00t/whapa) — WhatsApp forensics toolkit, msgstore parsing.
- [`andreas-mausch/whatsapp-viewer`](https://github.com/andreas-mausch/whatsapp-viewer) — has the msgstore schema committed as SQL, useful for pinning.
