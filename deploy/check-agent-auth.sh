#!/usr/bin/env bash
# Assert the credential-isolation invariant ADR 0011 depends on.
#
#   1. the DEFAULT agent's auth store is empty
#   2. every other configured agent has one of its own
#
# Rule 2 is not decoration. Auth profiles resolve read-through: an agent with no
# profile of its own falls back to the default agent's store. On 2026-08-15 the
# default agent was emptied by hand and REFILLED ITSELF within two minutes while one
# agent was depending on that fallback; with every agent holding its own profile it
# has stayed empty across restarts. So rule 2 is the thing that keeps rule 1 true,
# and checking rule 1 alone would report success right up until it mattered.
#
# The writer was never identified — see the CHANGELOG entry for 2026-08-15. This
# script therefore detects the state rather than preventing it, which is the honest
# shape for a fault whose cause is unknown.
#
# Run as root on the Pi:  sudo deploy/check-agent-auth.sh
# Exits non-zero on any violation, so it can gate a deploy.

set -euo pipefail

CONFIG=${OPENCLAW_CONFIG:-/var/lib/openclaw/.openclaw/openclaw.json}
AGENTS_DIR=${OPENCLAW_AGENTS_DIR:-/var/lib/openclaw/.openclaw/agents}

[ -r "$CONFIG" ] || { echo "cannot read $CONFIG (run with sudo)" >&2; exit 2; }

# ponytail: python3 for the JSON, not jq — jq is not installed on the Pi and this
# script is the wrong place to discover that.
default_agent=$(python3 -c '
import json, sys
c = json.load(open(sys.argv[1]))
ids = [a for a in c.get("agents", {}).get("list", []) if a.get("default")]
print(ids[0]["id"] if ids else "main")
' "$CONFIG")

all_agents=$(python3 -c '
import json, sys
c = json.load(open(sys.argv[1]))
print("\n".join(a["id"] for a in c.get("agents", {}).get("list", [])))
' "$CONFIG")

rows() {
	local db="$AGENTS_DIR/$1/agent/openclaw-agent.sqlite"
	# An agent that has never run has no store file at all. For the default agent
	# that is the ideal state; for anyone else it is the failure this catches.
	[ -e "$db" ] || { echo "-"; return; }
	sqlite3 "file:$db?mode=ro" "SELECT count(*) FROM auth_profile_store;" 2>/dev/null || echo "?"
}

fail=0
printf '%-12s %-8s %s\n' AGENT PROFILES VERDICT
while read -r id; do
	[ -n "$id" ] || continue
	n=$(rows "$id")
	if [ "$id" = "$default_agent" ]; then
		if [ "$n" = "0" ] || [ "$n" = "-" ]; then
			verdict="ok (default agent holds nothing)"
		else
			verdict="VIOLATION: default agent holds $n profile(s) — every agent without its own inherits them"
			fail=1
		fi
	else
		if [ "$n" = "0" ] || [ "$n" = "-" ]; then
			verdict="VIOLATION: no profile of its own, so it resolves through '$default_agent'"
			fail=1
		else
			verdict="ok"
		fi
	fi
	printf '%-12s %-8s %s\n' "$id" "$n" "$verdict"
done <<<"$all_agents"

if [ "$fail" -ne 0 ]; then
	cat >&2 <<-EOF

	Credential isolation is not holding.

	  - default agent non-empty: there is no supported way to disable read-through,
	    so emptying it is the only lever. Stop the gateway first; a live sqlite is
	    not a safe target. Then re-run this and watch it for a few minutes.
	  - an agent with no profile: give it one, and check where it landed —
	      openclaw models auth --agent <id> login --provider xai --method oauth
	      openclaw models --agent <id> status | grep effective=
	    Note the flag order: --agent belongs to 'models auth', before 'login'.
	EOF
	exit 1
fi

echo
echo "OK — '$default_agent' holds nothing and every other agent has its own profile."
