# 0010 — Profiles are pre-bound grant bundles, and the container is the enforcement

**Status:** Accepted — implemented in [NVB-12](https://linear.app/naveh-brenner/issue/NVB-12), with the container half in NVB-14/15
**Amends:** [0007](0007-principals-on-the-control-channel.md), [0009](0009-agents-are-containers-that-ask-by-name.md) · **Extends:** [0008](0008-authority-is-a-conversation-sender-pair.md)

## Context

[ADR 0008](0008-authority-is-a-conversation-sender-pair.md) made authority a property
of the (conversation, sender) pair and said each pair carries "its own profile". It
did not say what a profile *is*. Left there, `profile = "family"` is a string the
gate passes along and something downstream interprets — which is the shape of every
authorisation system that later turns out to have been advisory.

Two requirements forced the question:

- **A group sender must not be a binary pass/ignore.** In a family room, the owner
  and their mother should reach different agents with different reach.
- **The spread has to run coarse to fine**, the way a GitHub token does: one broad
  bundle for the owner's own chat, and a bundle holding exactly one capability over
  exactly one account for a shared room.

## Decision

**A profile is a named bundle of tool instances, bound to a pair in config before any
agent exists.** `[[agent.profiles]]` lists `tools`; each entry names a row in
`src/agent/registry.py` that is already bound to one account and one verb.

### Granularity comes from the registry, not from a scope language

`calendar.family.rw` and `calendar.family.create_event` are two grants over one
calendar; `calendar.personal.rw` is a different account. Fineness is *which
instances are listed*, so there is no wildcard syntax, no matcher, and no policy
evaluated at runtime — nothing to write and nothing to get subtly wrong. A tool name
absent from the registry refuses to start, because a profile that appears to grant
something and grants nothing is invisible until the day someone relies on it.

### The container is the enforcement, so there is no token to steal

A PAT is held by the caller and presented at request time; it can be stolen, replayed
or talked out of someone. A profile is compiled into the container: the image carries
that profile's tools, systemd mounts that profile's credentials, and everything else
is **absent**. `email.personal.draft` in a container without the mailbox credential
does not fail a check — there is nothing to call.

So the gate emits the profile *name* and nothing more. It deliberately does **not**
copy the tool list into `commands.jsonl`: a capability list travelling in a JSON line
is a label the runner would have to trust, and a boundary that a message can describe
is a boundary a message can lie about.

```
pair → profile → tool list → image + mounted credentials → the tools that exist
```

### The container is named in config, and derived from nothing

Each conversation declares the `agent` that serves it; a sender row may override it.
An earlier draft derived the name from the pair, making it impossible to express a
shared family agent — so the name is a choice, and a choice belongs in config rather
than in a rule that a generator has to re-apply.

`deploy/render-agents.py` (NVB-14) is the single place that turns these names into
material: `wpa-agent:<profile>` images, `wpa-agent@<agent>.service` drop-ins with
their `LoadCredential=` sets, and the outbox bind. The gate names an agent; it never
starts one.

### Two rules keep a shared agent safe

This is where ADR 0007's *"no two principals share an agent session"* is amended
rather than quietly contradicted. It becomes:

1. **No agent session spans two conversations.** Inside one room, sharing a session
   costs nothing that is not already shared — everyone reads everyone's messages, so
   the disclosure boundary and the injection surface are the room itself. Across
   rooms it costs the containment that matters: one session spanning a private chat
   and a group would carry group text into the private chat and let an injection in
   the group act with the private chat's credentials. It is also what makes outbound
   `self` well defined for a shared agent — one conversation, one answer.
2. **Senders resolving to one agent resolve to one profile.** An agent *is* its tools
   and its mounted credentials; varying capability per speaker inside one container
   would be a runtime string check, which is the enforcement this ADR removes.
   Different profiles therefore mean different agents, which is exactly the mixed
   case: the owner on a wider profile and everyone else on a narrow one, two
   containers in one room.

Both are checked at load; the gate refuses to start on either.

### Authority is never editable over the chat

There is no message that grants a capability. The messages the agent reads are the
injection surface, so a "grant me X" path reachable over chat is a
privilege-escalation path reachable by prompt injection. Permanent grants are the
owner editing config on the Pi.

That is separable from *one-off* actions, which are not permission changes: ADR
0008's targeted confirmations approve a single action by name, and a request needing
someone else's credentials is brokered to *their* agent rather than lending a tool
across containers — mom's container still has no credential to run it with. The
ladder is: in-profile → it happens; out-of-profile but brokered → one approval;
genuinely new capability → a config edit.

## Consequences

**Accepted:**

- More configuration, and configuration where a mistake is a refusal to start rather
  than a silently different grant. Six such refusals exist (dangling profile, unknown
  tool, duplicate pair name, agent spanning conversations, agent with two profiles,
  send list naming no conversation), and `--check` prints the resolved matrix so the
  answer to "what can this thing do" is generated rather than remembered.
- The registry is a closed list. Adding a capability means editing code, which is the
  point: `send_email` cannot appear because drafting got tedious (AGENTS.md).
- Non-technical family members configure nothing. What they get is a deterministic
  list of what their agent can do, a fixed refusal when they ask for more, and a
  person to ask — all host text, never model prose.

**Gained:**

- The coarse/fine spread of a fine-grained PAT, with no token in existence to steal.
- A shared family agent, without anyone in the room inheriting the owner's reach.
- One place — config — that answers both "who may command this" and "as what".
