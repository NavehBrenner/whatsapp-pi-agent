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
# Emits the raw text ONLY when asked for `-o cat`. Without that flag real
# journalctl prefixes each line with a timestamp, host and unit, which defeats
# every ^-anchored pattern in triage.sh — a production-only failure the first
# version of this stub hid completely.
cat >"$fx/bin/journalctl" <<'SH'
#!/bin/sh
for a in "$@"; do
  [ "$a" = "cat" ] && { printf '%s\n' "$JOURNAL"; exit 0; }
done
printf '%s\n' "$JOURNAL" | sed 's/^/Aug 26 12:00:00 pi unit[1]: /'
SH
chmod +x "$fx/bin/journalctl"

cat >"$fx/openclaw.json" <<'JSON'
{"agents":{"list":[{"id":"main"},{"id":"owner"},{"id":"liron"},{"id":"builder"}]}}
JSON

export PATH="$fx/bin:$PATH"
export OUT="$fx/sent"
export WPA_OPENCLAW_CONFIG="$fx/openclaw.json"

fails=0
run() { # run <unit> <journal>
	JOURNAL=$2 ; export JOURNAL
	rm -f "$OUT"
	sh "$fx/triage.sh" "$1" owner >/dev/null 2>&1 || true
	sent=$(cat "$OUT" 2>/dev/null || echo "")
}
ok() { # ok <description> <needle>
	if printf '%s' "$sent" | grep -qF -- "$2"; then
		printf '  ok   %s\n' "$1"
	else
		printf '  FAIL %s — expected to find: %s\n' "$1" "$2"; fails=$((fails + 1))
	fi
}
absent() { # absent <description> <needle>
	if printf '%s' "$sent" | grep -qF -- "$2"; then
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

# --- wpa-agent-auth: the complaint that prompted this pass ---------------------
# Before, all three of these produced the same sentence plus "Check: <script>".
run wpa-agent-auth 'AGENT            PROFILES  VERDICT
main             1         (default)
liron            0         VIOLATION: inherits from '"'"'main'"'"': xai:navegerc@gmail.com
Credential isolation is not holding.'
ok "read-through names the actual remedy" "login --provider"
ok "and warns about the flag order that catches everyone" "--agent belongs to 'models auth'"
ok "and names WHICH agent is broken" "Affected: liron"
absent "and does not just tell you to run the script" "Check: journalctl -u wpa-agent-auth"
# Regression: named_agents used to inherit the exit status of its last loop
# iteration, so whenever the config's LAST agent was healthy (builder, on the real
# box) `set -e` killed the script before it sent anything. Silently. The stub config
# above ends with "builder" precisely so this stays covered.
if [ -n "$sent" ]; then printf '  ok   %s\n' "a message is sent at all when the last configured agent is healthy"
else printf '  FAIL %s\n' "set -e regression: nothing was sent"; fails=$((fails + 1)); fi

run wpa-agent-auth 'A TOOL credential is in the auth profile store:
  owner: google:navegerc@gmail.com'
ok "a stray tool credential is a different diagnosis" "ADR 0013"
ok "with its own command" "logout --provider"
ok "and names that agent too" "Affected: owner"

run wpa-agent-auth 'Could not read the auth store of: liron
This is not a clean bill of health'
ok "an unreadable store points at the gateway, not at isolation" "systemctl is-active wpa-openclaw.service"
ok "an unreadable store names the agent too — the branch that actually fires here" "Affected: liron"
ok "and says those agents were not checked, rather than cleared" "not checked"
absent "and is not misreported as a violation" "login --provider"
absent "and the header does not assert a leak the body denies" "Credential isolation is not holding"

# An agent id that is NOT in the config must never be echoed: the vocabulary comes
# from agents.list, not from the journal.
run wpa-agent-auth 'ghost-agent      0         VIOLATION: inherits from '"'"'main'"'"'
Credential isolation is not holding.'
absent "an unknown agent id from the journal is never echoed" "ghost-agent"
ok "but the failure is still diagnosed" "login --provider"

# --- wpa-oc-auth: runbook 05 §7, encoded ---------------------------------------
run wpa-oc-auth 'opencode: command not found'
ok "PATH failure is not blamed on the token" "systemd's PATH is minimal"

run wpa-oc-auth 'model is a video model and is not available on this endpoint'
ok "the video-model trap names OC_MODEL" "OC_MODEL"
absent "and is explicitly not an auth problem" "auth login"

run wpa-oc-auth 'dial tcp [2606:50c0::]:443: connect: network is unreachable'
ok "the IPv6 trap names GODEBUG" "GODEBUG"

# --- wpa-gh-watch: a detected event whose wake failed --------------------------
run wpa-gh-watch 'waking agent:qualety:signal:group:abc: 1 PR
Error: request timed out'
ok "a failed wake is not read as a credential problem" "WAKING THE AGENT failed"
ok "and points at the session key" "GH_SESSION_KEY"

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
