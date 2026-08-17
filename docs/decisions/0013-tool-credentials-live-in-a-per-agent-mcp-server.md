# 0013 — Tool credentials live in a per-agent MCP server, never in the auth store

**Status:** Accepted 2026-08-17 ([NVB-32](https://linear.app/naveh-brenner/issue/NVB-32)),
mechanism verified against the running gateway — evidence in
[Q7](../OPEN-QUESTIONS.md#q7--why-does-the-default-agents-auth-store-refill-itself)

**Amends:** [0011](0011-openclaw-owns-the-channel-the-gate-owns-the-room.md) (its
"one hole in credential isolation, and how it is closed" — the closure does not hold),
[0010](0010-profiles-are-pre-bound-grant-bundles.md) (how "absent, not refused" is
achieved for a credentialed tool)

## Context

ADR 0011 closed OpenClaw's read-through credential inheritance with a structural rule:

> **The default agent holds no credentials.** … An agent cannot inherit what does not
> exist upstream. This is an invariant `deploy/render-agents.py` must assert, not a thing
> to remember.

**That state is not reachable.** Q7 ran for three rounds and closed on 2026-08-17 with a
named mechanism: after every successful OAuth refresh, OpenClaw calls
`mirrorRefreshedCredentialIntoMainStore` with `agentDir: void 0` — the default agent —
unconditionally. Emptied with the gateway stopped and verified empty, `main` refilled
**36 seconds after the restart**.

It is deliberate. The refresh lock's own comment gives the reason:

> *This lock is the serialization point that prevents the `refresh_token_reused` storm
> when N agents share one OAuth profile (see issue #26322) … peers can adopt the
> resulting fresh credentials instead of racing against a single-use refresh token.*

`main` is the noticeboard peers adopt from. Emptying it takes the noticeboard down for a
few seconds; it does not create a boundary.

Underneath that sits the constraint we had never read: **auth profile ids are keyed on
the account, not the agent** (`xai:navegerc@gmail.com`). Two agents authenticating as one
account are one principal to OpenClaw. There is no per-agent credential isolation in the
auth store to leak — there is no such feature. `OPENCLAW_AUTH_STORE_READONLY=1` does not
help: it guards the external-CLI sync and the oauth-file merge, and never reaches
`saveAuthProfileStore`.

ADR 0011 also assumed the *other* half was the dangerous one — an agent reading another
agent's store file directly. NVB-25's rootless Docker closed that, and ADR 0011 is
already amended to say so. What survived is the half the container cannot touch: the
**gateway** resolves credentials on an agent's behalf, so read-through is not a file read
and no sandbox stops it.

ADR 0012 tightened the box further: tools "execute in the gateway process, never in the
sandbox, on any runtime." So NVB-17's planned delivery — *"root-owned, `0600`, injected
per container via systemd `LoadCredential=`"* — cannot work for a tool making an outbound
HTTPS call. There is one gateway process; there is no per-agent container to mount into.

## Decision

**Tool credentials never enter OpenClaw's auth profile store.** That store is for **model
provider credentials only** — the one credential class every agent legitimately shares.

**A tool credential is delivered as its own MCP server entry, one per agent that may hold
it**, with the credential in that entry's `env`, and the agent's `tools.alsoAllow` naming
only its own server's tools:

```jsonc
"mcp": { "servers": {
  "cal-owner": { "command": "…", "env": { "GOOGLE_OAUTH_TOKEN": "<owner's>" } },
  "cal-liron": { "command": "…", "env": { "GOOGLE_OAUTH_TOKEN": "<liron's>" } }
}},
"agents": { "list": [
  { "id": "owner",  "tools": { "alsoAllow": ["cal-owner__list_events"] } },
  { "id": "liron",  "tools": { "alsoAllow": ["cal-liron__list_events"] } },
  { "id": "family" }
]}
```

`family` has no `cal-*` tool in its surface, so the tool is **absent from its turn** —
ADR 0010's property, intact. And because the credential has no auth profile id, there is
nothing for the mirror to copy into `main`: **this design is immune to the mechanism
above rather than defended against it.**

This is not new machinery. It is the shape the GitHub PAT already ships in —
`mcp.servers.github.env`, with `code-invariants` the only agent naming `github__*` tools.

## What this gives, and what it does not

**Gives:** existence separation. A tool absent from an agent's list cannot be reached by a
clever prompt, which is the ADR 0010 property and the thing this project has repeated
since ADR 0009. Combined with NVB-25's container — which stops any agent reading
`openclaw.json` — an agent can neither invoke another principal's tool nor read the
credential behind it.

**Does not give:** kernel-enforced separation *of the credentials from each other*. They
sit in `openclaw.json`, and the gateway holds all of them in one address space; each MCP
server is a child process with only its own `env`, but the parent spawned them all. What
separates `owner`'s calendar from `family` is a tool allowlist plus a container, which is
**policy plus containment, not structure**. ADR 0010's original promise — the credential
was never mounted anywhere it could be reached — is weakened to: the credential is in a
process the agent cannot address, holding a tool the agent does not have.

Recovering the structural version needs a credential the gateway holds opaquely on the
agent's behalf. ADR 0011 surveyed that (Anthropic-vaulted `environment_variable`
credentials) and recorded why it is not available self-hosted. **NVB-22** (containerize
the gateway) is where this gets revisited; until then this is the strongest available
shape, and it is materially stronger than a per-agent auth profile, which is not a
boundary at all.

## Constraints this imposes

- **The tool prefix is derived from the server key, not equal to it.** Upstream: *"Server
  globs use the provider-safe MCP server prefix, not necessarily the raw `mcp.servers`
  key. Non-`[A-Za-z0-9_-]` characters become `-`, names that do not start with a letter
  get an `mcp-` prefix, and **long or duplicate prefixes may be truncated or
  suffixed**."* Two similarly-named servers can therefore collide and be auto-suffixed —
  which would silently rename the tool an allowlist is pinning. **Keep server names
  short, ASCII, letter-initial, and obviously distinct**, and read the resolved tool name
  back after adding one rather than assuming it matches the key.
- **A grant is verified by tool count, never by the absence of an error.** Established
  twice already on this server (`create_issue`, `create_pull_request_review` — both
  advertised, neither registered). Runbook 06 carries the credential-free `tools/list`
  recipe.
- **One server per principal, not one server with a per-caller credential.** NVB-17 asks
  the right question — *"how does the tool server know which principal is calling?"* — and
  this is the answer that needs no answer: separate processes, so there is nothing to
  bind per connection.
- **`openclaw secrets audit` does not see these.** Run against the live config it flags
  `gateway.auth.token` as plaintext and lists the six auth stores as legacy residue, but
  says nothing about the PAT in `mcp.servers.github.env`. Do not read a clean audit as
  "no plaintext tool credentials".

## Verification

Against the running gateway, 2026-08-17:

- `code-invariants` holds `github__issue_write … github__pull_request_review_write`; no
  other agent has any `github__*` tool, and `family`, `liron` and `aryeh` carry no
  `tools` key at all, so they resolve the global set. `main` is `deny: ["*"]`.
- `openclaw mcp probe` reports the one server and its 8 tools; the credential is in that
  entry's `env` and in no auth store.
- `deploy/check-agent-auth.sh` asserts the auth store stays model-only, and fails if a
  profile id outside the model-provider allowlist appears in any agent's store.
  `deploy/check-agent-auth.test.sh` covers it on a fixture.

## Consequences

- **NVB-17's credential-delivery section is superseded.** `LoadCredential=` per container
  cannot deliver a gateway-process tool's credential. Its acceptance criterion —
  *"credentials arrive per container and are not visible to another principal's
  container — demonstrated"* — is met in the weaker form described above, and the issue
  should say which it means.
- **NVB-18 gains a rule:** `create_draft` and the calendar tool arrive as per-person MCP
  servers. A single shared mailbox server with the owner's token, gated only by tool
  policy, is the shape to refuse.
- **`main` stays non-empty and that is fine.** No runbook, script or ADR should treat a
  non-empty default agent as a fault. What is a fault is an agent missing a profile id
  that `main` holds, or any non-model profile id appearing in the store at all.
