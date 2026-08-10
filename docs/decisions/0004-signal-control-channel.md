# 0004 — Signal (signal-cli, dedicated number) as the control channel

**Status:** Accepted
**Related:** [0005](0005-no-whatsapp-write-path.md), [0006](0006-two-process-privilege-split.md)

## Context

I need to talk to the assistant from my phone: issue commands, receive summaries, and
answer confirmation prompts. The channel needs to be always-on, mobile-first, low-friction,
and — because it carries the confirmation gate — **trusted**.

Options:

| Option | Verdict |
|---|---|
| WhatsApp itself | Requires a write path. Rejected outright — see [0005](0005-no-whatsapp-write-path.md). Also collapses the trusted and untrusted channels into one, which destroys the trigger asymmetry in [0006](0006-two-process-privilege-split.md). |
| Telegram bot | Easiest API by far. But: another account, weaker privacy posture, and the assistant's traffic would sit in a service I don't otherwise use for anything sensitive. |
| Email | Latency and threading are wrong for interactive confirmation. And the agent already has Gmail as a *capability* — using it as the control channel too creates a loop where an injected draft could look like a control message. |
| Matrix / self-hosted | Another service to run on the same Pi that this project already loads. |
| SSH / web UI | Not mobile-first. I won't use it, so the confirmation gate rots. |
| **Signal via signal-cli** | **Chosen.** |

Signal has **no public bot API**. [`signal-cli`](https://github.com/AsamK/signal-cli) (Java,
with a JSON-RPC daemon mode) is the standard route, is widely used, and Signal does not ban
accounts for it. Unlike the WhatsApp situation, there's no adversarial detection posture to
reason about here.

Second choice: dedicated number vs. linking signal-cli as a device on my personal account.
Linking is less setup, but it puts the assistant inside my own identity — the assistant's
messages become my messages, conversations land in note-to-self, and the assistant holds key
material derived from my account.

## Decision

Use **Signal** as the control channel, via **signal-cli in JSON-RPC daemon mode**, with the
assistant on a **dedicated phone number registered as its own Signal account** — not a
linked device on my personal account.

The control channel is the **trusted** input side of the system. Only a Signal message from
my number triggers the privileged agent ([0006](0006-two-process-privilege-split.md)), and
the sender is verified against a configured allowlist before anything runs.

### One conversation, and nothing else

**The agent reads and writes exactly one Signal conversation: the one with my number.**
Everything else that arrives — any other sender, any group, any request to start a new
thread — is dropped before dispatch, not filtered somewhere downstream. The agent has no
capability to address a Signal recipient it was not configured with, in the same way it has
no `send_email` ([0006](0006-two-process-privilege-split.md), control 1).

This holds regardless of which account the assistant runs on. It is the control that carries
the weight, and it does not depend on the number being secret.

### What the dedicated number does and does not buy

**It does not prevent unauthorized invocation.** Anyone who learns the number can send the
assistant a message. Nothing about registering a separate account changes that. The control
that refuses those messages is the sender allowlist above, and it would be exactly the same
check on a linked device. A phone number is not a secret and must never be treated as one.

What the separate account buys is **blast radius**, and it is worth being precise that this
is the whole of it:

- **Key material.** The Pi runs Waydroid, WhatsApp, and a credential-holding agent; it is the
  most attackable component in this design. With a linked device, its key material is derived
  from my account, so root on the Pi means reading my entire Signal history and sending as
  me. With a separate account, it means an inbox with nothing in it.
- **Untrusted text reaching the privileged process.** A linked device receives every Signal
  message anyone sends me. A dedicated account receives messages from people who know a
  number given to nobody. Both are dropped by the same allowlist; the difference is how much
  attacker-controlled text passes through the privileged process on its way to being dropped,
  and therefore what a bug in that check would cost.
- **Revocability.** Killing the assistant doesn't touch my Signal.

None of that is a security absolute, and the cost is real: a number to source and keep alive
([Q3](../OPEN-QUESTIONS.md)). If that cost ever exceeds the blast-radius benefit, the linked
device is a legitimate choice — but it is a *blast-radius* trade, not a loosening of the
invocation control, and the one-conversation restriction above stays either way.

## Consequences

**Accepted:**

- **A dedicated number must be sourced and kept alive.** Open — see
  [OPEN-QUESTIONS Q3](../OPEN-QUESTIONS.md). Signal registration needs SMS or voice verification,
  and some VoIP ranges are blocked.
- A Java runtime on the Pi. Not free, but fine on 8GB.
- signal-cli's account state directory holds the account's key material. It's in
  `.gitignore`, it stays off the repo, and losing it means re-registering.
- Registration is per-number: if the number lapses, the account is gone.
- No formal API stability guarantee. signal-cli's JSON-RPC interface has been stable in
  practice; pin the version anyway.

**Gained:**

- A clean two-party conversation between me and the assistant, properly separated from my
  own account.
- Revocable independently — killing the assistant doesn't touch my Signal.
- Its key material is not my key material.
- A trusted channel structurally distinct from the untrusted one (WhatsApp), which is what
  makes trigger asymmetry possible: WhatsApp content can never initiate a privileged action
  because it arrives on a different wire entirely. Note the asymmetry is *between* the two
  wires — there is no WhatsApp-to-trigger path at all. It is not a claim about senders
  within Signal, where the allowlist is the only thing separating me from anyone else.
- End-to-end encrypted transport for confirmation prompts, which carry the descriptions of
  actions about to be taken with my credentials.
