#!/bin/sh
# Turn "a unit failed" into a message someone can act on from a phone.
#
#   wpa-triage <unit> <agent>
#
# Wired as ExecStart= in every *-failed.service. Reads the failed unit's journal,
# matches it against the signatures those scripts actually emit, and sends ONE
# message naming the cause and the exact command that fixes it.
#
# NVB-41. Before this, every alert ended in "Check: journalctl -u <unit> -n 50",
# which is the end of the help. The case that prompted it: a renamed repo returned
# 301, jq got the redirect body, and the journal said
#
#   jq: error (at <stdin>:5): Cannot index string with string "number"
#
# Getting from that to "edit GH_REPO in /etc/wpa-project.env" took a laptop and an
# SSH session. The fix was one sed. The diagnosis was already in the journal — it
# just never reached Signal.
#
# ⚠️ THE ONE RULE: RAW JOURNAL TEXT NEVER LEAVES THIS SCRIPT.
#
# Every message below is a fixed string written here. Two concrete reasons, not a
# principle: `push-opencode-auth.sh` deliberately does NOT discard opencode's
# stderr, so a credential can be sitting in that journal; and NVB-29 means the
# gateway writes outbound message text to journald. The journal is read only to
# CLASSIFY. Same discipline as src/wpa_mcp/push.py, and for the same reason.
#
# Three passes, in this order, because a specific cause beats a general one:
#
#   1. the failed unit's OWN vocabulary — check-agent-auth.sh's verdicts,
#      wpa-oc-auth's PATH and model errors, a failed wake in wpa-gh-watch
#   2. cross-cutting causes — 401, rate limit, DNS, a jq parse failure
#   3. the message that unit sent before this script existed
#
# Pass 3 is why this is safe to extend: an unrecognised failure is never given a
# worse message than it used to get, only an unimproved one.
#
# ponytail: a `case` over grep hits, not a rules engine. Upgrade path if it ever
# outgrows that: move the table to a file the script reads.
set -eu

unit=${1:?usage: wpa-triage <unit> <agent>}
agent=${2:?usage: wpa-triage <unit> <agent>}

CONFIG=${WPA_OPENCLAW_CONFIG:-/var/lib/openclaw/.openclaw/openclaw.json}

# -n 50 matches what the old messages told the owner to run, so the triage sees
# exactly what a human following the old advice would have seen. Failure here must
# not be fatal: an empty journal still gets the fallback message, which is strictly
# better than sending nothing because journalctl was unhappy.
#
# ⚠️ `-o cat` IS NOT COSMETIC. Default journalctl output prefixes every line with
#   Aug 26 11:03:58 raspberrypi wpa-agent-auth[1778490]: qualety  1  VIOLATION: …
# so a `^`-anchored pattern matches NOTHING. named_agents anchors on purpose (to
# read the verdict table's first column), and without this flag it silently found
# no agent and the message degraded to "run the check yourself" — the single most
# useful line, quietly missing. Not caught by the tests, whose stub emitted the
# script's raw output; caught on the Pi against a real violation. The stub now
# emits the prefixed form unless it is given `-o cat`, so removing this flag fails
# the suite.
log=$(journalctl -u "$unit" -n 50 --no-pager -o cat 2>/dev/null || true)

has() { printf '%s' "$log" | grep -qiE "$1"; }

