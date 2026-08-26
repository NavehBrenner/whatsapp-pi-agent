#!/bin/sh
# Say a GitHub PAT is about to expire, while there is still time to do something.
#
#   wpa-token-expiry
#
# NVB-41. The push PAT (NVB-36) is reachable only from agent-invoked tools —
# wpa__push and ghpr__create_pull_request — so NO TIMER TOUCHES IT. When it expires
# nothing fails, no OnFailure= runs, and nobody is paged: `builder` simply starts
# reporting "push failed" into a room, and the reason sits in the gateway journal.
# This is the check that turns that into two weeks of warning.
#
# GitHub answers the question directly, which is what makes this ~40 lines instead
# of a calendar reminder:
#
#   $ curl -sI -H 'Authorization: Bearer …' https://api.github.com/
#   github-authentication-token-expiration: 2026-11-23 12:49:22 UTC
#
# ⚠️ A TOKEN IS NEVER PUT IN argv. Every curl reads its Authorization header from a
# 0600 config file built by streaming the token in, never by interpolating it into
# a command line — `ps` is world-readable and this box has eight agents on it. Same
# technique as src/wpa_mcp/push.py's credential helper, same reason.
#
# ponytail: no library, no date maths beyond `date -d`. The state file holds the
# last threshold announced per token, so a crossing is reported once rather than
# every day for a fortnight.
set -eu

STATE=${WPA_TOKEN_EXPIRY_STATE:-/var/lib/wpa-token-expiry}
NOTIFY=${WPA_NOTIFY_BIN:-/usr/local/bin/wpa-outbox-notify}
CONFIG=${WPA_OPENCLAW_CONFIG:-/var/lib/openclaw/.openclaw/openclaw.json}
PUSH_TOKEN_FILE=${WPA_PUSH_TOKEN_FILE:-/etc/wpa-push.token}
API=${WPA_GITHUB_API:-https://api.github.com/}

# Each is announced once. 14 is far enough out to mint a token without hurrying; 1
# is the "you are about to lose the push path" shout.
#
# ⚠️ ASCENDING, AND THAT IS LOAD-BEARING. The loop takes the FIRST threshold the
# token is inside, which has to be the smallest one — at 5 days left, the message
# to send is the 7-day one, and matching 14 first would find a threshold already
# announced and then say nothing at all. Written descending initially and caught by
# test "crossing 7 days sends a second message".
THRESHOLDS="1 7 14"

mkdir -p "$STATE"
chmod 700 "$STATE"

notify() { "$NOTIFY" owner "$1"; }

# Announce `msg` for `name` at threshold `t`, unless that threshold already went
# out. Recording the threshold rather than a timestamp means a token replaced with
# a fresh one resets naturally: 90 days out matches no threshold, and the file is
# cleared the next time it does.
announce_once() { # announce_once <name> <threshold> <message>
	f="$STATE/$1.last"
	prev=$(cat "$f" 2>/dev/null || echo "")
	[ "$prev" = "$2" ] && return 0
	printf '%s' "$2" > "$f"
	notify "$3"
}

clear_state() { rm -f "$STATE/$1.last"; }

# Reads a token on stdin. Prints the expiry header's value, "none" when the token
# is valid but never expires, or "unauthorized" when GitHub rejects it.
probe() {
	cfg=$(mktemp); chmod 600 "$cfg"
	{ printf 'header = "Authorization: Bearer '; tr -d '\n'; printf '"\n'; } > "$cfg"
	# --max-time so a hung API cannot wedge a daily timer. A transport failure is
	# NOT reported as a bad token: this check exists to name causes precisely, and
	# "your network was down" must never read as "your credential died".
	head=$(curl -sS -I --max-time 30 -K "$cfg" "$API" 2>/dev/null || echo "CURLFAIL")
	rm -f "$cfg"
	case "$head" in CURLFAIL | "") echo "unreachable"; return 0 ;; esac
	printf '%s' "$head" | grep -qiE '^HTTP/[0-9.]+ 401' && { echo "unauthorized"; return 0; }
	exp=$(printf '%s' "$head" | sed -n 's/^[Gg]ithub-[Aa]uthentication-[Tt]oken-[Ee]xpiration: *//p' | tr -d '\r')
	[ -n "$exp" ] && echo "$exp" || echo "none"
}

