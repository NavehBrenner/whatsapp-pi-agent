#!/usr/bin/env bash
# Assert the credential-isolation invariant ADR 0011 depends on.
#
#   1. the DEFAULT agent's auth store is empty
#   2. every other configured agent has one of its own
#
# Rule 2 is not decoration. Auth profiles resolve read-through: an agent with no
# profile of its own falls back to the default agent's store, so rule 2 is what
# bounds the damage when rule 1 is violated — which it will be.
#
# It was believed until 2026-08-16 that rule 2 also KEPT rule 1 true. It does not.
# `main` refilled itself during a twenty-hour window in which every agent held its
# own profile. Both rules are worth asserting; neither is a fix, and a green run is
# a statement about this moment only. Run it on a schedule, not after logins.
#
# The writer was identified on 2026-08-16 (NVB-32, Q7): an agent's own OAuth token
# refresh is persisted into the DEFAULT agent's store whenever main already holds an
# equivalent identity with a later-or-equal expiry. It is self-sustaining — main's
# copy is always the freshest, so the condition stays true — and it needs nobody to
# be inheriting. Emptying main is the lever, because the check that redirects the
# write fails immediately when main holds nothing.
#
# Knowing the mechanism still gives no switch to turn it off, so this script keeps
# detecting rather than preventing. What changed is that it now runs on
# wpa-agent-auth.timer (boot + hourly) instead of when someone remembers.
#
# The signature to look for by hand: a moving `main` row beside FROZEN per-agent
# rows. Read updated_at from the row, never the file mtime — a WAL checkpoint moves
# the file without moving the row, and that cost a wrong conclusion once already.
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
