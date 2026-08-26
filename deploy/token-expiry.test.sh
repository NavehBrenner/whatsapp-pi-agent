#!/usr/bin/env bash
# The three things worth testing in token-expiry.sh:
#
#   1. a crossing is announced ONCE, not every day for a fortnight
#   2. a dead token, a never-expiring token and an unreachable API are three
#      different outcomes — in particular, a network blip must stay SILENT
#   3. the token never reaches curl's argv
#
# (2) is the one that keeps this check trustworthy. wpa-gh-watch has been fixed
# twice by paging LESS; a daily timer that alerts whenever the uplink hiccups is
# how a real expiry ends up muted.
#
#   bash deploy/token-expiry.test.sh
#
# ponytail: stubs on PATH, same as gh-watch.test.sh.

set -u

script=${1:-$(dirname "$0")/token-expiry.sh}
command -v jq >/dev/null || { echo "SKIP: no jq"; exit 0; }

fx=$(mktemp -d)
trap 'rm -rf "$fx"' EXIT
mkdir -p "$fx/bin" "$fx/state"

TOKEN="ghp-test-DO-NOT-LEAK-1234567890"
printf '%s\n' "$TOKEN" > "$fx/push.token"

# curl stub: answers from HEADERS, and records its own argv so the test can prove
# the token was never passed on the command line.
cat >"$fx/bin/curl" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$ARGV_LOG"
printf '%s\n' "$HEADERS"
SH
chmod +x "$fx/bin/curl"

cat >"$fx/bin/wpa-outbox-notify" <<'SH'
#!/bin/sh
printf '%s\n---\n' "$2" >> "$OUT"
SH
chmod +x "$fx/bin/wpa-outbox-notify"

export PATH="$fx/bin:$PATH"
export OUT="$fx/sent" ARGV_LOG="$fx/argv"
export WPA_TOKEN_EXPIRY_STATE="$fx/state"
export WPA_NOTIFY_BIN="$fx/bin/wpa-outbox-notify"
export WPA_PUSH_TOKEN_FILE="$fx/push.token"
export WPA_OPENCLAW_CONFIG="$fx/nonexistent.json"   # only the push token in play

fails=0
run() { HEADERS=$1; export HEADERS; sh "$script" >/dev/null 2>&1 || true; }
reset() { rm -f "$OUT" "$ARGV_LOG"; rm -rf "$fx/state"; mkdir -p "$fx/state"; }
sent() { cat "$OUT" 2>/dev/null || echo ""; }
count() { grep -c -- '---' "$OUT" 2>/dev/null || echo 0; }

check() { # check <description> <actual> <expected>
	if [ "$2" = "$3" ]; then printf '  ok   %s\n' "$1"
	else printf '  FAIL %s — got %s want %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
has() { # has <description> <needle>
	if sent | grep -qF "$2"; then printf '  ok   %s\n' "$1"
	else printf '  FAIL %s — expected: %s\n' "$1" "$2"; fails=$((fails + 1)); fi
}

hdr() { printf 'HTTP/2 200\ngithub-authentication-token-expiration: %s\n' \
	"$(date -u -d "+$1 days" '+%Y-%m-%d %H:%M:%S UTC')"; }

echo "token-expiry:"

# --- comfortably in date: silence -------------------------------------------
reset; run "$(hdr 90)"
check "90 days out sends nothing" "$(count)" "0"

# --- crossing a threshold: exactly one message ------------------------------
reset; run "$(hdr 10)"
check "inside 14 days sends one message" "$(count)" "1"
has "and it names the days remaining" "expires in 10 day(s)"
has "and it gives the exact fix" "install -m600 /dev/stdin /etc/wpa-push.token"
has "and it says a restart is required" "systemctl restart wpa-openclaw"

# --- the same threshold again: still one -------------------------------------
run "$(hdr 10)"; run "$(hdr 9)"
check "the same threshold is not repeated" "$(count)" "1"

# --- a tighter threshold does speak up ---------------------------------------
run "$(hdr 5)"
check "crossing 7 days sends a second message" "$(count)" "2"

# --- a replaced token resets the state ---------------------------------------
run "$(hdr 90)"
check "a fresh token goes quiet again" "$(count)" "2"
run "$(hdr 5)"
check "and can alert again after replacement" "$(count)" "3"

# --- already dead -------------------------------------------------------------
reset; run 'HTTP/2 401
{"message":"Bad credentials"}'
check "a 401 sends one message" "$(count)" "1"
has "a 401 says it is already rejected" "ALREADY REJECTED"

# --- never expires -------------------------------------------------------------
reset; run 'HTTP/2 200'
check "a token with no expiry header reports once" "$(count)" "1"
has "and calls it a standing risk rather than a failure" "never expires"

# --- expired outright ----------------------------------------------------------
reset; run "$(hdr -3)"
check "an already-past date sends one message" "$(count)" "1"
has "and says it expired" "EXPIRED"

# --- unreachable: SILENT --------------------------------------------------------
reset
cat >"$fx/bin/curl" <<'SH'
#!/bin/sh
exit 6
SH
chmod +x "$fx/bin/curl"
run ""
check "an unreachable API sends NOTHING" "$(count)" "0"

# --- the token never reaches argv ------------------------------------------------
cat >"$fx/bin/curl" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$ARGV_LOG"
printf '%s\n' "$HEADERS"
SH
chmod +x "$fx/bin/curl"
reset; run "$(hdr 10)"
if grep -qF "$TOKEN" "$fx/argv" 2>/dev/null; then
	printf '  FAIL the token appeared in curl argv\n'; fails=$((fails + 1))
else
	printf '  ok   the token never reaches curl argv\n'
fi
if sent | grep -qF "$TOKEN"; then
	printf '  FAIL the token appeared in a message\n'; fails=$((fails + 1))
else
	printf '  ok   the token never reaches a message\n'
fi

echo
if [ "$fails" -eq 0 ]; then
	echo "token-expiry: all assertions passed"
else
	echo "token-expiry: $fails FAILED"; exit 1
fi
