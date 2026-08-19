// Ask-first: a `before_tool_call` gate that stops a named tool until a human says yes.
//
// NVB-16. OpenClaw already ships the per-call approval mechanism — the prompt, the
// `/approve <id>` command, the Signal reaction handling, the expiry and the
// fail-closed default. This plugin is the policy on top of it: which tools ask, what
// the prompt says, and which answers are offered.
//
// WHAT WAS READ OUT OF THE SHIPPED dist RATHER THAN THE DOCS (2026-08-19, 2026.7.1-2):
//
//   - `title` is capped at 80 characters and `description` at 512. Longer values are
//     the caller's problem, so everything here is clamped on the way out.
//   - Timeout defaults to 120s and is CLAMPED to 600s. Ten minutes is the longest a
//     prompt can ever wait, whatever we pass.
//   - The default decision set INCLUDES `allow-always`. Omitting `allowedDecisions`
//     therefore offers a standing grant, which is the opposite of what an ask-first
//     gate is for. Every entry below names its decisions explicitly.
//   - Reaction bindings are fixed in core: 👍 allow-once, ♾️ allow-always, 👎 deny.
//     They are not configurable, and the emoji offered follow `allowedDecisions` —
//     so withholding `allow-always` also withholds ♾️.
//
// THE PROMPT'S LAYOUT IS NOT OURS AND CANNOT BE MADE OURS. Traced 2026-08-19: an
// approval prompt is delivered by the approval FORWARDER —
// `handlePluginApprovalRequested` -> `deliverToTargets` -> `sendDurableMessageBatch`
// — and that path never emits `message_sending`. That hook is emitted only by the
// reply/dispatch pipeline, so a `message_sending` rewriter compiles, loads, and
// never runs. A version of this plugin shipped exactly that and looked fine.
//
// The payload itself comes from the CHANNEL's approval renderer
// (`resolveChannelApprovalAdapter`), with a core fallback. So `Tool:`, `Plugin:`,
// `Agent:`, `ID:`, `Expires in:`, `Reply with:`, `React with:` and the
// "Allow Always is unavailable" footnote belong to Signal and core, and no plugin
// hook reaches them.
//
// WHAT WE ACTUALLY CONTROL is the four fields below: `title`, `description`,
// `severity` (the badge) and `allowedDecisions` (which emoji are offered). Anything
// we want a person to read has to live in those.
//
// THE PROMPT IS NOT A SUMMARY OF THE AGENT'S INTENT. `params` is model-controlled, so
// anything derived from it describes what was *asked for*, not what is *true*. For a
// tool where that distinction matters — NVB-37's deploy — the description is rendered
// by host code from artifacts on disk, and this hook passes it through untouched.

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { approvalRouteExists } from "./route.js";

/** Trim to a limit core would otherwise trim for us, and say that we did. */
function clamp(text, max) {
  const value = String(text ?? "");
  return value.length <= max ? value : `${value.slice(0, max - 3)}...`;
}

const TITLE_MAX = 80;
const DESCRIPTION_MAX = 512;

// The longest wait core will honour. Passing more is silently clamped, so say it here.
const MAX_TIMEOUT_MS = 600_000;

// WHY A CONSTANT AND NOT CONFIG. The set of tools that must ask is a security
// boundary, and `openclaw config set` can rewrite config. A change here is a diff in
// a pull request; a change in config is a chat message away from being a capability
// grant, which ADR 0010 forbids.
// Nothing is gated yet. The mechanism was proven on 2026-08-19 against `web_search`
// as a throwaway probe (NVB-16), and that entry was removed once it had done its
// job: an approval prompt on every web search is a tax nobody agreed to pay, and a
// gate that cries wolf is a gate people learn to tap through.
//
// `wpa__deploy` is the first real entry, and it arrives with NVB-37. The shape:
//
//   wpa__deploy: {
//     title: "Deploy to the Pi",
//     severity: "critical",
//     warning: "Installs merged code and your config change on the Pi, as root.",
//     agents: ["builder"],
//     allowedDecisions: ["allow-once", "deny"],   // never allow-always
//     timeoutMs: 120_000,
//     describe: () => renderedByHostCodeFromDiskNotFromParams(),
//   }
const GATED = {};


export default definePluginEntry({
  id: "wpa-approve",
  name: "WPA Ask-First",
  description: "Requires a human approval in Signal before a named tool runs.",
  register(api) {
    api.on("before_tool_call", (event, ctx) => {
      const rule = GATED[event.toolName];
      // Returning nothing lets the call through, so gated and free tools coexist
      // under one hook and the decision is per call.
      if (!rule) return;
      if (rule.agents && !rule.agents.includes(ctx.agentId)) return;

      // A GATE WITH NO ROUTE IS NOT A GATE, IT IS A TEN-MINUTE HANG. Measured
      // 2026-08-19: an approval nobody can answer does not fail fast — the turn
      // waits out the whole timeout, the channel shows a typing indicator, and the
      // health monitor logs `stalled_agent_run` every 30s until it expires.
      //
      // So check the route ourselves and refuse immediately instead. This fails
      // CLOSED: a tool that must ask, in a session where nobody can be asked, is
      // refused rather than quietly allowed.
      if (!approvalRouteExists(api, ctx.agentId)) {
        api.logger.warn?.(
          `wpa-approve: blocking ${event.toolName} for agent=${ctx.agentId ?? "?"} — ` +
            "no approvals.plugin route reaches it",
        );
        return {
          block: true,
          blockReason:
            "This action needs a person's approval, and no approval route is " +
            "configured for this agent. Refused rather than run unapproved.",
        };
      }

      api.logger.info?.(
        `wpa-approve: requiring approval for ${event.toolName} agent=${ctx.agentId ?? "?"}`,
      );

      return {
        requireApproval: {
          title: clamp(rule.title, TITLE_MAX),
          // The warning leads, because it is what the decision turns on. It eats
          // the same 512-character budget as the detail, so a rule with a long
          // warning gets a short description rather than a truncated warning.
          description: clamp(
            [rule.warning ? `⚠️ ${rule.warning}` : "", rule.describe(event, ctx)]
              .filter(Boolean)
              .join("\n\n"),
            DESCRIPTION_MAX,
          ),
          severity: rule.severity,
          // Explicit rather than inherited: the default set includes allow-always.
          allowedDecisions: rule.allowedDecisions,
          timeoutMs: Math.min(rule.timeoutMs, MAX_TIMEOUT_MS),
          // Core's default too, and stated because it is the property that matters:
          // an unanswered prompt must refuse, never proceed.
          timeoutBehavior: "deny",
          pluginId: "wpa-approve",
          // The argument is a bare STRING, not a resolution object:
          // "allow-once" | "allow-always" | "deny" | "timeout" | "cancelled".
          // Reading `.decision` off it yields undefined, which an earlier version
          // of this file defaulted to "timed-out" — so every approval AND every
          // denial was logged as a timeout. For a privileged tool that log line is
          // the audit trail, so a wrong default there is worse than no line.
          onResolution: (decision) => {
            api.logger.info?.(`wpa-approve: ${event.toolName} ${decision}`);
          },
        },
      };
    });
  },
});
