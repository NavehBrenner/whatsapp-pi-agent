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

## Work starts with a plan

**Unless the request explicitly says otherwise, produce a plan and get it approved
before changing anything.** Not a paragraph of intent — the plan names the files
it will touch, the approach, what it deliberately leaves out, and how the result
will be verified end to end.

This is not ceremony. Most of the expensive mistakes in this repo would have been
caught by someone reading the intent for thirty seconds: a socket mode that fails
`connect(2)`, an allowlist keyed on an identifier the wire never sends, a deploy
that quietly deletes half the reader. A plan is also where a scope change gets
noticed — "family members can use this too" reopened an ADR, and that is a
conversation to have before the code exists, not after.

Two things a plan must not do: assume a fact that hardware can settle, and defer
the unglamorous half. If something is unverified, the plan says so and the
verification is a step in it.

Exceptions are fine when they are stated: a typo, a one-line fix, or an explicit
"just do it". Silence is not an exception.

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
uv run mypy && uv run pytest
```

**Tooling is [uv](https://docs.astral.sh/uv/).** It creates `.venv`, installs the dev
dependency group from `pyproject.toml`, and — the reason it's worth having — installs the
Python named in `.python-version` (3.11) rather than whatever the dev box happens to have.
That file, `pyproject.toml` and `uv.lock` are the whole configuration; don't `pip install`
into `.venv` by hand, the next `uv run` will undo it.

`uv.lock` is committed and CI runs `--locked`, so a stale lock fails the build instead of
quietly resolving a different dependency set. Change dependencies with `uv add` / `uv lock`
and commit the result.

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

**The gate forwards a closed set of known one-to-one Signal conversations, and
nothing else.** Each carries a named principal and a profile; anything from an
unlisted sender, a group, or a new thread is dropped before dispatch and counted.
A phone number is not a secret — anyone who learns the assistant's number can
message it — so this list, not the number, is what refuses an unauthorized
invocation. It holds whether the assistant runs on a dedicated account or a
linked device. ([ADR 0004](docs/decisions/0004-signal-control-channel.md),
[ADR 0007](docs/decisions/0007-principals-on-the-control-channel.md))

**A trigger is a `dataMessage` with a non-empty body, never any `receive`
envelope.** Typing indicators and read receipts arrive on the same stream with no
`dataMessage` at all, so an agent that fires on `receive` is invocable by anyone
who can make the assistant's phone show "typing…" — and a sender check does not
help on an envelope carrying no command. There are fixture tests for this.

**No two principals share an agent session.** Own history, own tools, own
credentials or none. A profile holds nothing its principal does not already own,
which is what bounds an injection arriving through someone else's conversation.
Enforced by one container per principal, not by agent code being careful.
([ADR 0007](docs/decisions/0007-principals-on-the-control-channel.md),
[ADR 0009](docs/decisions/0009-agents-are-containers-that-ask-by-name.md))

**Authority is a (conversation, sender) pair, never a sender alone.** The same
person in a group and in their own chat is two principals with two profiles, and
the group one is narrower — a reply in a group is disclosed to everyone in it,
including people who did not ask. Groups are keyed by id, never by name, and
membership is pinned: drift refuses rather than degrades.
([ADR 0008](docs/decisions/0008-authority-is-a-conversation-sender-pair.md))

**The gate is the only process that touches Signal.** The agent asks by name and
the gate resolves it through its own roster — a JSON-RPC client of signal-cli
receives the inbound stream as well as sending, so an agent holding that socket
would see every envelope the gate refused. The agent handles no identifiers, and
agents never talk to each other directly; requests between them go through the
broker as confirmations.
([ADR 0009](docs/decisions/0009-agents-are-containers-that-ask-by-name.md))

**A confirmation names the action it authorises.** Quoted reply or a designated
reaction, single-use, expiring, answerable only by the pair it was sent to. There
is no "newest pending wins" — with two prompts outstanding that authorises the
wrong one, silently.
([ADR 0008](docs/decisions/0008-authority-is-a-conversation-sender-pair.md))

**The reader's cursor keys on `_id`, never `timestamp`.** Companion devices
deliver messages out of order (worst observed lag 823s) and backfill inserts
years-old rows. A timestamp cursor silently drops messages. There is a test
asserting this; if it fails, read [ADR 0003](docs/decisions/0003-local-db-read.md)
before "fixing" the test.

**The chat allowlist keys on JIDs, and there is no "read everything" default.** Group
subjects are chosen by whoever is in the group, so a name-keyed allowlist can be renamed
into. The reader refuses to start without a config rather than falling back to reading every
chat. Filtering happens in SQL, not on the returned rows — filter in Python and a batch with
no allowlisted messages stalls the cursor forever.

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
