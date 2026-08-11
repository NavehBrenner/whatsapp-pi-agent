# 0008 — Authority is a (conversation, sender) pair, and confirmations are targeted

**Status:** Accepted — pair authority implemented in [NVB-12](https://linear.app/naveh-brenner/issue/NVB-12); targeted confirmations remain [NVB-16](https://linear.app/naveh-brenner/issue/NVB-16)
**Amends:** [0007](0007-principals-on-the-control-channel.md) · **Extended by:** [0010](0010-profiles-are-pre-bound-grant-bundles.md), which says what a profile *is* · **Related:** [0004](0004-signal-control-channel.md), [0006](0006-two-process-privilege-split.md)

## Context

[ADR 0007](0007-principals-on-the-control-channel.md) made the control channel carry named
principals with profiles, and kept ADR 0004's refusal of group messages: a group has no
single sender, so it has no principal.

Two requirements break that:

- **A family group.** The assistant sitting in a group where several people may address it,
  each with different authority — my mother asking it something is not me asking it.
- **A dedicated confirmation conversation**, whose entire purpose is answering prompts, so a
  stray message in the main chat can never be read as authorisation.

Both are conversations with more than one sender, which the current model cannot express at
all. And the obvious shortcut — "check the sender, ignore the conversation" — is wrong in a
way that matters: it would give a family member in a group the same authority they have in
their own chat, in a room where the reply is read by everyone present.

## Decision

**Authority is a property of the (conversation, sender) pair.** Not of the sender, and not of
the conversation. The same person in two conversations is two principals with two profiles.

The gate holds a table of conversations. Each entry names permitted senders and the profile
that applies to each of them *there*:

- **Groups are keyed by group id, never by name.** Group names are chosen by members, so a
  name-keyed allowlist can be renamed into — the same trap the WhatsApp chat allowlist avoids
  by keying on JIDs rather than subjects.
- **A sender not listed for that conversation is dropped and counted**, exactly like an
  unknown sender in a one-to-one. In a family group that is where probing will appear.
- **A conversation not in the table is dropped whoever sent it.**

### A group profile must be narrower, because the reply is disclosed

In a one-to-one, the answer reaches one person. In a group it reaches everyone in the room,
including people who did not ask and may not be principals at all. So a group profile is an
**egress** boundary and not only an invocation one, and the same person's group profile must
be narrower than their one-to-one profile.

The failure this prevents is not exotic: "the owner asked, so use the owner's capabilities"
reads the owner's private calendar aloud to the family.

### Membership is pinned, and drift refuses rather than degrades

Group membership changes. Somebody added later is inside a conversation that was allowlisted
before they arrived, and no code will notice unless it is asked to.

The gate verifies membership against a pinned set and **refuses when it differs**. The
failure mode to build is *this stops working and you are told* — never *this keeps working
with an extra person present*. The confirmation conversation is strictest: a changed member
set means confirmations stop being honoured until a human looks.

### Confirmations are targeted

A `YES` that means "proceed" is not merely vague. With two prompts outstanding — ask for
something that drafts an email *and* creates an event — it authorises the **wrong** action,
silently. Newest-pending-wins is therefore rejected.

Signal has no interactive messages: no buttons, no cards, no quick replies, and no bot API to
hang them off. The two affordances that exist both name a specific message, and both were
verified on hardware on 2026-08-11:

| Mechanism | What it carries | Status |
|---|---|---|
| Quoted reply | `quote.id` — the timestamp of the assistant's own message — plus `author`, `authorUuid`, `text` | **Captured.** Produced `reply_to=1786454056853` on an accepted command. |
| Reaction | `reaction.targetSentTimestamp`, `emoji`, `targetAuthorUuid`, `isRemove` | **Captured.** A bodiless `dataMessage`, currently dropped as `no body`. |

So a confirmation must be **targeted, single-use, expiring, and bound to its pair**:

- The reply quotes the prompt or reacts to it, and `quote.authorUuid` /
  `reaction.targetAuthorUuid` must be the assistant's own ACI — otherwise, in a group,
  quoting somebody else's message could collide with a prompt id.
- Consumed on approval. A second approval of the same prompt does nothing, and an `isRemove`
  reaction afterwards does not unspend it.
- Expires (15–30 min). A `YES` tapped days later would authorise an action whose context is
  gone.
- Answerable only by the pair it was addressed to. One principal's approval can never answer
  another's prompt.
- If reactions are honoured, a **designated emoji** — any-emoji-approves is too easy to do by
  accident for something that spends real credentials.
- Refusals are deterministic host text, never model prose.

## Consequences

**Accepted:**

- The invariant becomes: *a closed set of conversations, each with a set of permitted
  senders, each pair carrying its own profile; everything else dropped before dispatch.*
- More configuration, and configuration that is easy to get subtly wrong — a group entry
  grants authority to several people at once. It asks for a profile per sender precisely so
  that adding a row means answering "as what, in here?".
- Membership pinning needs a source of truth for who is in a group and a periodic check.
  That is a live dependency on signal-cli's group listing, not a static config.
- A reaction becomes security-relevant input, so the gate's "a trigger needs a body" rule
  gains one narrow exception: a reaction counts only when it targets a pending prompt.

**Gained:**

- The assistant can live in shared rooms without anyone in them inheriting the owner's reach.
- A confirmation channel where nothing is a command by construction, rather than by an agent
  choosing to ignore things.
- Approvals that mean exactly one action, with two outstanding.