check() { # check <name> <human description> <fix text>  — token on stdin
	name=$1 desc=$2 fix=$3
	result=$(probe)

	case "$result" in
	unreachable)
		# Silent on purpose. A daily timer that pages every time the uplink
		# blips is the alarm that gets muted, and then the real expiry is
		# silent too — the mistake wpa-gh-watch already made twice.
		echo "$name: could not reach GitHub, skipping" >&2
		return 0
		;;
	unauthorized)
		announce_once "$name" dead "⚠️ $desc is ALREADY REJECTED by GitHub (401) — expired or revoked.

$fix"
		return 0
		;;
	none)
		announce_once "$name" never "ℹ️ $desc never expires. That is not a failure, but a non-expiring token is a standing risk — consider replacing it with a fine-grained token that has an expiry date.

$fix"
		return 0
		;;
	esac

	# "2026-11-23 12:49:22 UTC" — `date -d` parses it as-is.
	secs=$(date -d "$result" +%s 2>/dev/null || echo "")
	[ -n "$secs" ] || { echo "$name: unparseable expiry '$result'" >&2; return 0; }
	days=$(( (secs - $(date +%s)) / 86400 ))

	if [ "$days" -lt 0 ]; then
		announce_once "$name" dead "⚠️ $desc EXPIRED on $result.

$fix"
		return 0
	fi

	for t in $THRESHOLDS; do
		if [ "$days" -le "$t" ]; then
			announce_once "$name" "$t" "⚠️ $desc expires in $days day(s) — $result. Nothing has broken yet.

$fix"
			return 0
		fi
	done

	# Comfortably in date: forget any threshold already announced, so a replaced
	# token starts from a clean slate rather than staying silent at its old level.
	clear_state "$name"
	echo "$name: $days days left" >&2
}

push_fix="Mint a fine-grained PAT — Contents: Read and write, Pull requests: Read and write, this repo only — then:
  ssh pi \"sudo install -m600 /dev/stdin /etc/wpa-push.token\"
  (paste the token, then Ctrl-D)
  sudo openclaw config set mcp.servers.wpa.env.WPA_PUSH_TOKEN <token>
  sudo openclaw config set mcp.servers.ghpr.env.GITHUB_PERSONAL_ACCESS_TOKEN <token>
  sudo systemctl restart wpa-openclaw
The restart is REQUIRED — 'openclaw mcp reload' does not re-read a changed env."

issues_fix="Mint a fine-grained PAT — Issues and Pull requests, this repo only — then:
  sudo openclaw config set mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN <token>
  sudo systemctl restart wpa-openclaw
The restart is REQUIRED — 'openclaw mcp reload' does not re-read a changed env."

# --- the push PAT: builder's path to GitHub, watched by nothing else ---------
if [ -r "$PUSH_TOKEN_FILE" ] && [ -s "$PUSH_TOKEN_FILE" ]; then
	check push "The push PAT (wpa__push, ghpr__create_pull_request)" "$push_fix" \
		< "$PUSH_TOKEN_FILE"
else
	echo "push: no token at $PUSH_TOKEN_FILE, skipping" >&2
fi

# --- the issues PAT: read from the one place that already holds it -----------
# Same read as deploy/gh-watch.sh, so there is still exactly one home for this
# value and no second copy to drift.
if [ -r "$CONFIG" ]; then
	jq -r '.mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN // empty' "$CONFIG" \
		> "$STATE/.tok" 2>/dev/null || true
	chmod 600 "$STATE/.tok" 2>/dev/null || true
	if [ -s "$STATE/.tok" ]; then
		check issues "The GitHub issues PAT (mcp.servers.github)" "$issues_fix" \
			< "$STATE/.tok"
	else
		echo "issues: no token in $CONFIG, skipping" >&2
	fi
	rm -f "$STATE/.tok"
else
	echo "issues: cannot read $CONFIG, skipping" >&2
fi
