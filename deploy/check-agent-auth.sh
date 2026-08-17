#!/usr/bin/env bash
# Assert what is left of the credential-isolation invariant ADR 0011 depends on.
#
# The original pair of rules was:
#
#   1. the DEFAULT agent's auth store is empty
#   2. every other configured agent has one of its own
#
# **Rule 1 is dead.** It was closed on 2026-08-17 (NVB-32, Q7) by reading the code
# and then watching it happen: `main` refilled 36 seconds after a restart that had
# left it empty. OpenClaw mirrors every refreshed OAuth credential into the default
# agent's store on purpose — `mirrorRefreshedCredentialIntoMainStore`, called with
# `agentDir: void 0`, straight after a successful refresh. The refresh lock's own
# comment says why: N agents sharing one OAuth profile must not race on a
# single-use refresh token, so `main` is the rendezvous peers adopt from.
#
# So an empty `main` is not a state this system has. Asserting it produced a red
# light on every run, which is worse than no light at all.
#
# Two things are worth asserting instead.
#
#   1. every profile id is a MODEL credential (ADR 0013)
#
# The auth store is for model providers only, because that is the one credential
# class every agent legitimately shares. A tool credential belongs in its own MCP
# server entry, one per principal, where it has no profile id and so nothing can
# mirror it anywhere. A `google:` or `ms:` id appearing here means someone ran
# `models auth login` for a tool — catch it before its first refresh, not after.
#
#   2. for every profile id in the default agent's store, every other agent holds
#      that same profile id itself
#
# because an agent that lacks it resolves read-through to main's copy. Today main
# holds only the shared xai account, which every agent authenticates as anyway, so
# nothing crosses a boundary. Rule 1 is what keeps that true.
#
# The signature by hand: a moving `main` row beside FROZEN per-agent rows. Read
# updated_at from the row, never the file mtime — a WAL checkpoint moves the file
# without moving the row, and that cost a wrong conclusion once already.
#
# Runs on wpa-agent-auth.timer (boot + hourly). OnBootSec is load-bearing:
# read-through resolves at gateway startup.
#
# Run as root on the Pi:  sudo deploy/check-agent-auth.sh
# Exits non-zero on any violation, so it can gate a deploy.

set -euo pipefail

CONFIG=${OPENCLAW_CONFIG:-/var/lib/openclaw/.openclaw/openclaw.json}
AGENTS_DIR=${OPENCLAW_AGENTS_DIR:-/var/lib/openclaw/.openclaw/agents}

# Model providers whose credentials may live in the auth store (ADR 0013). A space
# -separated list of `<prefix>:` values; anything else in any store is a tool
# credential in the wrong place. Widen it when a model provider is added, never to
# quiet an alarm about a tool.
MODEL_PROFILE_PREFIXES=${MODEL_PROFILE_PREFIXES:-xai: anthropic: openai: google-vertex:}

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

# Profile ids held by an agent, one per line. An agent that has never run has no
# store file at all, which is simply no ids.
#
# A store that exists but cannot be READ is a different thing entirely, and it must
# never be reported as "holds nothing". Swallowing the error turned the whole check
# into a silent pass: no ids anywhere means no strays and nothing inherited, so it
# printed OK and exited 0 having read nothing at all. A monitor that cannot see is
# not a monitor that is happy.
#
# This was found while chasing a failure mode that turned out not to exist. The
# theory was that a WAL database cannot be opened read-only once its -shm sidecar
# is gone, so the OnBootSec run would race the gateway. Tested against copies with
# -shm and -wal removed AND the directory chmod a-w: SQLite reads them fine. The
# restriction only applies to a non-empty WAL needing replay; a cleanly checkpointed
# database opens read-only without shared memory. The guard stays anyway, because
# "reads nothing, reports OK" is wrong for any cause — permissions, corruption, a
# bad disk — and those do not need a theory to happen.
UNREADABLE='!unreadable'
ids() {
	local db="$AGENTS_DIR/$1/agent/openclaw-agent.sqlite"
	[ -e "$db" ] || return 0
	sqlite3 "file:$db?mode=ro" \
		"SELECT key FROM auth_profile_store, json_each(json_extract(store_json, '\$.profiles'));" \
		2>/dev/null || printf '%s\n' "$UNREADABLE"
}

unreadable=""
note_unreadable() {  # agent id, its ids
	case "$2" in *"$UNREADABLE"*) unreadable="$unreadable $1" ;; esac
}

