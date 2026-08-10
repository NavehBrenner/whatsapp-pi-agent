# Working on this repo

Conventions for anyone — human or agent — changing this codebase.

## Changelog

**Every PR that changes behaviour updates [`CHANGELOG.md`](CHANGELOG.md)** in the
`Unreleased` section.

Do it in the PR, not afterwards. A changelog reconstructed from `git log` six
months later is a list of commit subjects, not an account of what changed and
why it mattered.

Record the unflattering findings too — a limitation discovered on hardware is
worth more to the next reader than another line about a feature landing.

## `.local/` — private context, never committed

`.local/` is gitignored. It holds machine-local knowledge that a new session
needs but a public repo must not carry: which Linear project this repo maps to,
how to reach the Pi, scratch notes mid-investigation.

**Read `.local/` at the start of a session.** It is the answer to "which Linear
project is this?" and "what's the Pi's address?" without having to ask.

Current contents:

| File | What |
|---|---|
| `.local/linear.md` | Linear workspace, project, milestones, current issue |
| `.local/pi.md` | Pi address, SSH alias, paths, running services |

Keep it small and current. Anything that turns out to matter to the project
rather than to this machine belongs in the repo proper — an ADR, the changelog,
or an issue. `.local/` is for coordinates, not decisions.

**Never put secrets here.** It's untracked, not encrypted, and sits in a
directory you might one day copy somewhere. Credentials belong in systemd
`LoadCredential=` or a root-owned `0600` file outside the repo.

## Branch and PR flow

`main` is protected. It applies to admins, so there is no bypass:

- PR required; direct pushes are rejected
- CI check `check` must pass
- Branch must be up to date with `main`
- Linear history; no force-push, no branch deletion

## CI

`mypy` (strict) and `pytest`, both on **Python 3.11** — matching the Pi, not the
dev box. Testing on a newer Python than we deploy on would hide real breakage.

The mypy config is deliberately aggressive: `strict = true` plus
`disallow_any_explicit`, `disallow_any_unimported`, `disallow_any_decorated`,
`warn_unreachable`. This code sits at a trust boundary handling untrusted input,
so "we don't know the type here" should fail the build rather than pass quietly.

If mypy blocks you, add a narrow per-module override with a comment explaining
why. Don't loosen the global config.

Run locally before pushing:

```bash
python3 -m venv .venv && .venv/bin/pip install mypy pytest
.venv/bin/mypy && .venv/bin/pytest
```

## Dev environment

The repo is canonical on WSL (`~/projects/whatsapp-pi-agent`). Windows-side
mounting was rejected — UNC paths didn't work with Cowork. `core.autocrlf` is
`input` and `.gitattributes` pins `eol=lf`, so shell scripts survive the round
trip to the Pi.

Deploy to the Pi with `git pull` or `rsync -a --delete` over SSH.

## Invariants — changing these means reopening an ADR

These aren't style preferences. Each one is load-bearing, and each was either
argued for in an ADR or paid for with hardware debugging.

**No write path to WhatsApp.** There is no `send_whatsapp` tool, disabled or
otherwise. This is what makes detection layer L4 unreachable rather than merely
mitigated, and it removes the worst prompt-injection outcome in the system.
([ADR 0005](docs/decisions/0005-no-whatsapp-write-path.md))

**The reader and the agent never share a context.** Not one session that swaps
toolsets — if it's one context, attacker text is still in the window when
privileges rise. Two OS processes, structured JSON between them, formatted by
deterministic host code. The model's prose is never the transport.
([ADR 0006](docs/decisions/0006-two-process-privilege-split.md))

**Every new tool is evaluated as "what does a successful injection do with
this?"** `create_draft`, never `send_email`. Calendar events without dispatching
invites. The failure mode for this project isn't a clever attack — it's
`send_email` appearing one day because drafting got tedious.

**The reader's cursor keys on `_id`, never `timestamp`.** Companion devices
deliver messages out of order (worst observed lag 823s) and backfill inserts
years-old rows. A timestamp cursor silently drops messages. There is a test
asserting this; if it fails, read [ADR 0003](docs/decisions/0003-local-db-read.md)
before "fixing" the test.

**Snapshots include `-wal` and `-shm`, and land on tmpfs.** Copying
`msgstore.db` alone returns stale data. `/tmp` is *not* tmpfs on Raspberry Pi OS
— it's on the SD card, merely cleared at boot. Use `/dev/shm`.

**No message content in logs.** Other people's private messages live on this Pi;
that's an obligation, not just a risk. ([threat model R4](docs/threat-model.md))

## Rejected approaches

Documented in [detection-model.md](docs/detection-model.md) and the ADRs, each
failing for a structural reason rather than a tuning one: WhatsApp Cloud API,
Baileys / whatsapp-web.js, headless browsers, Frida/LSPosed hooking, WhatsApp
Desktop, a Windows VM on the Pi, and free cloud VMs.

Don't revisit them without a new fact that invalidates the original reasoning.
