# Runbook 06 — the project room

A Signal group bound to one repository, served by its own agent, which can file and
comment on that repo's issues and is woken when the repo moves.

It exists so the owner can start coding work from his phone: describe the task, the
agent opens an issue and asks the runner for a plan, the plan comes back into the
room, he approves, a PR appears. [Runbook 05](05-opencode-ci-token.md) covers the
runner half; this covers the room.

## 0. Why it is a separate agent

`load_config` refuses one agent in two conversations ("an agent session may not span
conversations"), so the owner's DM agent cannot also serve this room. That refusal is
doing real work here rather than being a formality: this room receives issue bodies
and PR titles from a **public** repo, which is text a stranger wrote arriving at an
agent with durable memory. A shared session would carry a poisoned issue into the
private chat; a shared workspace would keep it in the same `MEMORY.md`.

The cost is that the project room does not remember the DM's context, and that is
the intended trade rather than a limitation to work around.

| | |
|---|---|
| Signal group | `mKfSyvnnWHQHR1Au6FjN8CEAM/02Y2iBPkGnsUgalKo=`, two members: the owner and the assistant |
| Gate conversation | `qualety`, agent `qualety`, profile `project` (`send_to` = `["self"]`) |
| OpenClaw agent | `qualety`, workspace `~/.openclaw/workspace-qualety` |
| Session key | `agent:qualety:signal:group:mKfSyvnn…` |

`members` is two entries because **the assistant is a member of its own group**.
Leave its ACI out and the room refuses everything forever, which reads exactly like
a bug — `deploy/pin-group.py` prints the correct block.

## 1. Deploying the agent

Four things, and skipping the last is the `liron` failure from #24:

```bash
# 1. gate: conversation + profile in config.toml, then
sudo -u wpa-gate PYTHONPATH=/opt/wpa/src python3 -m gate.signal --check /opt/wpa/config/config.toml

# 2. openclaw: agents.list entry, a binding (GROUP ids carry NO `uuid:` prefix), and
#    the room's tools.allow ceiling
sudo -u openclaw HOME=/var/lib/openclaw openclaw config validate

# 3. the workspace, with an IDENTITY.md — an agent without one answers
#    "I don't have a name yet"
sudo -u openclaw install -d -m 0700 /var/lib/openclaw/.openclaw/workspace-qualety

# 4. ITS OWN auth profile. Without this it reads through to `main` and appears to
#    work right up until `main` is actually empty.
sudo -u openclaw HOME=/var/lib/openclaw openclaw models auth --agent qualety login
```

`--agent` is an option on the **parent** command: `openclaw models auth --agent X
login`, never `… login --agent X`.

Verify the fourth with `openclaw models --agent qualety status` and read the
`effective=` path. If it names `agents/main/...`, the login did not take.

### Renaming the agent

The agent was `code-invariants` until 2026-08-24 (NVB-43), when it took the name of
the repo it serves. Renaming one is not an edit to `agents.list[].id`: **both the
workspace and the auth profile are derived from that id**, at
`workspace-<id>/` and `agents/<id>/agent/openclaw-agent.sqlite`. Change the id
without moving both directories, with the gateway stopped, and the agent reads
through to `main` — it works right up until `main` is the wrong answer (NVB-38).

**Moving the directory is not enough: the database stamps its own agent id inside.**
That was missed here and went unnoticed for three days — `schema_meta.agent_id` still
read `code-invariants`, so every turn ended with *"database … belongs to agent
code-invariants; requested agent qualety"* and failed its post-run auth bookkeeping.
With the gateway stopped:

```bash
sudo sqlite3 /var/lib/openclaw/.openclaw/agents/<new-id>/agent/openclaw-agent.sqlite \
  "UPDATE schema_meta SET agent_id='<new-id>' WHERE agent_id<>'<new-id>';"
```

Check every agent afterwards — the stamp should equal the directory name for all of
them, and one command says so:

```bash
sudo bash -c 'for d in /var/lib/openclaw/.openclaw/agents/*/; do id=$(basename "$d");
  echo "$id $(sqlite3 "file:$d/agent/openclaw-agent.sqlite?mode=ro" \
  "select agent_id from schema_meta;" 2>/dev/null)"; done'
```

The name is worn by four things and they are not all the same thing: the OpenClaw
agent id, the gate conversation `label`, the sender pair `name`, and the GitHub repo.
The outbox directory follows the **agent**, not the label (`src/gate/signal.py`,
`_prepare`). `GH_SESSION_KEY` in `/etc/wpa-project.env` goes last, after the renamed
agent has taken one turn, because it must name a session that already exists — and
`wpa-gh-watch.timer` stays stopped until it does, or a tick fails the wake and sends
an `OnFailure` alert about nothing.

## 1a. The second room, and the one thing it must not inherit

`builder` (NVB-34) is deployed by the same four steps and serves the "agents
management" group. Two differences, and the second is the one that would be
destructive:

- **It holds no MCP tools and no plugins.** Its room ceiling is `read`, `write`,
  `edit`, `apply_patch`, `session_status`, `web_search` and nothing else — no
  `group:plugins`, so it also has no `image_generate` / `video_generate` despite the
  global grant. The narrowing lives entirely in the ceiling: that agent carries no
  `tools` block at all, so there is one list to read rather than two to reconcile.
- **`/workspace/repo` is a hand-seeded clone and must never join
  `wpa-project-sync.timer`.** That script does `reset --hard` + `clean -qfd` every
  tick — correct for a reviewer that only reads, fatal for an author whose work in
  progress would be deleted within the minute, silently. `builder` gets its sync tool
  in NVB-35 and gets none before then.

## 2. The GitHub MCP server

`/usr/local/bin/github-mcp-server` (v1.9.0, arm64, checksum-verified), spawned over
**stdio** by the gateway. Stdio deliberately: there is no loopback port, so nothing
to firewall — NVB-27's lesson applied before it could bite.

Three narrowings, weakest to strongest:

| Layer | Where |
|---|---|
| `toolFilter.include` | `mcp.servers.github` in `openclaw.json` |
| `--tools=…` | the server's own argv |
| **the PAT** | fine-grained, `Issues: read/write`, one repo, no contents |

The PAT is the floor and the only one that holds if the other two are misconfigured.

```bash
sudo -u openclaw HOME=/var/lib/openclaw openclaw mcp probe github --json
# expect: 8 tools, diagnostics: []
```

The eight are `issue_write`, `add_issue_comment`, `issue_read`, `list_issues`,
`actions_get`, `actions_list`, `get_job_logs` and `pull_request_review_write`.

### `--tools` has two silent failures, and they are not the same one

Measured against v1.9.0 on 2026-08-24, with the credential-free recipe below:

| `--tools=` | registers |
|---|---|
| `issue_write` | `issue_write` |
| `create_issue` | *nothing* |
| `create_pull_request_review` | *nothing* |
| `issue_write,create_issue` | `issue_write` |
| `issue_write,create_pull_request_review` | `issue_write` |
| `issue_write,bogusxyz123` | ***nothing*** |
| `issue_write,issues` | ***nothing*** |
| `issue_write,repos` | ***nothing*** |
| `issue_write,pull_request_write` | ***nothing*** |

Two different failures, neither of which prints anything:

1. **Recognised but empty.** `create_issue` and `create_pull_request_review` are
   advertised by `list-scopes`, accepted, and map to no tool — the real names are
   `issue_write` and `pull_request_review_write`. Alone they register nothing; inside a
   longer list they are ignored and their neighbours survive.
2. **Unrecognised, which takes the whole list down.** Any name the server does not know
   registers **zero** tools, not "the rest" — the known-good names go down with it.
   Every **toolset** name behaves this way, because `--tools` and `--toolsets` are
   different vocabularies: `issues`, `repos` and `pull_request_write` are toolsets, and
   passing one to `--tools` is indistinguishable from a typo.

So the rule, unchanged in force and sharper in scope:

> **On this server, a grant is verified by tool count. The absence of an error proves
> nothing — and a wrong name can cost you the tools you got right.**

The cheapest way to check a name before touching the live config is to list the tools
from a throwaway instance — `tools/list` does not authenticate, so no credential is
needed and nothing is spawned by the gateway:

```bash
{ printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 4; } \
| GITHUB_PERSONAL_ACCESS_TOKEN=x github-mcp-server stdio --tools=<candidate> 2>/dev/null \
| tail -1 | jq -r '.result.tools[].name'
```

Empty output means the name is a phantom — or that one of the others is. Probe the
candidate **alone and beside a known-good name** to tell the two failures apart. Keep the
`sleep`: the server exits on stdin EOF and answers nothing if the pipe closes first.

It also costs the narrow verb ADR 0010 asks for, twice. `issue_write` is a
consolidated write that can close and relabel; `pull_request_review_write` carries
`create`, `submit_pending`, `delete_pending`, `resolve_thread` and
`unresolve_thread` in one tool. There is no create-only variant of either, so the
containment falls back to the PAT's scope.

### Formal reviews, and what the agent cannot review

`pull_request_review_write` takes `event: APPROVE | REQUEST_CHANGES | COMMENT`, so the
agent can request changes rather than leaving a plain comment that cannot mark the
merge box.

**It cannot do that on a PR it authored** — GitHub rejects `REQUEST_CHANGES` and
`APPROVE` on your own pull requests, and the agent's PAT is the owner's identity. So
any PR *Naveh* opens can only receive a `COMMENT` review from the agent. The PRs that
matter are the runner's, authored by `nvb-opencode[bot]`, and those take the full set.

This ruined the first smoke test: the test PR was opened by the owner, so
`REQUEST_CHANGES` was impossible on it and the agent fell back to `COMMENT` and
explained why. **Design the probe so the identity is right**, not just the payload —
the same lesson as §5, one layer further in.

An approving review is advisory on this repo: `required_approving_review_count` is 0,
so approve is not merge authority. The required `build` check and `enforce_admins`
are what actually gate `main`.

### A rotated token needs a gateway restart

`openclaw mcp reload` disposes cached runtimes but does **not** re-read a changed
env. The MCP child is spawned per turn from the gateway's in-memory config, so after
editing the token you get 401s from a child holding the old one — while the file and
the config both hold the new one. Diagnosed by hashing:

```bash
sudo sh /path/to/check-mcp-token.sh    # file / config / live process, hashes only
```

Restart `wpa-openclaw` after any credential change.

## 2a. The `wpa` MCP server — the one we write

`/opt/wpa/.venv/bin/python -m wpa_mcp`, stdio, spawned by the gateway. NVB-35, phase 2
of NVB-33. Source in `src/wpa_mcp/`; the git logic is in `sync.py` and `push.py`, the
protocol binding in `__main__.py`, split so the tests never import the SDK.

**It held no credential until NVB-36, and now it holds a GitHub push token.** Through
phase 2 that was the point — `wpa__sync` fetches a public repo, so the entry's `env`
carried no secret and the server could be proven before anything dangerous hung off it.
`wpa__push` ends that: it needs `Contents: write`, and ADR 0013 says a tool credential
lives in its own server entry's `env`, one entry per principal. `builder` is the only
agent naming `wpa__*`, so one entry is one principal and the rule holds — but **do not
read this section's old claim anywhere else and assume it still stands.** Two things
follow:

- **This process is now worth compromising.** `push.py` therefore never passes git's
  output back to the model: every failure is one of a fixed set of strings written in
  that module, and git's real stderr goes to the journal. Git prints the remote URL into
  its own error text and a credential helper can be made to print into it.
- **A rotated token needs `systemctl restart wpa-openclaw`,** not `openclaw mcp reload`.
  Same rule as the GitHub PAT in §2, same symptom: 401s from a child holding the old
  value while the file and the config both hold the new one.

Four things about it are load-bearing and easy to get wrong.

### The class is `MCPServer`, not `FastMCP`

`mcp.server.fastmcp` was **removed** in the SDK's 2.0.0; the high-level class is
`MCPServer` in `mcp.server.mcpserver`. Every tutorial and most model memory still says
FastMCP, and the failure is an `ImportError` at spawn that surfaces as a dead server
rather than as a name that changed.

Related: `ToolAnnotations` takes **snake_case** field names (`read_only_hint`), even
though the spec spells them `readOnlyHint` and the server emits them that way on the
wire. The camelCase spelling is rejected as an unexpected keyword.

### It is the first Python dependency this repo has ever had

`wpa-reader` and `wpa-gate` run `python3 -m` against system Python with
`dependencies = []`. The MCP SDK broke that, so `deploy/install.sh` builds
`/opt/wpa/.venv` with:

```bash
uv sync --project /opt/wpa --no-dev --locked --python /usr/bin/python3
```

**`uv` is a prerequisite the installer checks for and does not install** — see runbook
01. `--locked` rather than `pip install mcp` because the lock is what makes the box run
what CI tested; `--python /usr/bin/python3` so a deploy cannot silently download a
managed CPython. The reader and gate were left on system Python: they need nothing.

The package is **not installed into the venv**. `PYTHONPATH=/opt/wpa/src` in the server
entry reads it out of the deployed tree, mirroring `wpa-reader.service` and
`wpa-gate.service`, so a `git pull` plus `install.sh` is the whole update.

**Budget ~1.15 s and ~62 MB per spawn** (measured on this Pi: `import mcp.server` is
1148 ms against 13–29 ms for bare `python3`, peak RSS 62 MB against 9 MB). No MCP child
is kept alive between turns, so that is paid every time the tool is used. It was
accepted knowingly in exchange for the SDK maintaining the protocol; a `git fetch` takes
longer than the import.

### `wpa__sync` never force-heals, and that is the whole design

`sync-project-repo.sh` does `reset --hard` + `clean -qfd` every tick. That is right for
the reviewer's mirror, which nobody writes to. `builder` **authors** in its checkout, so
the same policy would be data loss on a 60-second timer.

| Tree state | What happens |
|---|---|
| clean, behind | `merge --ff-only origin/main` |
| clean, current | nothing; says so |
| **uncommitted changes** | **nothing** — reported, left alone |
| **local commits** | **nothing** — reported, left alone |

The fetch always runs (it writes only to `.git`), so `behind` is a real number either
way: the staleness ceiling became a reported number rather than an automatic reset.
There is a test for the dirty case in `tests/test_wpa_mcp.py`; if it fails, read the
docstring in `sync.py` before "fixing" it.

**This checkout must never join `wpa-project-sync.timer`.** That timer would undo the
entire property within the minute.

### `wpa__push` pushes a branch; the agent opens the PR itself

Two steps, and the split is the design. NVB-36, phase 3.

```
agent (in its container):  git checkout -b …, edit, commit, run the tests
agent:                     wpa__push("branch")        → {branch, sha, base, repo}
agent:                     ghpr__create_pull_request(…)  its own title and body
```

`wpa__push` **pushes an existing ref and does nothing else.** It does not name branches,
does not commit, does not switch back to `main`. That version was designed when `builder`
could not run git; NVB-42 gave every agent `exec` and removed the reason for it. The
workspace is bind-mounted, so the host and the container share one `.git` — the agent
commits inside, and the tool pushes the ref it is handed.

| Asked to push | What happens |
|---|---|
| a branch that exists | `git push origin refs/heads/X:refs/heads/X`, no `--force` |
| **`main`** | **refused before any network contact** |
| a name git would reject | refused — `git check-ref-format` decides, not a regex here |
| a branch with no local ref | refused; commit it first |
| **a non-fast-forward** | **refused and reported** — rebase and call again |

There is no force path and no `--force-with-lease` path. Fast-forward-only needs no flag:
it is what `git push` already does to a branch ref, so the guarantee is that no option is
ever added rather than that the right one is passed.

**Why not GitHub MCP.** All 85 tools were enumerated on 2026-08-24 and there is no
`publish_branch`. The absence is structural: REST can build a commit out of bytes you
upload (`push_files`) or point a ref at a commit GitHub already has (`create_branch`),
but nothing accepts a packfile of local objects, because `git push` speaks
`git-receive-pack`. `push_files` would also round-trip every edited file through the
model, so the workspace and the PR could silently disagree.

**Two allowlist edits per tool, and one alone fails silently** — see the table below;
`wpa__push` and `ghpr__create_pull_request` each need the room ceiling *and*
`agents.list[builder].tools.alsoAllow`.

### `toolFilter` is not optional here — the server offers five tools, not one

The Python SDK advertises `prompts` and `resources` capabilities whether or not any are
registered, and OpenClaw synthesises a meta-tool for each. Without a filter the probe
returns:

```
"tools": ["wpa__prompts_get", "wpa__prompts_list", "wpa__push",
          "wpa__resources_list", "wpa__resources_read", "wpa__sync"]
```

while `"tools": 2` and `diagnostics: []` sit right above it, because the **count** is of
real tools and the four extras are synthesised over them. So on this server the count
does not describe the surface — **read the `tools` array**. That is a sharper version of
§2's "a grant is verified by tool count", cutting the other way.

`toolFilter: { include: ["sync", "push"] }` on the server entry removes them. The
allowlists would have contained them anyway, but ADR 0010 asks for absent rather than
refused. **The filter names the tool as the server registers it (`push`), not as the
agent sees it (`wpa__push`)** — the prefix is added afterwards, and putting the prefixed
name here filters everything out.

### Verifying it — and why asking the agent is not enough

Name the tool before pinning it anywhere; the §2 credential-free `tools/list` recipe
works here too. Then:

```bash
sudo -u openclaw HOME=/var/lib/openclaw openclaw mcp probe wpa --json
# expect: "tools": ["wpa__push", "wpa__sync"], diagnostics: []
```

**Adding a tool is two config edits and one alone fails silently** — worse than
silently, as it turns out:

| Edit | What it does on its own |
|---|---|
| the room's `tools.allow` ceiling | makes the tool **visible** to the model |
| `agents.list[builder].tools.alsoAllow` | makes it **callable** |

With only the ceiling, `tools.profile: "minimal"` still strips the tool — and the agent
*lists it as available anyway*, because the name reached its prompt. Asked to call it,
it announces that it is calling it and nothing happens. `alsoAllow` is the layer that
widens against `minimal`; the ceiling only narrows. And because an agent-level
`alsoAllow` **replaces** the global one rather than merging, every global name has to be
repeated beside the new tool.

**So the authoritative check is the tool-policy log, not the agent's self-report:**

```bash
sudo journalctl -u wpa-openclaw --since "2 min ago" | grep "tool policy removed"
```

The tool must **not** appear in any removal line. Verified 2026-08-21: with the ceiling
alone the log read `removed 9 tool(s) via tools.profile (minimal): … wpa__sync`, while
the agent cheerfully listed `wpa__sync` among its tools. With both edits the line drops
back to 8 and the call works.

This qualifies NVB-34's probe technique. Asking the agent in neutral text is still worth
doing — it is the only thing that shows what the model *believes* — but it answers from
the prompt, so it can name a tool that policy has removed. Confirm with the log, or by
making the agent actually call the thing.

**NVB-39 looked exactly like that gap and was not one.** On 2026-08-21 `builder`
replied `NO WRITE TOOL` and created nothing, with `write` present in the global
`alsoAllow`, in `builder`'s own list, in the room ceiling, in `tools.sandbox.tools.allow`
and in no removal line. The trajectory for that same turn settles it: the registered
tools were `apply_patch, edit, read, session_status, web_search, wpa__sync, write`, and
the system prompt it was sent lists `write: Create or overwrite files`. Nothing had been
removed.

What happened is that the **first** turn of that session asked it to enumerate its tools
and it answered with six names, omitting `write`. That answer stayed in the history every
later turn re-read, so every subsequent probe was agreeing with its own earlier mistake.
Re-run on 2026-08-24 in a fresh session against unchanged config, the same prompt
produced a real `write` toolCall and a file on disk.

So the rule is stronger than "the self-report answers from the prompt": **a self-report
you ask for early poisons the session**, and asking again does not correct it. Probe in a
throwaway session, or read the trajectory.

### The trajectory is the ground truth

Better than the removal log, because it is per turn and positive rather than negative —
it says what the model was *given*, not what was taken away:

```bash
sudo python3 -c '
import json, sys
for line in open(sys.argv[1]):
    e = json.loads(line)
    if e.get("type") == "context.compiled":
        print(e["ts"], [t["name"] for t in e["data"]["tools"]])
' /var/lib/openclaw/.openclaw/agents/builder/sessions/<session-id>.trajectory.jsonl
```

`openclaw sessions list --agent <id> --json` gives the session id and the file path. The
same file's `model.completed` events carry `messagesSnapshot`, where a `toolCall` block
is proof the tool was *invoked* rather than described — the difference the agent's prose
cannot express.

For the sandbox layer specifically, `openclaw sandbox explain --agent <id> --json` prints
the effective sandbox allow/deny and the config key each came from. It exonerated the
sandbox here in one command.

## 2b. `exec`, and the test loop it exists for

NVB-42. Every agent can run a shell, and it runs inside that agent's own container. The
point is not the shell — it is that `builder` can now run the tests it writes instead of
proposing into CI and finding out later.

### The image is nearly empty, and that is fine

```
git      /usr/bin/git
python3  /usr/bin/python3
uv       MISSING
pytest   MISSING
node     MISSING
gcc      MISSING
```

`network: none` means a container can never fetch the missing ones. So `deploy/install.sh`
builds a dev venv **inside the agent's workspace**, at `.venv-sandbox`, and it appears in
the container at `/workspace/.venv-sandbox`. No mount, no config key — the workspace is
already mounted.

Two things make that work, and one of them is not obvious:

- **A venv survives being read at a different absolute path**, because `sys.prefix` is
  computed from the runtime executable. It is built at
  `/var/lib/openclaw/.openclaw/workspace-builder/.venv-sandbox` and read at
  `/workspace/.venv-sandbox`. Invoke it as `bin/python -m pytest`: the console scripts in
  `bin/` carry the build path in their shebangs and do **not** survive the move.
- **It is writable by the agent, and that costs nothing.** The workspace is mounted `rw`,
  so the agent can modify its own venv — but it has `exec`, so it could run anything it
  wanted regardless. Read-only would have defended against nothing.

### ⛔ `docker.binds` is the obvious way to do this, and it cannot be

It was tried in NVB-42. `createSandboxContainer` passes
`bindSourceRoots: [workspaceDir, agentWorkspaceDir]` — **hardcoded, with no config key
that widens it** — and `validate-sandbox-security` rejects any bind whose source falls
outside them:

```
Sandbox security: bind mount "/opt/wpa-sandbox-venv:/opt/wpa-sandbox-venv:ro"
source "/opt/wpa-sandbox-venv" is outside allowed roots
(/var/lib/openclaw/.openclaw/workspace-builder).
```

**⚠️ And it is an outage, not a refusal.** The check runs at *container creation*, so one
bad bind under `agents.defaults` fails every sandboxed turn for **every agent** — not just
the one being configured. On 2026-08-24 it ran nineteen minutes and took twelve turns with
it: six in the project room, three in the owner's DM, four probes. Those turns fail *after*
the gate hands them off, so nothing retries them.

```bash
sudo -u openclaw HOME=/var/lib/openclaw openclaw config unset agents.defaults.sandbox.docker.binds
sudo systemctl restart wpa-openclaw    # ← NOT optional, despite what the unset says
```

**`config unset` prints "No gateway restart needed" and it is wrong here.** The unset fixes
the file; the running gateway keeps the bind in memory and goes on failing until restarted.

**And do not verify with an agent that has already answered today.** The check runs at
container creation, so an agent whose container is warm cannot fail — it will answer happily
while the box is still broken for everyone else. That is exactly how the 2026-08-24 outage
was declared over eight minutes into its nineteen. Verify with an agent that has been idle,
or restart first and then probe several.

The invocation, which belongs in the agent's `TOOLS.md` because it will not guess the
`PYTHONPATH`:

```bash
cd /workspace/repo
PYTHONPATH=/workspace/repo/src /opt/wpa-sandbox-venv/bin/python -m pytest -q
PYTHONPATH=/workspace/repo/src /opt/wpa-sandbox-venv/bin/python -m mypy
```

Measured in the real image, network off: **103 tests in 3670 ms**, mypy 11144 ms cold and
**403 ms warm**. Fast enough to iterate against, which was the whole question.

### Why every agent got it, when only one needed it

`collectExplicitDenylist` in `dist/tool-resolution-*.js` **concatenates** deny entries from
every layer — profile, global, agent, room, subagent, gateway — and no layer subtracts. A
name in the global `tools.deny` is denied to everyone, full stop. Granting `builder` alone
would have meant deleting `exec` there and re-denying it on seven other agent entries:
seven edits, silent when one is missed, and a miss hands a family DM agent a shell.

Granting it globally removes that hazard — `tools.deny` loses it, `tools.alsoAllow` gains
it, and `tools.sandbox.tools.allow` gains it too, because that second allowlist applies only
to sandboxed runs and would otherwise strip it after everything above passed.

**But three global edits are not enough, and the way they fall short is backwards.** An
agent-level `tools.alsoAllow` **replaces** the global list rather than merging — the same
trap that cost `qualety` its `MEMORY.md`. So the global grant reached only the four
family DM agents, which have no agent entry of their own, while `builder`, `owner` and
`qualety` carry their own lists and got nothing. The agent the whole change was for
was the one agent that did not get it, and the tool-policy log said
`removed 25 tool(s) via tools.profile (minimal): … exec …` while `exec` sat in the global
`alsoAllow`, the room ceiling and the sandbox allowlist.

Every agent-level `alsoAllow` has to name `exec` as well. That is seven live edits in total,
not three.

The room ceilings are the separate question, and they only narrow: `exec` is in this room's
and the project room's, and deliberately **not** in the family room's. Every DM session has
it regardless, because a DM has no ceiling to narrow it.

### ⚠️ `tools.elevated.enabled` must stay `false`

It runs `exec` **outside** the sandbox — on the host, as uid 991, the gateway's own user,
which holds every credential on this box. While exec was denied outright this key gated
nothing. It is now the difference between a shell in a box and a shell on the Pi, and it is
written up in [the threat model](../threat-model.md) as a control rather than a default.

### Verifying it

The sandbox layer, in one command:

```bash
sudo -u openclaw HOME=/var/lib/openclaw openclaw sandbox explain --agent builder --json
# expect: "exec" in .sandbox.tools.allow, and the venv in .sandbox.workspaceMounts
```

Then the registered surface — from the trajectory, per §2a, never from the agent. And then
the only check that actually proves it: make the agent run the suite, and confirm the
`toolCall` block exists rather than believing the report of it.

## 3. The watcher

`wpa-gh-watch.timer` → `/usr/local/bin/wpa-gh-watch`, every 60s.

GitHub cannot push (the Pi is not reachable) and the agent cannot poll (no
scheduling tool, no sandbox network), so this box looks on its behalf. Idle ticks
cost one authenticated HTTPS call and **no model tokens**; a turn is spent only when
something happened. That is the whole reason it is not an `openclaw cron` job waking
the agent on a timer to find nothing.

It reads the token out of `mcp.servers.github.env` rather than keeping a second
copy, filters events authored by the token owner (so the agent is never woken about
its own `/oc` comments), and dedupes against a `seen` list.

Three things it watches: new issue comments, new or updated issues and PRs, and
**failed workflow runs**. The last is not author-filtered — a failure matters whoever
caused it, and the ones caused by the agent's own PRs are exactly the ones it must
act on. The `actions/runs` endpoint takes no `since`, so that window is applied
client-side, and the dedupe id carries the conclusion so a re-run that fails again is
a new event.

The Issues-scoped PAT reads `actions/runs` on a public repo without extra
permissions — verified, HTTP 200 — so no widening was needed for this.

The agent can then investigate it: `actions_get`, `actions_list` and `get_job_logs`
are granted read-only, so it pulls the failing job's log and reports the actual error
rather than guessing. `actions_run_trigger` — the fourth tool in that toolset — is
deliberately withheld: re-running a workflow is a write and the agent has no reason
to hold it. The Issues-scoped PAT reads all three on a public repo, verified before
they were granted.

### `openclaw system event` does not wake anything here

It is the obvious-looking call. It enqueues an event and returns `ok` **without
running a turn**, because the heartbeat that consumes the queue is suppressed by a
comments-only `HEARTBEAT.md`. `--expect-final` still returns `ok`. The working
primitive is:

```bash
openclaw agent --session-key "<key>" --deliver --timeout 120 --message "…"
```

`--deliver` alone suffices. And note the gateway logs **no** `delivered reply` line
for a CLI-initiated turn, so that log proves nothing either way — the room does.

### The cursor advances on wall-clock, not on events

Anchoring it to the newest event seen freezes the window on a quiet repo, so every
poll re-fetches a widening slice of history. With `per_page=50` and no pagination
that eventually drops events off the end — a quiet week silently breaking the first
busy day. It advances to `now - 5min` each run; the overlap plus the seen-list is
what makes that safe.

## 4. The code mirror

`wpa-project-sync.timer` → `/usr/local/bin/wpa-project-sync`, ticking every 60s —
and `wpa-gh-watch` also runs it inline on every tick with `WPA_SYNC_FORCE=1`, which
is where it actually earns its keep (see §4a).

`/workspace/repo` inside the sandbox is a shallow, single-branch mirror of `main`,
which the agent reads with the `read` tool it already has. **That is the whole point
of doing it this way**: the alternatives were exec plus a network in the sandbox
(undoing NVB-14/23/25) or GitHub repo tools (one file per call, a 5000-char window,
and a wider PAT). A directory appearing in a workspace it already reads grants
nothing new at all.

- **The agent decides when.** It cannot run the sync, but it can ask: writing any
  text into `/workspace/repo-sync.request` makes the next tick sync immediately and
  consumes the file. ADR 0009's shape — an intent, not a command, so there are no
  arguments to smuggle anything through. The write tool rejects an empty file, so
  the request needs a byte in it.
- **`reset --hard` and `clean`, not `pull`.** The workspace is read-write to the
  agent, so the checkout can have been edited. A mirror that force-heals is easier
  to reason about at 04:00 than one that wedges on a conflict, and the clone branch
  self-heals even if `.git` is deleted.
- **`repo-sync.json` is as much the point as the checkout.** It carries the commit,
  its subject and the fetch time, and lives outside the checkout so `git clean` does
  not remove it. A mirror that merely looks current is worse than none: the agent is
  told to state which commit it is reasoning from.
- The staleness ceiling with nobody asking is `max_age` in the script (10 min), not
  the timer interval. The timer's minute is the resolution at which a request is
  picked up.

git goes through libcurl, which does Happy Eyeballs and falls back to IPv4 on its
own — this is **not** the Go-resolver trap that needs `GODEBUG=netdns=cgo`.

## 4a. Pull requests: a checkout each, and the diff beside it

The mirror also fetches `refs/pull/*/head` and lays out one checkout per **open** PR:

| Path in the sandbox | What |
|---|---|
| `/workspace/pr-<N>/` | a linked worktree of the PR's head, detached |
| `/workspace/pr-<N>.diff` | `git diff origin/main...pr/<N>` |
| `/workspace/repo-sync.json` → `.prs[]` | `{number, head}` for every open PR |

- **The pull refs need no credential.** The repo is public, so this cost the PAT
  nothing — still `Issues: read/write`, no contents. That is the whole reason this
  shape won over the GitHub MCP's PR tools, which would have needed
  `Pull requests: read` + `Contents: read`, a rotation, a gateway restart, and would
  still return a truncated diff through a 5000-char window.
- **The diff file exists because the agent has no `exec`.** It cannot run `git diff`,
  so a checkout on its own would leave it comparing two trees by eye. Three-dot, not
  two: they agree today because `main` requires branches to be up to date, but the
  merge-base form stays correct if that rule is relaxed.
- **What is open comes from the API, never from git.** `main` is protected as linear
  history, so PRs land **squashed** — a merged PR's head is never an ancestor of
  `main`, and `git branch --merged` / `merge-base --is-ancestor` report that nothing
  has ever merged. `GET /pulls?state=open` is also the only signal that catches a PR
  closed *without* merging, and it self-corrects after a failed tick.
- **Cleanup is "delete everything not in the open set",** checkout and diff in one
  pass so neither outlives the other, then `git worktree prune`. The self-heal path
  (`rm -rf repo`) wipes the `pr-*` dirs too: they are linked worktrees of that store,
  and left behind they would hold a `.git` file pointing at nothing while the agent
  read a frozen tree as current.
- ~280 KB per PR against a 604 KB object store, so this is not a disk question.

### The trigger is the head sha

`wpa-gh-watch` compares the heads it last **reported** — kept in
`/var/lib/wpa-gh-watch/heads` — against `repo-sync.json` after the sync, and reports
any PR whose head moved. That covers a PR just opened and one that just gained
commits, with the same event.

> **Changed 2026-08-17.** It used to read `repo-sync.json` before *and* after the
> sync, within one run. That loses a push permanently whenever the run dies between
> the two: the sync has already written the new sha, so the next run's "before"
> matches it and the change is never seen. Comments and CI runs survive that because
> they re-query with `since=`; a head sha had nothing to fall back on. Keeping the
> reported heads in their own file makes the comparison mean what it says.

**`updated_at` does move on a plain push** — measured on 2026-08-17 with an empty
commit to PR #9: it went from `2026-08-16T04:17:18Z` to `2026-08-17T09:14:57Z`, two
seconds after the push, with zero comments. An earlier version of this runbook claimed
the opposite and justified the head-sha trigger with it. That was wrong, and the
`since=` poll in §3 does catch a push on its own.

What the head sha is still doing, now stated honestly:

- **It is not author-filtered.** The `issues?since=` loop drops anything whose author
  is the token owner, so a PR **Naveh raised by hand** produces no event at all — five
  of the first seven PRs on that repo. `refs/pull/*` does not care who opened it.
- **It names the commit.** The event carries the sha and the checkout path, so the
  agent is told what to read rather than that something changed. Its dedupe id carries
  the sha, so a re-push is a new event and a re-read is not.
- **It is tied to what is on disk, not to GitHub's metadata.** The event is derived
  from the mirror that just synced, so the agent is never told about a PR whose
  worktree is missing.

Both fire for a bot-authored push, which is why the verification run reported the PR
twice — that is duplication, not a fault, and the `seen` list keeps each to one event.

Two consequences worth keeping straight, and neither depends on the `updated_at`
question above:

- **The sync runs on every watcher tick, not only when a PR event was seen.** A
  worktree the agent is told about must already exist, and the sha comparison needs
  the fetch to have happened.
- **A failed sync does not swallow the wake.** It is bounded at 90s and, on failure,
  appends a stale-mirror warning to the message instead of aborting — comments and CI
  failures still have to get through, and an agent told its checkout may be stale is
  far better off than one not woken at all. `wpa-gh-watch.service` therefore carries
  `TimeoutStartSec=300`, which must stay above 90s + the agent's `--timeout 120`.

`flock` on `$GH_WORKSPACE/.git.lock` serialises the two paths. Without it the timer
and the watcher fetch into the same `.git` and race on `index.lock`, and under
`set -e` a lost race becomes an `OnFailure` DM about nothing.

## 5. Verifying without touching the repo

Rewind the cursor past an existing event and let it replay:

```bash
sudo sh -c 'echo 2026-08-14T18:00:00Z > /var/lib/wpa-gh-watch/cursor
            : > /var/lib/wpa-gh-watch/seen
            chown openclaw:openclaw /var/lib/wpa-gh-watch/{cursor,seen}'
sudo systemctl start wpa-gh-watch.service
sudo journalctl -u wpa-gh-watch -n 15 --no-pager     # expect a "waking …" line
```

The script rewrites a correct cursor on the way out, so this is self-cleaning.

**Probe design matters here.** An earlier check asked the agent for a count of open
issues, got `0`, and was taken as success — but the repo genuinely had zero, so the
success value and the failure value were identical and a dead credential passed. Ask
for something whose failure is distinguishable, or check a process's environment
rather than a model's report.

## 5a. DNS, and why every Go binary here struggled

The LAN router at `10.10.0.138` answers glibc's queries and does **not** answer the
Go resolver's. Measured against the GitHub MCP server on 2026-08-15:

```
router only        14 of 15 lookups failed   dial tcp: lookup api.github.com
                                             on 10.10.0.138:53: no such host
getent / curl      10 of 10 fine, HTTP 200
public resolver    15 of 15 fine
```

So this was never a blip. It reached a human three times as an agent saying "DNS
blip, retrying", which is exactly how a systematic failure hides — the paraphrase
sounded transient and nobody measured it until it was in the way.

**`GODEBUG=netdns=cgo` does not fix this class of binary.** It selects a resolver
that is not compiled into a `CGO_ENABLED=0` build, and `github-mcp-server` is
statically linked (`ldd` → "not a dynamic executable"). It was set on that MCP server
for a day and did nothing. It *did* genuinely fix dockerd, which is dynamically
linked — same symptom, same box, and only one of the two could take that cure. Check
the linkage before reaching for it.

The fix is `deploy/networkmanager/90-wpa-dns.conf` (`dns=none`) plus a hand-written
`/etc/resolv.conf` putting public resolvers first. Two things about the shape:

- **`dns=none`, not per-connection `ipv4.dns`.** NM merges DNS from every active
  connection, so setting it on eth0 alone left wlan0 injecting the router at the top
  of the list — observed, and the retest went straight back to 9 of 10 failing.
- **Reload, do not reactivate.** `systemctl reload NetworkManager` leaves links up.
  SSH to this box arrives over wlan0, so `nmcli con up preconfigured` would cut the
  session performing the fix.

The router stays as the last nameserver: it cannot serve the Go resolver, but it is
the only thing that knows LAN names, and a resolver only falls through to it on
timeout rather than NXDOMAIN.

## 6. When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| Room silent on new PRs/plans | watcher failing; a Signal DM should have said so | `journalctl -u wpa-gh-watch -n 50` |
| Agent reports 401 from GitHub | child spawned before a token change | restart `wpa-openclaw`, then hash file/config/process |
| A granted tool "is not available" | one of six policy layers stripped it | `journalctl -u wpa-openclaw \| grep tool-policy` — it names the layer |
| Agent answers in the wrong room | binding uses a `uuid:` prefix on a group id | group ids carry no prefix; check `sessions.json` |
| Everything replayed after a reboot | cursor lost | `StateDirectory=` owns it; check it exists and is `openclaw`-owned |
| Agent describes code that has changed | mirror stale or frozen | `repo-sync.json` carries the commit and fetch time; `journalctl -u wpa-project-sync` |
| Room silent when opencode pushes to a PR | head-sha comparison broken, not `updated_at` | is `.prs[]` in `repo-sync.json` moving? compare it with `/var/lib/wpa-gh-watch/heads`, which is what was last reported; if `.prs[]` is empty the sync is failing inside `wpa-gh-watch` |
| Room silent, and `GH_REPO` is not the repo you were looking at | the watcher watches one repo | `grep GH_REPO /etc/wpa-project.env` — activity on any other repo is correctly ignored |
| `/workspace/pr-<N>/` missing for an open PR | sync failed, or the PR is not in `?state=open` | `journalctl -u wpa-gh-watch` — the wake message says so when the sync failed |
| `another sync holds the lock` in the journal | a fetch took >45s and both paths collided | transient; if it repeats, the network is the problem, not the lock |
| Agent says a tool it was granted "is not available" | an agent-level `alsoAllow` **replaced** the global one | list the agent's own `tools.alsoAllow` and check the global names are repeated in it |

## Operational notes

- **An agent-level `tools.alsoAllow` REPLACES the global one; it does not merge.**
  Granting the GitHub tools to `qualety` therefore removed `read`, `write`,
  `edit` and `apply_patch` from it — including its ability to maintain its own
  `MEMORY.md` — and nothing said so. `config validate` passed, the room ceiling still
  listed those tools, and the only symptom was the agent reporting a shorter tool
  list than the config implies. An agent's own list must repeat the global names.
  This is the same class as the `--tools=create_issue` trap: a grant that appears to
  grant and does not.
- **Six layers can strip a tool**, and the tool-policy log names which one every
  time: `tools.profile`, global `tools.deny`, agent `alsoAllow`, the room's
  `tools.allow` ceiling, `tools.sandbox.tools.allow`, and a **built-in**
  `sandbox tools.deny` that is not in our config at all. That last one is why the
  agent has no `cron`: upstream denies scheduling to sandboxed agents, and clearing
  the list would mean emptying an unenumerable default to unmask one entry.
- **Subagents inherit the parent's own tools, but not its MCP tools.** A child gets
  the file tools (`read`, `write`, `edit`, `apply_patch`, plus `web_search` where the
  parent has it) and is stripped of `github__*` as non-inheritable and of
  `sessions_spawn`/`subagents` as a recursion guard. So a subagent can read and map
  the mirror; it cannot post to GitHub as you, which is the right split.

  An earlier pass concluded they inherit *nothing* and die with "No callable tools
  remain". That was true at the time and true for the wrong reason: the child's list
  is derived from the parent's **effective** allowlist, and the parent's was empty of
  file tools because of the `alsoAllow`-replaces-global fault above. Fixing that
  fixed subagents as a side effect. Worth remembering as a shape: a platform
  limitation inferred from a symptom that was really local misconfiguration.

  The inherited set follows the *session*, not the agent — a subagent spawned from a
  `--agent` CLI probe gets a narrower list than one spawned in the room or DM,
  because those sessions have different effective policy. Check in the session you
  actually use.
- **The MCP server is gateway-global, not agent-bound.** Only `qualety` is
  granted the tools, but that is tool *policy*, not an absent credential — the
  distinction ADR 0010 exists to make. If policy ever fails open the blast radius is
  issues and comments on one repo, which is why the PAT's scope matters more than it
  looks. Per-agent MCP servers are not supported on `2026.7.1-2`.
- **No registry rows were added** for these grants. `src/agent/registry.py` is
  checked against `[[agent.profiles]].tools`, and nothing lists them: enforcement is
  OpenClaw's `alsoAllow` plus the room ceiling. Adding rows nothing references would
  be dead entries in a list whose whole value is that it refuses unknown names. The
  two-config drift this leaves is the cost ADR 0011 already accepts.
