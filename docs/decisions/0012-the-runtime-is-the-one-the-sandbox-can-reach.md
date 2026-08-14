# 0012 — The runtime is the one the sandbox can reach

**Status:** Accepted 2026-08-14, **deployed and verified on hardware the same day**
([NVB-23](https://linear.app/naveh-brenner/issue/NVB-23)) — evidence in
[Q6](../OPEN-QUESTIONS.md), isolation work it unblocks in
[NVB-14](https://linear.app/naveh-brenner/issue/NVB-14)

> **What deployment settled.** Three of this ADR's open questions now have hardware
> answers. **xAI OAuth is obtainable**, and *one subscription permits a device-code
> login per agent* — `owner` and `family` each hold their own profile from the same
> account, so the "let main hold the model credential only" fallback below is **not
> needed** and `main` stays empty. **Per-agent existence separation holds on the built-in
> runtime**: with a narrowing on one agent, `owner` reported
> `apply_patch, edit, read, session_status, write` and `family` reported
> `read, session_status`. **Durable memory works** — a file written by the agent landed
> in the real host workspace and was read back in a fresh session.
>
> Two things this ADR did not anticipate, both in the changelog in full. **It assumed a
> sandbox backend existed and none did** — OpenClaw registers only `docker` and `ssh`,
> so `sandbox.mode: "all"` would have isolated nothing while looking entirely
> successful. And **`tools.profile: "minimal"` strips the file tools**, so the memory
> path this ADR exists to restore was still dead after the runtime moved, until
> `alsoAllow` put them back. Not denying a tool is not the same as having it.
**Amends:** [0011](0011-openclaw-owns-the-channel-the-gate-owns-the-room.md) (agent runtime, and the billing premise underneath it), [0006](0006-two-process-privilege-split.md) (which vendor sees family message content)

## Context

[ADR 0011](0011-openclaw-owns-the-channel-the-gate-owns-the-room.md) chose OpenClaw as
the gateway and pinned `models.providers.anthropic.agentRuntime.id: "claude-cli"`, so the
assistant would run on the Claude subscription already being paid for rather than on
metered API credits. That pin was made on billing grounds. Nothing at the time suggested
it constrained isolation.

NVB-14 then asked for the thing this project has repeated since
[ADR 0009](0009-agents-are-containers-that-ask-by-name.md): isolation the kernel enforces,
not isolation that holds because policy code is correct. Settling it meant reading the
shipped `dist/` of `openclaw@2026.7.1-2` — the exact version on the Pi — rather than the
prose docs, which had already proved unreliable on this codebase.

The pin turned out to foreclose the requirement. Three findings, each verified against
shipped code and recorded in full under Q6:

- **The sandbox never reaches the CLI.** OpenClaw sandboxes *its own* tools — `exec`,
  `read`, `write`, `edit`, `apply_patch`, `process`. The gateway stays on the host, and
  under `claude-cli` the CLI is a host subprocess, so its native `Bash`/`Read`/`Write` run
  beside the gateway as the gateway uid. `sandbox.mode: "all"` governed nothing that
  executed.
- **The MCP bridge will not substitute.** `NATIVE_TOOL_EXCLUDE` is exactly those six
  names, because `claude-cli` declares `nativeToolMode: "always-on"` — OpenClaw assumes the
  CLI brings its own file tools and withholds replacements.
- **Per-agent tool policy was never the problem.** Tool separation over the bridge is real
  and is *existence* separation: the same scoped list answers `tools/list` and
  `tools/call`, so an agent is never told a capability exists that it cannot use.

## The bind those create

Memory is plain Markdown in the agent workspace — `MEMORY.md`, `memory/YYYY-MM-DD.md` —
written with an ordinary file-write tool. So under `claude-cli` on one gateway:

- Floor the CLI's native tools, and **no writer exists**. The agent cannot remember
  anything.
- Un-floor them, and `Write` accepts **absolute paths** as the shared `openclaw` uid. Every
  agent can read every other agent's credential store, sessions, and workspace.

**Durable memory and agent isolation were mutually exclusive.** `compaction.memoryFlush`
does not rescue it: it is a silent *agentic* turn that prompts the model to write, needing
the tool that is absent, and OpenClaw skips the whole compaction flow for backends
declaring `ownsNativeCompaction`, which `claude-cli` does.

Nor could the subscription be kept while fixing it. For Anthropic, OAuth **is** Claude CLI
reuse; there is no Anthropic OAuth token OpenClaw's own transport can send. Subscription
auth and sandbox coverage are structurally exclusive there.

## Decision

**Run the agent on OpenClaw's built-in runtime, and pick the provider from the set whose
credentials that runtime can use.** Concretely:

- `agentRuntime.id: "openclaw"`, **pinned**. `auto` prefers a registered harness when one
  supports the provider, so the pin is load-bearing rather than cosmetic.
- Provider **xAI/Grok via OpenClaw-managed OAuth** — subscription billing, no registered
  harness, therefore sandboxed. Qwen is the equivalent fallback.
- `sandbox.mode: "all"`, `scope: "agent"`, and **`workspaceAccess: "rw"`**.
- The `cliBackends` block is deleted. It governed `claude-cli` only.

`read`/`write`/`edit` become real OpenClaw tools: subject to per-agent policy *and*
executed inside the container. Memory works, and `~/.openclaw/agents/*/agent/*.sqlite`,
session stores, and other agents' workspaces become unreachable by the kernel rather than
by policy being right.

### `workspaceAccess` is `rw` or the decision fails

The agent's own workspace is bind-mounted into its container: the sandbox does not cut the
agent off from its own files, only from everything else. `none` gives a throwaway
directory under `~/.openclaw/sandboxes`, so memory would silently not persist. `ro`
disables `write`/`edit`/`apply_patch` outright. Only `rw` delivers what this ADR is for.

### Why not simply strip the CLI's tools and hand it OpenClaw's

The obvious cheaper alternative, and it is not expressible. `NATIVE_TOOL_EXCLUDE` has
**one** call site and is passed unconditionally into the loopback tool resolver;
`nativeToolMode` is a backend plugin declaration, absent from both config references. The
bridge strips those six names before the tool list is built, on every request, and no
configuration changes that.

### What this does not buy

**The built-in runtime is not architecturally safer.** Were the configuration above
expressible under `claude-cli`, the two would be roughly equivalent — sandboxed file tools,
the same per-agent policy, the same mechanisms. We are not switching to a better security
model; we are switching because **the model we want is only reachable there**.

It also leaves the larger gap untouched. NVB-17/18's calendar and mail tools make outbound
HTTPS calls, and there is no dispatcher that could route them into a container:
`resolveSandboxToolPolicyForAgent` is an allow/deny policy layer, not a routing table. So
the credentials that matter most sit in the gateway process on **every** runtime.

That splits the original NVB-14 cleanly, and the split is the useful part of this ADR:

> **Switching runtimes bounds what a compromised agent can reach. Containerizing the
> gateway bounds what a compromised gateway can reach.**

AGENTS.md's standing question — "what does a successful injection do with this" — is about
the first, and injection is by far the likelier event. So gateway containerization is
retained as an upgrade with a trigger, in the shape
[ADR 0009](0009-agents-are-containers-that-ask-by-name.md) already uses for microVMs,
rather than as a prerequisite.

### The vendor question, which is not a technical one

This moves family message content from Anthropic to xAI. [ADR
0006](0006-two-process-privilege-split.md) treats who can see household data as a decision
to be made deliberately, so it is recorded here as a decision and not as a side effect of a
runtime choice: **we accept a second vendor seeing family message content in exchange for
kernel-enforced isolation between family members.**

The alternative that keeps data with Anthropic is gateway containerization, which was
deferred above on likelihood grounds. If the vendor change is unacceptable, that deferral
inverts and containerization becomes the prerequisite instead. It is a real fork, and it is
the one to revisit first if this ADR is ever reopened.

### The billing premise underneath ADR 0011 has changed

ADR 0011 argued partly from February 2026 terms prohibiting subscription reuse by
third-party apps, server-enforced from April 2026. OpenClaw's docs cite Anthropic's **15
June 2026** support update stating that Claude Agent SDK, `claude -p`, and third-party app
usage draw from the signed-in subscription's limits. The premise is out of date. It is not
load-bearing for this ADR — the decision rests on sandbox reachability, not billing — but
it must not be cited again without rechecking Anthropic's own support articles.

### What would reopen this

- OpenClaw exposes `nativeToolMode` or the loopback exclusion as configuration, making the
  cheaper alternative expressible.
- Anthropic ships an OAuth token usable by a non-CLI transport.
- xAI OAuth turns out to be unobtainable for this account, or a single subscription refuses
  the several device-code logins the per-agent auth stores need.
  *(Settled 2026-08-14: obtainable, and one subscription permits a login per agent.)*
- Model quality on family-facing work proves materially worse than the Claude baseline.
- ~~Web search, image and video generation are judged necessary.~~ **Withdrawn
  2026-08-14 — this was based on a wrong finding and never applied.** `web_search` runs
  fine alongside per-agent sandboxing (see Consequences), so no capability-vs-isolation
  fork exists here and NVB-22 stays a deferred upgrade rather than a prerequisite.

## Consequences

- **Every agent needs its own xAI OAuth login.** Credential read-through is two-tier —
  local agentDir, then the default agent's store — and the "main holds nothing" invariant
  deliberately empties the fallback. If one subscription will not permit that, the narrow
  fix is to let main hold **the model credential only**: it is the one credential that is
  not a differentiator, since every agent needs inference and a shared subscription under a
  provider-side cap is already accepted. Tool credentials stay out of main, which is the
  part that was ever load-bearing.

  **Settled 2026-08-14: one subscription does permit it.** `owner` and `family` each
  hold their own `xai/oauth` profile issued to the same account, with independent
  expiries. The fallback above was never exercised and `main` remains empty, so the
  invariant stands as written rather than as a compromise.
- **`tools.deny` must stop denying the file tools.** They are now the memory path.
  `tools.profile: "minimal"` also needs revisiting, since it strips `web_search` — exactly
  what the least-trusting family profile is for.
- **Session stores must be cleared** when the tool policy changes. Policy binds at session
  creation, and a tightened policy does not reach an existing session.
- **The web search tool arrives free, and it works alongside the sandbox.** Confirmed on
  hardware 2026-08-14 ([NVB-26](https://linear.app/naveh-brenner/issue/NVB-26)): with
  `sandbox.mode: "all"`, `scope: "agent"` and per-agent containers running, both the owner
  and the family group return live results through `web_search` on the agent's own xAI
  OAuth, with no separate key. `exec` stays refused and memory still persists.

  **Getting there needed one non-obvious token, and its absence fails silently.** The
  sandbox tool allowlist must contain **`group:plugins`**:

  ```json5
  tools: {
    alsoAllow: ["read", "write", "edit", "apply_patch", "web_search"],
    web: { search: { enabled: true, provider: "grok" } },
    sandbox: { tools: { allow: [ /* … */ "group:plugins", "web_search"] } },
  }
  ```

  An allowlist built only from known core tool names resolves `includePluginTools` to
  false, which drops the xAI plugin's provider registrations — and a tool whose provider
  never registered does not appear, is not logged as removed, and produces no error. It
  reads exactly like the sandbox suppressing it.

  ⚠️ **An earlier revision of this ADR claimed the sandbox structurally excluded these
  tools, and that was wrong.** The evidence for it was a sandbox-on/sandbox-off comparison
  where the sandbox-off run also had no sandbox allowlist, so the plugin providers loaded
  there and nowhere else. The variable was never isolated. The tell was visible and
  ignored: `session_status` is equally gateway-side and was available under the sandbox
  throughout. Recorded because the mistake, not the setting, is the reusable lesson —
  **a capability that is missing is not evidence of what removed it.**

- **`image_generate` and `video_generate` work too, and need `timeoutSeconds` raised.**
  Media generation is asynchronous: the turn records a detached task and returns, and a
  separate *completion run* delivers the result through the session's visible-reply mode.
  Generation takes ~90s, and at the default `agents.defaults.timeoutSeconds` the
  completion run is cut off — which trips an auth-profile cooldown, so the *next*,
  unrelated turns fail with "xai hasn't been responding". `600` settles it: image
  produced, completion `ended with stopReason=stop`, zero errors, on both the owner DM
  and the family group path.

  So the ADR's original consequence was right and this section's first draft was not.
  The capabilities do arrive with the credential; the two settings they need
  (`group:plugins`, and a run timeout that outlasts an async task) are both invisible in
  the failure mode, which is what cost the time.
- **NVB-16 becomes a `before_tool_call` plugin** returning `requireApproval`, routed by
  `approvals.plugin`. Plugin approvals are a separate family from exec approvals and can be
  delivered to a chosen person, which is what makes a per-tool "ask first" gate possible at
  all.
- **Cross-agent communication stays off.** `tools.agentToAgent.enabled` remains `false`.
  The investigation behind that deferral is recorded under Q6 so it is not re-litigated.

## The line to hold

The runtime is not a taste decision and it is not a billing decision. It is the choice of
which tools the kernel can contain. Any future runtime change is evaluated by one question
first: **does the sandbox reach the tools that actually execute?** If it does not, no amount
of tool policy above it substitutes.
