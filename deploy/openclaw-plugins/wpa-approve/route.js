// Does an approval raised for this agent have anywhere to go? Kept in its own file
// with no OpenClaw imports so it can be tested with plain `node` — see route.test.mjs.
//
// This is the fail-closed check, so it is the one piece of this plugin worth a test:
// wrong in one direction it hangs a turn for the full timeout, wrong in the other it
// lets a tool that must ask run without asking.

/**
 * Whether a plugin approval raised for this agent could actually reach a human.
 * Mirrors core's own forwarding rules: the block must be enabled, and `agentFilter`
 * — when present — must name the agent.
 */
function approvalRouteExists(api, agentId) {
  const cfg = api.runtime?.config?.current?.() ?? api.config;
  const plugin = cfg?.approvals?.plugin;
  if (!plugin?.enabled) return false;
  const filter = plugin.agentFilter;
  if (!Array.isArray(filter) || filter.length === 0) return true;
  return filter.includes(agentId);
}

export { approvalRouteExists };