# Profile ids on stdin that are NOT model-provider credentials (ADR 0013).
strays() {
	local pid prefix ok
	while read -r pid; do
		[ -n "$pid" ] || continue
		ok=0
		for prefix in $MODEL_PROFILE_PREFIXES; do
			case "$pid" in "$prefix"*) ok=1; break ;; esac
		done
		[ "$ok" = 1 ] || printf '%s\n' "$pid"
	done
}

main_ids=$(ids "$default_agent")
note_unreadable "$default_agent" "$main_ids"

fail=0
stray_report=""
printf '%-16s %-9s %s\n' AGENT PROFILES VERDICT

# The default agent first, for context. A non-empty main is never a fault on its own.
n=$(printf '%s' "$main_ids" | grep -c . || true)
printf '%-16s %-9s %s\n' "$default_agent" "$n" \
	"(default — mirrored credentials land here by design, NVB-32)"

for pid in $(printf '%s\n' "$main_ids" | strays); do
	stray_report="$stray_report  $default_agent: $pid
"
	fail=1
done

while read -r id; do
	[ -n "$id" ] || continue
	[ "$id" != "$default_agent" ] || continue

	agent_ids=$(ids "$id")
	note_unreadable "$id" "$agent_ids"
	n=$(printf '%s' "$agent_ids" | grep -c . || true)

	# Anything main holds that this agent does not, it inherits.
	inherited=""
	while read -r pid; do
		[ -n "$pid" ] || continue
		printf '%s\n' "$agent_ids" | grep -qxF "$pid" || inherited="$inherited $pid"
	done <<-EOF
		$main_ids
	EOF

	for pid in $(printf '%s\n' "$agent_ids" | strays); do
		stray_report="$stray_report  $id: $pid
"
		fail=1
	done

	if [ -n "$inherited" ]; then
		verdict="VIOLATION: inherits from '$default_agent':$inherited"
		fail=1
	elif [ "$n" = "0" ]; then
		# main holds nothing either, so nothing is inherited — but an agent with no
		# credential at all cannot answer, which is its own problem.
		verdict="warn: no profile of its own (nothing to inherit either)"
	else
		verdict="ok"
	fi
	printf '%-16s %-9s %s\n' "$id" "$n" "$verdict"
done <<<"$all_agents"

if [ -n "$unreadable" ]; then
	cat >&2 <<-EOF

	Could not read the auth store of:$unreadable

	This is not a clean bill of health — it is no reading at all. The usual cause is
	the gateway being stopped: SQLite will not open a WAL database read-only once the
	-shm sidecar is gone, and that file disappears when the last connection closes.

	    systemctl is-active wpa-openclaw.service

	Start the gateway and re-run. If it is already running, check that this is running
	as root — the stores are 0700 and owned by uid 991.
	EOF
	exit 2
fi

if [ -n "$stray_report" ]; then
	cat >&2 <<-EOF

	A TOOL credential is in the auth profile store. ADR 0013 says it must not be:

	$stray_report
	The auth store is for model providers only. Anything with a profile id gets
	mirrored into '$default_agent' on its next token refresh and inherited by every
	agent that lacks it — that is the leak ADR 0013 exists to avoid, and it is not
	fixable by tidying the store afterwards.

	Move it to its own MCP server entry, one per principal, credential in that
	entry's env, and grant only that agent its '<server>__*' tools. Then delete the
	profile:

	    openclaw models auth --agent <id> logout --provider <p>

	EOF
fi

if [ "$fail" -ne 0 ]; then
	cat >&2 <<-EOF
	Credential isolation is not holding.

	If an agent is resolving read-through to '$default_agent' for a profile it does
	not hold itself: emptying the default agent does NOT fix it — main refills on the
	next token refresh, by design (NVB-32). Give the agent its own profile instead:

	    openclaw models auth --agent <id> login --provider <p> --method oauth
	    openclaw models --agent <id> status | grep effective=

	Note the flag order: --agent belongs to 'models auth', before 'login'.

	If the inherited profile is one this agent must never have, it needs a separate
	account — profile ids are keyed on the account, not the agent, so two agents on
	one account cannot be separated by this store at all. See ADR 0013.
	EOF
	exit 1
fi

echo
echo "OK — auth store is model-only, and every agent holds each of '$default_agent's"
echo "     profile ids itself, so nothing inherits."