# Which agents the check named, matched against the ids in the gateway config.
#
# This is the ONE place anything journal-derived reaches a message, and it stays
# inside the rule at the top of this file: nothing is copied out of the log. The
# log is used to SELECT from a vocabulary we already hold — an agent id from
# `agents.list[].id` — so the only strings that can ever be emitted are ones that
# were already in the config. An id is not a secret; which agent is broken is the
# whole difference between "go run the check" and "fix liron".
#
# ⚠️ THE `|| true` AND THE `if` ARE BOTH LOAD-BEARING, and this bit me. Under
# `set -e`, `who=$(named_agents)` takes the FUNCTION's exit status, and a `while`
# loop exits with the status of its last iteration. So whenever the last agent in
# the config was not one of the broken ones — `builder` on this box — the grep
# returned 1, the function returned 1, and the whole script died BEFORE sending
# anything. Silently: exit 1, no message, no trace. Every credential-isolation
# alert would have vanished. Caught by triage.test.sh only because its stub config
# happens to end with an agent that is not in the journal.
named_agents() {
	jq -r '.agents.list[]?.id // empty' "$CONFIG" 2>/dev/null | while read -r a; do
		[ -n "$a" ] || continue
		# check-agent-auth.sh names an agent in THREE shapes, and all of them matter:
		#   the verdict table   "liron   0   VIOLATION: inherits from 'main': …"
		#   the stray report    "  owner: google:navegerc@gmail.com"
		#   the unreadable list "Could not read the auth store of: liron qualety"
		# The third was missed at first, which left the ONE branch that actually
		# fires on this box as the only one that could not name its agent.
		# `if` rather than `&&` so a non-match is a condition, not a failed command.
		if printf '%s' "$log" | grep -qE "^$a[[:space:]]+[0-9]+[[:space:]]+(VIOLATION|warn)|^[[:space:]]+$a:[[:space:]]|Could not read the auth store of:.*[[:space:]]$a([[:space:]]|\$)"; then
			printf '%s ' "$a"
		fi
	done || true
}

# The per-unit fallback: what that alert said before NVB-41. Unmatched failures must
# not get a WORSE message than they used to.
case "$unit" in
wpa-gh-watch)
	what="wpa-gh-watch failed — the project room will not hear about new plans, PRs or merges until it is fixed."
	pat="the GitHub issues PAT (mcp.servers.github)"
	patfix="Mint a fine-grained PAT (Issues + Pull requests, this repo), then on the Pi:
  sudo openclaw config set mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN <token>
  sudo systemctl restart wpa-openclaw"
	;;
wpa-project-sync)
	what="wpa-project-sync failed — the qualety mirror is frozen. The agent will keep reading it and will not know it is stale."
	pat="the GitHub issues PAT (mcp.servers.github)"
	patfix="Mint a fine-grained PAT (Issues + Pull requests, this repo), then on the Pi:
  sudo openclaw config set mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN <token>
  sudo systemctl restart wpa-openclaw"
	;;
wpa-oc-auth)
	what="opencode token refresh failed. /oc runs on GitHub will start returning 401 once the published token expires (~6h)."
	pat="the opencode/xai credential"
	patfix="On the Pi: opencode auth login
Then: sudo systemctl start wpa-oc-auth"
	;;
wpa-agent-auth)
	# Deliberately NOT "credential isolation is not holding" any more. That was the
	# old fixed string, and it pre-judges: the most common real cause on this box is
	# an unreadable store, where asserting a leak in the first line contradicts the
	# diagnosis in the second. The header states what ran and failed; the cause
	# below says what it means.
	what="wpa-agent-auth failed — the credential-isolation invariant (NVB-32) did not verify."
	pat="an auth profile"
	patfix="Check which agent: sudo /opt/wpa/deploy/check-agent-auth.sh"
	;;
wpa-token-expiry)
	what="The token expiry check itself failed, so nothing is watching when the PATs run out."
	pat="a GitHub PAT"
	patfix="Check: journalctl -u wpa-token-expiry -n 50"
	;;
*)
	what="$unit failed."
	pat="a credential"
	patfix="Check: journalctl -u $unit -n 50"
	;;
esac

cause=""
fix=""

