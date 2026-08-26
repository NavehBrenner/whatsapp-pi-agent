#!/usr/bin/env bash
# The two things worth testing in triage.sh:
#
#   1. a known signature produces its own advice, and an unknown one still produces
#      the message the alert sent before NVB-41 existed
#   2. NOTHING from the journal reaches the outgoing message
#
# The second is the security property. The journal can hold a credential
# (push-opencode-auth.sh deliberately keeps opencode's stderr) and, for the
# gateway, message text (NVB-29). If test_journal_text_never_leaks fails, read the
# banner in triage.sh before "fixing" it.
#
#   bash deploy/triage.test.sh
#
# ponytail: stubs on PATH, same as gh-watch.test.sh. journalctl and
# wpa-outbox-notify are both called by absolute name or found on PATH, so a
# directory prepended to PATH is the whole mocking story.

set -u

script=${1:-$(dirname "$0")/triage.sh}

fx=$(mktemp -d)
trap 'rm -rf "$fx"' EXIT
mkdir -p "$fx/bin"

# The script execs the notifier by absolute path, so the stub has to live there
# too. Rather than write to /usr/local/bin in a test, run a copy with the path
# rewritten to somewhere writable — the alternative is a test that needs root.
sed "s|/usr/local/bin/wpa-outbox-notify|$fx/bin/wpa-outbox-notify|" "$script" >"$fx/triage.sh"
chmod +x "$fx/triage.sh"

cat >"$fx/bin/wpa-outbox-notify" <<'SH'
#!/bin/sh
# Record what would have been sent: agent on the first line, text after.
printf '%s\n%s\n' "$1" "$2" > "$OUT"
SH
chmod +x "$fx/bin/wpa-outbox-notify"

# journalctl is stubbed by content, set per-case in JOURNAL.
cat >"$fx/bin/journalctl" <<'SH'
#!/bin/sh
printf '%s\n' "$JOURNAL"
SH
chmod +x "$fx/bin/journalctl"

export PATH="$fx/bin:$PATH"
export OUT="$fx/sent"

fails=0
run() { # run <unit> <journal>
	JOURNAL=$2 ; export JOURNAL
	rm -f "$OUT"
	sh "$fx/triage.sh" "$1" owner >/dev/null 2>&1 || true
	sent=$(cat "$OUT" 2>/dev/null || echo "")
}
ok() { # ok <description> <needle>
	if printf '%s' "$sent" | grep -qF "$2"; then
		printf '  ok   %s\n' "$1"
	else
		printf '  FAIL %s — expected to find: %s\n' "$1" "$2"; fails=$((fails + 1))
	fi
}
absent() { # absent <description> <needle>
	if printf '%s' "$sent" | grep -qF "$2"; then
		printf '  FAIL %s — should NOT contain: %s\n' "$1" "$2"; fails=$((fails + 1))
	else
		printf '  ok   %s\n' "$1"
	fi
}

echo "triage:"

run wpa-gh-watch 'curl: (22) The requested URL returned error: 401'
ok "401 names the credential" "rejected (401)"
ok "401 says a restart is required" "systemctl restart wpa-openclaw"
absent "401 on gh-watch does not offer the push PAT path" "/etc/wpa-push.token"

run wpa-gh-watch 'HTTP 403 API rate limit exceeded for user'
ok "rate limit is not read as a credential failure" "rate-limited"
absent "rate limit does not tell you to mint a token" "Mint a replacement"

run wpa-project-sync 'jq: error (at <stdin>:5): Cannot index string with string "number"'
ok "the NVB-41 signature names a rename" "renamed"
ok "and points at the file that holds the slug" "GH_REPO"

run wpa-project-sync 'another sync holds the lock'
ok "lock contention is called benign" "Benign once"

run wpa-oc-auth 'no auth store at /home/x/.local/share/opencode/auth.json'
ok "a missing auth store says how to log in" "opencode auth login"

run wpa-gh-watch 'curl: (6) Could not resolve host: api.github.com'
ok "DNS failure is not blamed on the token" "DNS or connectivity"

run wpa-agent-auth 'agent liron is missing profile xai:navegerc@gmail.com'
ok "an unmatched agent-auth failure keeps its own text" "Credential isolation is not holding"

# --- the fallback, which must not be worse than what it replaced ---
run wpa-gh-watch 'something nobody has ever seen before'
ok "an unknown cause still names the unit" "wpa-gh-watch failed"
ok "an unknown cause still gives the journalctl line" "journalctl -u wpa-gh-watch -n 50"

# --- the security property ---
run wpa-oc-auth 'refreshing... Bearer sk-live-SUPERSECRET-DO-NOT-LEAK
no auth store at /nowhere'
absent "test_journal_text_never_leaks: no credential from the journal" "sk-live-SUPERSECRET-DO-NOT-LEAK"
absent "test_journal_text_never_leaks: no raw journal line at all" "refreshing..."
ok "but it still classified the failure" "opencode auth login"

run wpa-gh-watch 'sent to +972000000000: "hi, running late tonight"'
absent "message text from the journal never travels (NVB-29)" "running late tonight"

echo
if [ "$fails" -eq 0 ]; then
	echo "triage: all assertions passed"
else
	echo "triage: $fails FAILED"; exit 1
fi
