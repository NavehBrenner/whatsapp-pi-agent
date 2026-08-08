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
  because it arrives on a different wire entirely.
- End-to-end encrypted transport for confirmation prompts, which carry the descriptions of
  actions about to be taken with my credentials.
