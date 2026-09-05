// node deploy/openclaw-plugins/wpa-approve/gated.test.mjs
//
// Guards the NVB-37 policy constants: wpa__deploy is gated, allow-always is not
// offered, and the description budget is what core actually clamps to.

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

// index.js imports openclaw's plugin SDK at top level. In CI / install.sh we may
// not have that package. Load the file as text and eval the pure exports we need
// by importing via a thin stub when the real SDK is absent.
let GATED, clamp, extractSummary, DESCRIPTION_MAX;

async function load() {
  try {
    const mod = await import(pathToFileURL(join(here, "index.js")).href);
    GATED = mod.GATED;
    clamp = mod.clamp;
    extractSummary = mod.extractSummary;
    DESCRIPTION_MAX = mod.DESCRIPTION_MAX;
    return;
  } catch (err) {
    // Fallback: exercise the same pure functions copied inline when SDK missing.
    // Keep behaviour locked to what index.js documents.
    if (!String(err).includes("openclaw") && !String(err.message || "").includes("Cannot find")) {
      throw err;
    }
  }

  // Minimal re-implementation matching index.js — only used when SDK is absent so
  // install.sh / CI without openclaw still asserts the policy constants via the
  // source file text below.
  const src = await import("node:fs").then((fs) =>
    fs.readFileSync(join(here, "index.js"), "utf8"),
  );
  assert.match(src, /wpa__deploy\s*:/);
  assert.match(src, /allowedDecisions:\s*\[\s*"allow-once"\s*,\s*"deny"\s*\]/);
  assert.doesNotMatch(
    src.replace(/\/\/.*$/gm, ""),
    /allowedDecisions:\s*\[[^\]]*allow-always/,
  );
  assert.match(src, /agents:\s*\[\s*"builder"\s*\]/);
  assert.match(src, /severity:\s*"critical"/);
  assert.match(src, /timeoutBehavior:\s*"deny"/);
  assert.match(src, /DESCRIPTION_MAX\s*=\s*512/);
  console.log("wpa-approve gated (source): all assertions passed");
  process.exit(0);
}

await load();

assert.ok(GATED.wpa__deploy, "wpa__deploy must be gated");
const rule = GATED.wpa__deploy;
assert.deepEqual(rule.allowedDecisions, ["allow-once", "deny"]);
assert.ok(!rule.allowedDecisions.includes("allow-always"));
assert.deepEqual(rule.agents, ["builder"]);
assert.equal(rule.severity, "critical");
assert.equal(typeof rule.describe, "function");

assert.equal(DESCRIPTION_MAX, 512);
assert.equal(clamp("abcd", 3), "...");
assert.equal(clamp("hi", 10), "hi");

const block = extractSummary(
  "sha=abc\n---summary---\nline one\nline two\n---end---\ntrailer\n",
);
assert.equal(block, "line one\nline two");

// Only deploy is gated today — a free tool must not appear here by accident.
assert.equal(Object.keys(GATED).length, 1);

console.log("wpa-approve gated: all assertions passed");
