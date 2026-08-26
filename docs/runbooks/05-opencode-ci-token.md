# Runbook 05 — the opencode CI token

The Pi publishes an xAI OAuth token to GitHub repo secrets every four hours, so
that coding work dispatched from Signal can run as grok inside a GitHub Actions
runner.

Nothing in this runbook is on the message path. If it is entirely broken, the
assistant still works; what stops is `/oc` runs on GitHub, and they stop
*remotely and several hours later*, which is why the failure path is wired to
Signal rather than to a log line.

## 0. Why this exists

The obvious design — give the agent a GitHub PAT and let it work on repos — does
not survive contact with the box. Verified against the live config 2026-08-15:

```
tools.deny:      exec, process, terminal, code_execution, browser, screen,
                 gateway, nodes, subagents, cron
sandbox.docker:  network "none", readOnlyRoot true, capDrop ALL
```

There is no execution tool, so the agent cannot run `git` or `gh`; and its
container has no network and a read-only root, so a credential would be a string
it has no way to spend. That is the containment ADR 0011 and NVB-14/23/25 exist
to produce, and it is not worth trading for a coding agent.

So the split is:

| | |
|---|---|
| **The Pi agent** | the control surface — it talks to you in Signal, drafts the issue and the comment, and (once the GitHub MCP lands) posts them |
| **GitHub Actions** | the execution environment — `/oc` in an issue comment runs opencode as grok in the runner, which branches, commits and opens a PR |
| **You** | approve the plan in Signal, review and merge the PR |

The runner's `GITHUB_TOKEN` is scoped to its own repo by GitHub and dies with the
run, so no per-repo credential is stored anywhere. The **only** long-lived
credential this arrangement needs is the xAI one, and that is what this runbook
is about.

## 1. What is installed

| Path | Source | Owner |
|---|---|---|
| `/usr/local/bin/wpa-oc-auth` | `deploy/push-opencode-auth.sh` | `root 0755` |
| `/usr/local/bin/wpa-oc-auth-notify` | `deploy/oc-auth-notify.sh` | `root 0700` |
| `/etc/wpa-oc.env` | — (machine-local) | `root:root 0600` |
| `/etc/systemd/system/wpa-oc-auth.service` | `deploy/systemd/` | root |
| `/etc/systemd/system/wpa-oc-auth.timer` | `deploy/systemd/` | root |
| `/etc/systemd/system/wpa-oc-auth-failed.service` | `deploy/systemd/` | root |
| `~navehbrenner/.local/share/opencode/auth.json` | `opencode auth login` | `navehbrenner 0600` |

**It runs as the login user, not as `openclaw`.** The gateway uid owns every
agent's auth store inside its own home, so a rotating subscription token and a
GitHub PAT placed there would be reachable by anything that ever gets a file
tool outside the sandbox. Nothing here needs uid 991.

## 2. Install

```bash
# opencode itself, as the login user
curl -fsSL https://opencode.ai/install | bash
opencode auth login          # xAI → SuperGrok → device code (you are over SSH)
opencode models --all | grep -i grok

# scripts and units, as root
sudo install -m 0755 deploy/push-opencode-auth.sh /usr/local/bin/wpa-oc-auth
sudo install -m 0700 deploy/oc-auth-notify.sh /usr/local/bin/wpa-oc-auth-notify
sudo install -m 0644 deploy/systemd/wpa-oc-auth.service \
                     deploy/systemd/wpa-oc-auth.timer \
                     deploy/systemd/wpa-oc-auth-failed.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Take the **device-code** option at login. The browser handoff has nowhere to go
on a headless Pi and fails in a way that looks like a hung command.

## 3. Configure

`/etc/wpa-oc.env`, root-owned `0600`, never in the repo — this repo is public:

```sh
# Fine-grained PAT: the repos in OC_REPOS, Secrets = Read and write, nothing
# else. It cannot read code, open issues, or push.
GH_TOKEN=github_pat_...

# From `opencode models --all | grep -i grok`. Without it opencode picks its own
# default, which has been a *video* model — the run then fails with
# "not available on this endpoint", which reads like an auth problem and is not.
OC_MODEL=xai/grok-4.6

# Space-separated. Every repo that should be able to run /oc.
OC_REPOS="navehbrenner/qualety"
```

Adding a repo is two edits and they must both happen: `OC_REPOS` here, and the
PAT's repository list on GitHub. Adding only the first fails at the next run
with a 404 from `gh` — which reads like a typo'd repo name rather than a
permission that was never granted.

## 4. Enable

```bash
sudo systemctl start wpa-oc-auth.service     # one run now
journalctl -u wpa-oc-auth -n 30              # expect "pushed to <repo>"
sudo systemctl enable --now wpa-oc-auth.timer
systemctl list-timers wpa-oc-auth.timer
```

### Checkpoint

Prove the failure path before trusting it. A notification you have never seen
fire is not a notification.

```bash
mv ~/.local/share/opencode/auth.json{,.bak}
sudo systemctl start wpa-oc-auth.service     # expect a Signal DM
journalctl -u wpa-gate -n 5                  # expect: sent agent=owner profile=owner-full
sudo ls /var/lib/wpa-gate/outbox/owner/      # expect: empty, the gate drained it
mv ~/.local/share/opencode/auth.json{.bak,}
```

An entry still sitting in that directory means the gate never picked it up —
check `self` is in the owner profile's `send_to`, since the gate refuses an
unlisted label without a word (ADR 0009: an unlisted label and an unknown one
are the same refusal).

## 5. The repo side

One file per repo, on the **default branch** — `issue_comment` workflows only
ever run from there, which is also what stops a fork PR from editing the gate
below.

```yaml
# .github/workflows/opencode.yml
name: opencode
on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

