# 0007 — Principals on the control channel: more than one conversation, never one context

**Status:** Accepted
**Amends:** [0004](0004-signal-control-channel.md) · **Related:** [0006](0006-two-process-privilege-split.md)

## Context

[ADR 0004](0004-signal-control-channel.md) says the agent reads and writes **exactly one**
Signal conversation: the one with my number. That was right for an assistant with one user,
and the reasoning behind it — a phone number is not a secret, so the allowlist is the control
— does not depend on the list having one row.

The requirement changed: family should be able to use the assistant too. Each of them
messaging the same number, and each reaching something different — my mother getting web
search and her own calendar, not mine, and not my inbox.

That could be done by loosening the sender check, which would be the wrong half of the
system to loosen. So it is written down instead.

## Decision

**The gate forwards messages from a closed set of known one-to-one conversations. Each one
carries a named principal and a profile. Everything else is dropped before dispatch.**

- A **principal** is a configured sender: number (or UUID), a name, and a profile.
- A **profile** is a label the gate never interprets. The agent side reads it for the tool
  set, the credentials, and the confirmation route. The gate's job is to say *who*; deciding
  *what they may do* is a separate concern that lives with the thing holding the credentials.
- Anything from an unlisted sender is dropped and counted. Anything from a group is dropped:
  a group has no single sender, so it has no principal.

### One session per principal, and never a shared one

Each principal's messages go to an agent session of their own: own history, own tools, own
credentials or none.

This is [ADR 0006](0006-two-process-privilege-split.md)'s argument one level up. A shared
session means one principal's text sits in the context window while another principal's
credentials are in reach — the same failure the reader/agent split exists to prevent, with
the trust boundary moved from "WhatsApp vs. Signal" to "them vs. me". A per-principal filter
over one session would be a single bug away from total; a separate session makes the
capability *absent* rather than *withheld*, which is the pattern this project keeps choosing
(no `send_whatsapp`, `create_draft` and not `send_email`).

### The Signal side stops being uniformly trusted

ADR 0004 called the Signal channel the trusted input side. With one principal that was
true by construction. It no longer is: a family member forwarding a scam text, or repeating
what a message told them to ask for, puts attacker-influenced content on the privileged wire
— exactly what ADR 0006 keeps off it from the WhatsApp direction.

Two things carry that weight, and neither is a filter on message text:

1. **The session boundary above.** Injected instructions in a family member's turn reach
   only that principal's tools and credentials.
2. **A profile holds nothing its principal does not already own.** My mother's profile can
   hold her calendar credential; it must not hold mine. The blast radius of a successful
   injection through her conversation is then bounded by what she could have done herself by
   asking.

### Confirmations

The confirmation gate is per profile, `confirm_via`:

- **Today: `owner`.** Anything a non-owner triggers that needs authorisation prompts me, in
  my conversation, naming who asked. They may request; I authorise.
- **Later: `self`,** once a profile carries only its own principal's credentials. At that
  point authorising an action can only affect that principal's own accounts, so routing it
  through me buys nothing except my attention, and an approval gate nobody reads is worse
  than no gate at all.

The owner's profile is the only one that may hold the owner's credentials. That is the
condition that makes `self` safe, and it is why the setting is per profile rather than
global.

## Consequences

**Accepted:**

- The invariant in [AGENTS.md](../../AGENTS.md) changes from *exactly one conversation* to
  *a closed set of known conversations*. The control is unchanged in kind: an allowlist,
  checked before anything runs, refusing everything else.
- **Adding a principal is a security act**, not a convenience. It grants someone the ability
  to invoke a process running on my Pi. The config asks for a name and a profile precisely so
  that adding a row means answering "as what?".
- More sessions on one Pi: memory and per-principal state to keep. Waydroid already wants the
  8GB, so this has a ceiling that will need measuring rather than assuming.
- Multi-principal is plumbing — routing, per-profile tool policy, session lifetime — and
  [Q4](../OPEN-QUESTIONS.md) says to revisit the custom-vs-framework decision if the plumbing
  starts to dwarf the agent logic. This is that evidence arriving; the decision is still open
  and belongs to M4.

**Gained:**

- The assistant is useful to more than one person without the sender check being weakened for
  any of them.
- Blast radius is per principal, not per system.
- The gate stays a small deterministic thing that says who is talking, and stays out of the
  business of what they are allowed to say.
