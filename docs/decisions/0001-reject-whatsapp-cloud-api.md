# 0001 — Reject the WhatsApp Cloud API

**Status:** Accepted — 2026
**Supersedes:** nothing
**Related:** [0002](0002-waydroid-companion-device.md), [detection-model.md](../detection-model.md)

## Context

The official, sanctioned, ToS-clean way to programmatically interact with WhatsApp is the
WhatsApp Business Platform (Cloud API). It's the first thing to evaluate, and if it worked
it would dominate every alternative — no container, no database reading, no ban risk, no
spike.

It doesn't work, for four independent reasons.

**1. Groups are effectively unavailable.** The Groups API caps at 8 participants and is
gated behind a business qualification — roughly, 100k+ monthly conversations. My use case
is reading group chats with a few dozen people in them, as a private individual. Both
halves of that fail.

**2. Meta banned open-ended AI assistants on the platform.** As of Jan 2026, general-purpose
AI assistant bots are prohibited on the Business Platform. The policy is aimed exactly at
what this project is.

**3. It's a business messaging product, not a personal one.** It assumes a business
identity, a phone number registered to that business, and a customer-service conversation
model. My personal account with my personal group chats cannot be attached to it — moving
my number to the Business Platform would mean losing normal WhatsApp on that number.

**4. Cost model turns hostile.** From Oct 1, 2026, service messages inside the 24-hour
window become billable. Even if the above were solvable, the economics of an always-on
assistant reading chats become bad.

Any one of these would be a blocker. (2) and (3) are unfixable by any amount of
engineering.

## Decision

**Do not use the WhatsApp Cloud API / Business Platform.** It is structurally incapable of
supporting a personal AI assistant reading personal group chats, and no future refinement of
the design brings it back into range.

Use the read-only, non-API approach instead: official app in a container, read the local
database. See [0002](0002-waydroid-companion-device.md) and [0003](0003-local-db-read.md).

## Consequences

**Accepted:**

- We have no sanctioned API, and therefore no ToS-clean path. The design must instead
  minimise ban *risk* by construction — that's what [detection-model.md](../detection-model.md)
  is for.
- We take on operational complexity (a container, a spike, a pinned APK) that the API
  would have removed.
- There is no support channel and no stability guarantee. Schema changes are our problem.

**Gained:**

- Access to group chats at all, which the API cannot provide.
- No per-message cost.
- No business identity, no number migration, no loss of normal WhatsApp.

## Do not revisit

This was evaluated in full. Revisit only if Meta ships a *personal* (non-Business) API with
group read access — a different product that does not currently exist. Incremental changes
to the Business Platform's pricing or limits do not reopen this.