# ---------------------------------------------------------------------------
# 1. UNIT-SPECIFIC. Each of these scripts already writes a good diagnosis and the
#    exact remediation to stderr — check-agent-auth.sh even prints the openclaw
#    command with the flag order called out. All of that lands in the journal and
#    none of it used to reach Signal, which is the whole complaint NVB-41 opens
#    with. These rules recognise a unit's own vocabulary and restate its advice.
#
#    Runbook 05 §7 is a symptom/cause/fix table for wpa-oc-auth; the rows below
#    are that table, encoded. Keep the two in step.
# ---------------------------------------------------------------------------
case "$unit" in
wpa-agent-auth)
	# Fill the placeholders in when we can. A command you can paste is the entire
	# point of NVB-41 — "<id>" is still homework. Only substituted when exactly one
	# agent is named, because with two the reader has to choose anyway and a
	# half-filled command is worse than an honest template.
	who=$(named_agents | sed 's/ *$//')
	a_id="<id>"
	case "$who" in
	"") who_txt="Run the check to see which agent." ;;
	*" "*) who_txt="Affected: $who" ;;
	*) who_txt="Affected: $who"; a_id="$who" ;;
	esac

	# Same trick as the agent ids: the provider vocabulary is check-agent-auth.sh's
	# own MODEL_PROFILE_PREFIXES, not anything lifted out of the journal.
	a_prov="<p>"
	for p in ${MODEL_PROFILE_PREFIXES:-xai: anthropic: openai: google-vertex:}; do
		if printf '%s' "$log" | grep -qF "$p"; then
			a_prov="${p%:}"
			break
		fi
	done
	if has 'Could not read the auth store of'; then
		# This is the branch that actually fires on this box, so it gets the agent
		# name too — it was the only one of the three without it, which made the
		# most common alert the least useful one.
		cause="The check could not READ some auth stores — this is not a clean bill of health, it is no reading at all. Those agents were not checked; the verdict table says 'unreadable', which is neither ok nor a violation."
		fix="$who_txt

Usually the gateway is stopped: a read-only open of a WAL database still has to create its -shm sidecar, and that file is on disk only while some connection holds the database open.
  systemctl is-active wpa-openclaw.service
If it is running, the check was not run as root — the stores are 0700, uid 991.

An agent the gateway is merely idle on has no sidecar either, and is read through immutable=1 instead — skipped while a non-empty -wal says a write is in flight, which clears itself on the next run (NVB-49)."
	elif has 'A TOOL credential is in the auth profile store'; then
		cause="A TOOL credential is sitting in the auth profile store. ADR 0013 says it must not be — anything with a profile id gets mirrored into \`main\` on its next refresh and inherited by every agent that lacks it."
		fix="$who_txt
