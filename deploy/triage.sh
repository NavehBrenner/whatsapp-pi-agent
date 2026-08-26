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
# ponytail: a `case` over grep hits, not a rules engine. Nine signatures, first
# match wins, and the fallback is what the alert said before this existed. Upgrade
# path if it ever outgrows that: move the table to a file the script reads.
set -eu

unit=${1:?usage: wpa-triage <unit> <agent>}
agent=${2:?usage: wpa-triage <unit> <agent>}

# -n 50 matches what the old messages told the owner to run, so the triage sees
# exactly what a human following the old advice would have seen. Failure here must
# not be fatal: an empty journal still gets the fallback message, which is strictly
# better than sending nothing because journalctl was unhappy.
log=$(journalctl -u "$unit" -n 50 --no-pager 2>/dev/null || true)

has() { printf '%s' "$log" | grep -qiE "$1"; }

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
	what="Credential isolation is not holding (NVB-32). An agent is missing a profile that \`main\` holds, so it inherits main's copy. Emptying \`main\` will NOT fix it — it is re-mirrored on the next token refresh, by design."
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

# Ordered, first match wins. Ordering is deliberate where signatures overlap: a rate
# limit also mentions 403, and a dead token also fails to parse JSON, so the more
# specific cause has to be tested first.
if has '\b401\b|bad credentials'; then
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

elif has 'another sync holds the lock'; then
	cause="A previous tick was still running, so this one exited rather than racing it."
	fix="Benign once. If it repeats, the sync is taking longer than its interval:
  systemctl list-timers wpa-project-sync.timer"

elif has 'no auth store at'; then
	cause="opencode has no local auth store — it was never logged in, or the store was removed."
	fix="On the Pi: opencode auth login"

elif has 'token expires in under 1h|the refresh did not take'; then
	cause="The xai token refresh ran but did not actually refresh."
	fix="On the Pi: opencode auth login
Then: sudo systemctl start wpa-oc-auth"

elif has 'refusing to publish'; then
	cause="The refresh produced a token that failed its own safety check, so nothing was published. This is the check working."
	fix="Read the reason, then re-login:
  journalctl -u wpa-oc-auth -n 50
  opencode auth login"

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
