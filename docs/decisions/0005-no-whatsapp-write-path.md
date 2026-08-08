# 0005 — No write path to WhatsApp

**Status:** Accepted
**Related:** [0004](0004-signal-control-channel.md), [detection-model.md](../detection-model.md), [threat-model.md](../threat-model.md)

## Context

The obvious feature request for a WhatsApp assistant is that it replies in WhatsApp. Draft
a response, send it, done. It is also the single most expensive feature in the design, and
the cost is not implementation effort.

Sending from an automated client puts us squarely into detection layer L4 — account
behaviour. That layer keys on send rate, outbound/inbound ratio, unsolicited messages, bulk
content, and above all **recipient reports**. Everything that gets accounts banned lives on
the write side. Reading is invisible to it.

It also creates a security problem that no other part of the design has. The assistant reads
untrusted text from group chats. Give it the ability to send to those same group chats and
a successful prompt injection can make it *speak as me, to the people who injected it* —
irreversibly, immediately, and to an audience. Compare with the drafts-only email path
([0006](0006-two-process-privilege-split.md)), where the worst case is a file sitting in my
Drafts folder.

And the actual benefit is small. The assistant's value is *reading* — knowing what's in the
chats so it can act elsewhere. The composition step is where I want to be in the loop
anyway; these are messages to people I know, sent under my name.

Options considered:

- **Full send capability, rate-limited and human-paced** — mitigates L4 statistically, not
  structurally. Still one injection away from an outbound message. Still an arms race.
- **Send only to a whitelist of chats** — narrows the blast radius but keeps the entire ban
  surface and the entire "speaks as me" problem.
- **Draft in WhatsApp without sending** — no API for it. WhatsApp doesn't expose drafts to
  anything outside the app.
- **Draft elsewhere, I paste** — chosen.

## Decision

**The agent never sends to WhatsApp. There is no write path, and none will be added.**

The agent produces text over Signal. I copy it and paste it into WhatsApp myself.

This is a hard architectural boundary, not a default to be relaxed later. There is no
`send_whatsapp` tool, disabled or otherwise, anywhere in the codebase.

## Consequences

**Accepted:**

- Manual copy-paste for every WhatsApp reply. Roughly four seconds per message.
- No auto-replies, no "acknowledge and I'll handle it," no unattended WhatsApp behaviour of
  any kind.
- The assistant cannot act in WhatsApp while I'm asleep. Deliberate.

**Gained:**

- **Detection layer L4 is not merely mitigated, it is unreachable.** Outbound volume is
  zero, unsolicited messages are zero, and recipient reports — the strongest ban signal —
  are structurally impossible because there are no recipients. Nothing about our WhatsApp
  account behaviour differs from a person with a phone and a linked device.
- The entire ToS question on the write side disappears. We are not "automating messaging";
  we are not messaging.
- Removes the worst prompt-injection outcome in the system: an injected message causing an
  outbound message, under my name, to the people who wrote the injection.
- No rate limiting, no send queue, no human-typing simulation, no delivery-receipt handling,
  no retry logic. Meaningful code that doesn't need to exist.

## Do not revisit

The value of this decision is that it's absolute. A write path that exists "but is off by
default" restores L4 exposure the first time it's used and reintroduces the injection
outcome permanently, because the capability is what matters, not the default.

If WhatsApp replies become genuinely painful, the fix is better drafting and faster
handoff — not a send button.