Move it to its own MCP server entry (one per principal, credential in that entry's env), then delete the profile:
  sudo -u openclaw HOME=/var/lib/openclaw openclaw models auth --agent $a_id logout --provider $a_prov
Tidying the store alone does NOT fix it — it comes back on the next refresh."
	elif has "VIOLATION: inherits from|Credential isolation is not holding"; then
		cause="An agent is resolving read-through to \`main\` for a profile it does not hold itself."
		fix="$who_txt
Give that agent its own profile — emptying \`main\` does NOT work, it refills on the next token refresh by design (NVB-32):
  sudo -u openclaw HOME=/var/lib/openclaw openclaw models auth --agent $a_id login --provider $a_prov --method oauth
  sudo -u openclaw HOME=/var/lib/openclaw openclaw models --agent $a_id status | grep effective=
Note the flag order: --agent belongs to 'models auth', BEFORE 'login'.
Full detail: sudo /opt/wpa/deploy/check-agent-auth.sh"
	fi
	;;

wpa-oc-auth)
	if has 'command not found'; then
		cause="opencode is not on systemd's PATH — the installer puts it under \$HOME and systemd's PATH is minimal."
		fix="The script prepends ~/.opencode/bin; check the install location has not moved:
  ls ~/.opencode/bin/opencode"
	elif has 'is a video model|not available on this endpoint'; then
		cause="OC_MODEL is unset or stale, so opencode picked its own default — which has been a *video* model. The error reads like an auth problem and is not."
		fix="  opencode models --all | grep -i grok
Then update OC_MODEL in /etc/wpa-oc.env"
	elif has 'network is unreachable'; then
		cause="Go preferred an AAAA record and this Pi has no global IPv6."
		fix="Handled by Environment=GODEBUG=netdns=cgo — check the unit still has it:
  systemctl cat wpa-oc-auth.service | grep GODEBUG"
	elif has 'refusing to publish'; then
		cause="The refresh produced a token that failed its own safety check, so nothing was published. This is the check working."
		fix="Read which rule tripped, then re-login:
  journalctl -u wpa-oc-auth -n 50
  opencode auth login"
	elif has 'token expires in under 1h|the refresh did not take'; then
		cause="The xai token refresh ran but did not actually refresh."
		fix="On the Pi: opencode auth login
Then: sudo systemctl start wpa-oc-auth"
	elif has 'no auth store at'; then
		cause="opencode has no local auth store — never logged in, or it was removed."
		fix="On the Pi: opencode auth login"
	fi
	;;

wpa-gh-watch)
	if has 'waking .*:' && has 'error|failed|timed out'; then
		cause="The event was detected but WAKING THE AGENT failed, so nothing was committed and the event will be replayed next tick."
		fix="Usually a stale session key — it must name a session that already exists:
  grep GH_SESSION_KEY /etc/wpa-project.env
  sudo -u openclaw HOME=/var/lib/openclaw openclaw sessions list --agent qualety --json
If the agent was renamed or its sessions cleared, the old key is gone; take one turn in the room, then copy the new key in."
	fi
	;;

wpa-project-sync)
	if has 'another sync holds the lock'; then
		cause="A previous tick was still running, so this one exited rather than racing it."
		fix="Benign once. If it repeats, the sync is taking longer than its interval:
  systemctl list-timers wpa-project-sync.timer"
	fi
	;;
esac

# ---------------------------------------------------------------------------
# 2. CROSS-CUTTING. Only consulted when the unit's own vocabulary matched nothing.
#    Ordering is deliberate where signatures overlap: a rate limit also mentions
#    403, and a dead token also fails to parse JSON, so the more specific cause
#    has to be tested first.
# ---------------------------------------------------------------------------
if [ -n "$cause" ]; then
	: # already diagnosed above
elif has '\b401\b|bad credentials'; then
	# Deliberately the per-unit fix, not a menu of every credential on the box:
	# a wpa-gh-watch 401 is the issues PAT, and offering the push PAT's path
	# beside it would send someone to replace the wrong credential at 2am.
	cause="$pat is being rejected (401) — expired or revoked."
	fix="$patfix

A gateway restart is REQUIRED after any token change — 'openclaw mcp reload' does not re-read a changed env."

elif has 'rate limit|x-ratelimit-remaining: 0'; then
	cause="GitHub rate-limited us (403). Not a credential problem."
	fix="No action — it clears on its own. If it repeats daily, the poll interval is too tight."

elif has 'no GitHub token in the gateway config'; then
	cause="The token read from the gateway config came back empty."
	fix="Check it is still there:
  sudo jq '.mcp.servers.github.env|keys' /var/lib/openclaw/.openclaw/openclaw.json
A config edit that dropped the key will do this."

elif has 'could not resolve host|curl: \(6\)|curl: \(7\)|network is unreachable'; then
	cause="The Pi could not reach the network — DNS or connectivity, not a credential."
	fix="Check: ping -c1 api.github.com
  systemctl status NetworkManager
  cat /etc/NetworkManager/conf.d/90-wpa-dns.conf"

elif has 'jq: error|cannot index'; then
	cause="An API response was not the shape we expected — usually a redirect body after a repo rename, which is exactly what happened on 2026-08-22."
	# Two plain commands rather than one clever pipeline: this gets pasted into a
	# phone terminal, where a nested \$( ) is a typo waiting to happen.
	fix="Check the repo slug still resolves:
  grep GH_REPO /etc/wpa-project.env
  curl -sI https://api.github.com/repos/OWNER/NAME
A 301 there means the repo was renamed — update GH_REPO to the new slug."

else
	cause=""
	fix="Check: journalctl -u $unit -n 50"
fi

if [ -n "$cause" ]; then
	text="⚠️ $what

$cause

$fix"
else
	text="⚠️ $what

$fix"
fi

exec /usr/local/bin/wpa-outbox-notify "$agent" "$text"
