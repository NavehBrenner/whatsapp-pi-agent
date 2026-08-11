# 0009 — The gate is the only Signal endpoint; agents are containers that ask by name

**Status:** Accepted — implementation tracked in [NVB-11](https://linear.app/naveh-brenner/issue/NVB-11), [NVB-14](https://linear.app/naveh-brenner/issue/NVB-14), [NVB-17](https://linear.app/naveh-brenner/issue/NVB-17)
**Related:** [0006](0006-two-process-privilege-split.md), [0007](0007-principals-on-the-control-channel.md), [0008](0008-authority-is-a-conversation-sender-pair.md)

## Context

[NVB-10](https://linear.app/naveh-brenner/issue/NVB-10) built a gate that decides which
inbound messages are commands. The agent that consumes them does not exist yet, and two
questions have to be answered before it does: **how does it speak**, and **what stops one
principal's agent reaching another's?**

The obvious answer to the first — let the agent talk to `/run/wpa-signal/socket` — is wrong,
and not subtly. A JSON-RPC client of signal-cli does not get a send-only channel; it gets the
receive stream too. Giving the agent socket access hands the privileged process holding real
credentials every inbound envelope from every stranger, which is the exact thing the gate
exists to prevent.

The second question was answered in [ADR 0007](0007-principals-on-the-control-channel.md) as
a promise — no shared sessions, no profile holding credentials its principal does not own —
without saying what enforces it.

## Decision

### The gate is the only process that touches Signal

The agent writes `{"to": "<name>", "text": "..."}` to an outbox. The gate resolves the name
through **its own roster**, checks it against the send list for that agent's profile, and
sends. Refusals are counted and logged like inbound drops.

Two properties follow, and they are the reason for the indirection:

- **The agent never handles an identifier**, so it cannot forge or exfiltrate one.
- **The addressable set is exactly the roster.** Not "whatever the agent asked for,
  validated" — a name that is not in the roster resolves to nothing at all.

The default send list is `["self"]`: the conversation the command arrived from. An agent that
has been granted nothing can still answer its own principal and reach no one else. A send
list may contain only principals; messaging someone who cannot message back is a distinct
capability and needs its own decision.

The roster earns its keep a second way: **ACIs are not stable.** Re-registering a number
mints a new one, while the agent's memory of people is keyed on names. A rebind is a one-line
config edit rather than an assistant that has forgotten who its family is.

### One container per principal

Isolation is enforced by the kernel, not by the agent's good behaviour. Each principal's
agent runs in its own container: own mount namespace, own network policy, own credential
store, supervised as a systemd unit like everything else here.

Per-unit hardening (`DynamicUser`, `StateDirectory`, `ProtectSystem=strict`) is also
kernel-enforced and was the cheaper option. It is weaker in *scope* rather than in
enforceability — one filesystem tree, one network stack — and the boundary is harder to point
at. When the second principal is a person trusting the system with their calendar, a boundary
you can point at is worth the overhead.

Measured on the Pi, 2026-08-11: `/dev/kvm` present, 6398MB available with Waydroid, the JVM
and the gate all running, no container runtime installed, Waydroid on LXC. **Firecracker or
cloud-hypervisor microVMs are therefore feasible and are the documented upgrade**, at roughly
256–512MB per agent for a guest kernel and rootfs — competing with Waydroid for the same RAM.
The trigger to take it: a profile holding credentials whose loss could not be absorbed, or a
principal from outside the family. An SSD helps I/O, wear and image layers; it does not help
the binding constraint, which is memory.

### The mount surface is small on purpose

An agent container gets its queue, its outbox and its own state. It has **no** access to
Waydroid, no root, no `/dev/shm` snapshot, and never opens msgstore — that is
[ADR 0006](0006-two-process-privilege-split.md)'s line, and it does not move because the
agent got containerised.

WhatsApp context is a **capability granted per profile, defaulting to none**, served by a
broker answering scoped queries ("last N from chat X, if this profile may see chat X") rather
than by handing over a file. One family member's agent must not read another's groups, and
the reader's existing chat allowlist is the machinery to say so.

Note also that WhatsApp never notifies anything: the reader polls a snapshot every 30s, and
hooking was rejected outright ([detection-model.md](../detection-model.md)). There is no
event for an agent to subscribe to.

### Agents never talk to each other directly

"Requesting permission to view the calendar" is a **brokered** request: the agent asks the
broker, the broker turns it into a confirmation prompt to the owner, and on approval performs
the scoped call or issues a one-shot token. A credential never passes between agents.

If two agents can open a channel to each other, the isolation above is one socket away from
being decorative. v1 may have no inter-agent channel at all — what it may not do is make the
broker impossible.

## Consequences

**Accepted:**

- Another queue. The agent cannot send directly, so every reply is a file the gate drains.
- A container runtime on a Pi that is already running Waydroid's LXC and a JVM. **Waydroid
  manages its own bridge and NAT**, so a runtime that rewrites iptables is the most plausible
  way to break the WhatsApp side — rootless Podman is preferred partly for that reason, and
  "Waydroid still RUNNING, messages still arriving" is an acceptance criterion rather than an
  assumption.
- Memory per idle agent has to be measured, not assumed, before the third principal.
- The broker becomes a component that must exist before an agent can see any WhatsApp
  context at all — the simpler bind-mount was rejected for a reason that only bites with more
  than one principal.

**Gained:**

- A compromised agent can address exactly the people its profile lists, by name, and holds no
  identifier to reach anyone else.
- A compromised agent sees no inbound Signal traffic whatsoever — including messages the gate
  refused.
- Isolation between principals that a bug in agent code cannot undo.