jobs:
  opencode:
    if: |
      contains(github.event.comment.body, '/oc') &&
      github.event.comment.user.login == 'navehbrenner'
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@v6
      - name: restore opencode auth
        run: |
          mkdir -p ~/.local/share/opencode
          printf '%s' '${{ secrets.OPENCODE_AUTH_JSON }}' > ~/.local/share/opencode/auth.json
          chmod 600 ~/.local/share/opencode/auth.json
      - uses: anomalyco/opencode/github@latest
        with:
          model: xai/grok-4.6
          agent: ${{ contains(github.event.comment.body, '/oc plan') && 'plan' || 'build' }}
```

Two lines are load-bearing:

- **The `comment.user.login` gate.** These are public repos. Without it, anyone
  who can comment can spend the subscription and run an agent.
- **No `XAI_API_KEY` anywhere.** opencode's precedence is env → config `apiKey`
  → auth store, so setting it would silently override the OAuth that was just
  restored, and the run would succeed against the wrong credential.

Usage: `/oc plan <what to do>` gets a plan comment and no code; `/oc implement
the plan above` gets a branch and a PR.

**The secret is raw JSON, not base64.** Publisher and restore step have to agree;
if one changes, both do. This bit once already — two boxes publishing in
different formats, which fails intermittently rather than outright.

## 6. The four rules that keep the token alive

The credential is a **subscription OAuth token, and its refresh token rotates on
use** (verified 2026-08-15 by backdating the expiry and diffing the store: the
`access`, `expires` *and* `refresh` fields all changed). Rotation means whoever
refreshes invalidates every other copy. This box is designated the sole
refresher, and three things enforce it:

1. **Force a refresh every run.** opencode refreshes only when a token is near
   expiry and has no "refresh now" command, so the script backdates `.xai.expires`
   and makes one trivial call. Without this, a run publishes whatever life was
   left — sometimes minutes. The live store is copied to `.bak` first, because
   the refresh is provoked by corrupting it: a failure in between would otherwise
   leave this box holding a token marked expired and nothing to put back.
2. **Prove the refresh took before publishing.** `expires` must be more than an
   hour out or the run fails and restores the backup. Without this a silent
   refresh failure publishes a nearly-dead token, and the symptom is CI 401ing
   hours later on another machine. `expires` is in **milliseconds** (13 digits,
   verified on hardware) — the same unit as `date +%s%3N`, and comparing against
   a seconds clock would make the check fail always.
3. **Blank the refresh token in the published copy — do NOT delete the key.**
   A runner holding no usable refresh token cannot rotate ours, which is the
   containment: worst case becomes one CI run returning 401, not this box losing
   its login to a machine we do not control.

   ⚠️ **Deleting the key breaks the runner silently.** opencode's OAuth credential
   schema declares `refresh: Schema.String` — required, unlike the
   `Schema.optional` fields beside it — and the loader is
   `Record.filterMap(data, v => Result.fromOption(decode(v)))`, which *drops* a
   credential that fails to decode with no error, no warning and no log line. A
   deleted key therefore unregisters the whole provider on the runner, and the
   failure surfaces two layers away as

   ```
   Model not found: xai/grok-4.6. Did you mean: grok-4.6, grok-4.6-fast?
   ```

   because `getModel` falls back to suggesting from the static catalog. The model
   string was never wrong. An empty string decodes fine and is exactly as useless
   to a runner. Verified against opencode v1.18.18, `auth/index.ts`.

   The guard changes meaning with the transform: "is the key gone" is now the wrong
   question, so it asks whether the real token appears anywhere in the payload, and
   refuses outright if the local store has no refresh token to compare against.
4. **4h cadence against a ~6h token.** Every published copy has hours of life, so
   a runner never reaches the skew where opencode would try to refresh at all.
   Rule 4 is what rule 3 falls back on; running both means the race has to be
   lost twice.

Raising `OnUnitActiveSec` past ~5h reintroduces the race even with rule 2 in
place, because the runner then starts failing instead of working.

## 7. When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| CI 401s a few hours after working | timer failed; you should have had a Signal DM | `journalctl -u wpa-oc-auth -n 50` |
| CI 401s **immediately** after a push | opencode rejected a store with no refresh token | drop `del(.xai.refresh)` from the script; rule 3 carries it alone |
| `gh: network is unreachable` on an IPv6 address | Pi has no global IPv6; Go prefers AAAA and does not fall back | already handled by `Environment=GODEBUG=netdns=cgo`; check the unit still has it |
| `opencode: command not found` | systemd's minimal PATH; the installer puts it under `$HOME` | the script prepends `~/.opencode/bin`; check the install location moved |
| `is a video model ... not available on this endpoint` | `OC_MODEL` unset or stale | `opencode models --all \| grep -i grok`, update `/etc/wpa-oc.env` |
| `gh` 404 on a repo | repo in `OC_REPOS` but not in the PAT's repository list | add it on GitHub; the two lists are maintained by hand |
| This box's own opencode login is dead | something else refreshed and rotated it | `opencode auth login` again, then find what refreshed — a runner should not be able to |
| Unit fails with a read-only filesystem error | `ProtectSystem` too tight for a path opencode writes | it is deliberately `full`, not `strict`; do not tighten without checking what it touches |

## 8. Alerts name the cause, and PATs warn before they die

Both added by NVB-41, and the second exists because of a gap NVB-36 opened.

### `wpa-triage` — every `*-failed.service` goes through it

`ExecStart=/usr/local/bin/wpa-triage <unit> owner`. It reads the failed unit's
journal, matches the signatures these scripts actually emit, and sends **one**
message naming the cause and the exact command. An unmatched failure still sends
what the alert always sent, so the floor never drops.

⚠️ **Raw journal text never leaves that script.** Every message is a fixed string
written in it; the journal is read only to *classify*. Two concrete reasons, not a
principle: `push-opencode-auth.sh` deliberately keeps opencode's stderr, so a
credential can be in that journal, and NVB-29 means the gateway writes outbound
**message text** to journald. `deploy/triage.test.sh` asserts both — if
`test_journal_text_never_leaks` fails, read the banner in `triage.sh` before
"fixing" it.

It runs three passes, and the order is the design: **the failed unit's own vocabulary
first** (`check-agent-auth.sh`'s verdicts, `wpa-oc-auth`'s PATH and model errors, a failed
wake in `wpa-gh-watch`), then cross-cutting causes (401, rate limit, DNS, a `jq` parse
failure), then the message that unit sent before this existed. The rows for `wpa-oc-auth`
are §7 of this runbook, encoded — **keep the two in step.**

Adding a signature is one `elif` plus one test case. Put the more specific cause first: a
rate limit also mentions `403`, and a dead token also fails to parse JSON.

**The agent id and provider in the credential-isolation message are selected, not copied.**
The vocabulary is `agents.list[].id` from the gateway config and `check-agent-auth.sh`'s
`MODEL_PROFILE_PREFIXES`; an id present in the journal but absent from the config is never
echoed. That is what keeps "name the broken agent" inside the no-raw-journal-text rule.

### `wpa-token-expiry` — daily, and the only thing watching the push PAT

GitHub reports expiry directly, which is what makes this cheap:

```bash
curl -sI -H "Authorization: Bearer $TOKEN" https://api.github.com/ | grep -i expiration
# github-authentication-token-expiration: 2026-11-23 12:49:22 UTC
```

Two credentials, each read from where it already lives — `/etc/wpa-push.token` and
`mcp.servers.github.env` — with no second copy to drift. Messages at **14, 7 and 1
days**, each announced once, so a token's whole life costs at most three messages.
A 401 (already dead) and a token with **no** expiry header both get one message too;
the second is not a failure but a standing risk worth naming.

**An unreachable GitHub is deliberately silent.** `wpa-gh-watch` has been fixed
twice by paging *less*, and a daily check that alerts on every uplink blip is how a
real expiry ends up muted.

**Why it is its own unit** rather than folded into `wpa-gh-watch`: an expired PAT is
exactly what breaks `wpa-gh-watch`, so a warning living there would die with the
thing it warns about.

| Symptom | Cause | Fix |
|---|---|---|
| No expiry warning ever arrives | the timer is not enabled | `systemctl list-timers wpa-token-expiry.timer` |
| The same warning every day | the state directory is not persisting | `StateDirectory=wpa-token-expiry`; check `/var/lib/wpa-token-expiry` |
| A warning for a token you already replaced | the marker is per threshold, and clears once the new token is >14 days out | run it once by hand: `sudo systemctl start wpa-token-expiry` |

## Operational notes

- **The Pi holds a third xAI login.** `owner`, the family agents, and now
  opencode all authenticate against the same account with independent expiries.
  Rotation here is not their rotation — but if a run ever does break the Pi
  agents, check `openclaw models --agent owner status` before assuming
  otherwise.
- **Quota is shared with the assistant.** A runaway CI loop spends the same
  subscription the household assistant runs on, and nothing in OpenClaw caps
  per-agent spend (ADR 0011). The metered API key was the one option with a
  natural cap; it was declined deliberately in favour of the subscription.
- **This is not a coding agent on the Pi.** Adding `exec` or a network to the
  sandbox to "just run it locally" undoes NVB-14/23/25. The dispatch model in §0
  exists precisely so that never has to happen.
- **`agent: plan` vs `build`** is chosen by a shell expression on the comment
  body, not by two workflow files. If opencode renames its primary agents, that
  expression fails open to `build` — a plan request would write code. Check
  `opencode agent list` after an upgrade.
