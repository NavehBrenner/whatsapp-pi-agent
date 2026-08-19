// node deploy/openclaw-plugins/wpa-approve/route.test.mjs
//
// Guards the fail-closed rule. Measured on hardware 2026-08-19: an approval nobody
// can answer does NOT fail fast — the turn waits out the whole timeout (up to 600s),
// the channel shows a typing indicator throughout, and the health monitor logs
// `stalled_agent_run` every 30s. So "is there a route?" has to be answered before
// asking, and answered conservatively.

import assert from "node:assert/strict";

import { approvalRouteExists } from "./route.js";

const api = (approvals) => ({ config: approvals ? { approvals } : {} });

// No approvals block at all — the shipped default. Nothing can be asked, so nothing
// that must ask may run.
assert.equal(approvalRouteExists(api(undefined), "owner"), false);
assert.equal(approvalRouteExists({ config: undefined }, "owner"), false);
assert.equal(approvalRouteExists({}, "owner"), false);

// Present but switched off is still off.
assert.equal(approvalRouteExists(api({ plugin: { enabled: false } }), "owner"), false);
assert.equal(approvalRouteExists(api({ exec: { enabled: true } }), "owner"), false);

// Enabled with no filter forwards for every agent — core's own rule.
assert.equal(approvalRouteExists(api({ plugin: { enabled: true } }), "owner"), true);
assert.equal(approvalRouteExists(api({ plugin: { enabled: true, agentFilter: [] } }), "x"), true);

// Enabled with a filter forwards only for the agents it names. This is the case that
// bit us: `web_search` is in the global tools.alsoAllow so every agent holds it,
// while agentFilter named only `owner`.
const filtered = api({ plugin: { enabled: true, agentFilter: ["owner", "builder"] } });
assert.equal(approvalRouteExists(filtered, "owner"), true);
assert.equal(approvalRouteExists(filtered, "builder"), true);
assert.equal(approvalRouteExists(filtered, "family"), false);
assert.equal(approvalRouteExists(filtered, "liron"), false);

// An agent we cannot identify is an agent we cannot route to.
assert.equal(approvalRouteExists(filtered, undefined), false);

// A non-array `agentFilter` is treated as no filter — i.e. permissive, for EVERY
// agent, not just the one the string names. Asserted in both directions so the
// behaviour is stated rather than implied by a passing test.
//
// This is deliberate rather than fail-closed: `agentFilter` is `array of string` in
// the schema, so a string fails `openclaw config validate` before it can reach the
// gateway, and diverging from core's forwarding rule here would block calls core
// would happily route.
const malformed = api({ plugin: { enabled: true, agentFilter: "owner" } });
assert.equal(approvalRouteExists(malformed, "owner"), true);
assert.equal(approvalRouteExists(malformed, "family"), true);

// The live config is read per call, not captured at registration: `openclaw.json` is
// watched and edits apply without a restart, so a stale snapshot would keep asking
// on a route that has been removed.
let current = { approvals: { plugin: { enabled: true, agentFilter: ["owner"] } } };
const live = { runtime: { config: { current: () => current } }, config: {} };
assert.equal(approvalRouteExists(live, "owner"), true);
current = { approvals: { plugin: { enabled: false } } };
assert.equal(approvalRouteExists(live, "owner"), false);

console.log("wpa-approve route: all assertions passed");
