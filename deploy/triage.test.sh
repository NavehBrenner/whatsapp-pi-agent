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
# The repeat suppressor keys on the composed message, so every distinct case below
# is a distinct key and passes through untouched. Pointed at the fixture dir
# because the default is /var/lib and this suite does not run as root — without
# this the suppressor would fail open and never be exercised at all.
export STATE_DIRECTORY="$fx/state"

fails=0
# Each case starts with an empty suppressor. Cases below deliberately reuse
# messages — several signatures share one fixed remediation string — and without
# this reset the FIRST case to produce a given message would be the only one that
# ever sends, failing every later case for a reason that has nothing to do with
# what it is testing. The suppression block near the end manages its own state.
run() { # run <unit> <journal>
	JOURNAL=$2 ; export JOURNAL
	rm -f "$OUT"
	rm -rf "$STATE_DIRECTORY"
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

# --- the model provider is out of credits (2026-08-27) --------------------------
run wpa-gh-watch 'waking agent:qualety:signal:group:abc: new comment on issue #50
GatewayClientRequestError: FailoverError: 403 "You have run out of credits or need a Grok subscription."'
ok "a provider refusal names the provider, not the watcher" "MODEL PROVIDER refused"
ok "and says no restart is needed" "no restart is needed"
ok "and reassures that events are replayed" "replayed"
absent "and is NOT misread as a stale session key" "GH_SESSION_KEY"
absent "and is NOT misread as a GitHub rate limit" "rate-limited"
absent "and does not tell you to mint a PAT" "Mint a fine-grained PAT"

# The reason the rule matches `FailoverError` and not the human phrase: gh-watch
# logs the GitHub comment bodies it found, and on 2026-08-27 the opencode bot had
# posted its own quota error into an issue. That text in the journal says nothing
# about whether THIS box failed, so it must not classify.
run wpa-gh-watch 'waking agent:qualety:signal:group:abc: new comment by bot on issue #50: APIError: spending-limit: You have run out of credits
Error: request timed out'
absent "a quota phrase quoted from a GitHub comment does not fake a provider failure" "MODEL PROVIDER refused"
ok "and the real cause is still diagnosed" "WAKING THE AGENT failed"

# --- repeat suppression ---------------------------------------------------------
# The 2026-08-27 case: one cause, 27 identical alerts, because the unit retried
# every 60s. The first must send and the second must not.
JOURNAL='GatewayClientRequestError: FailoverError: 403 out of credits' ; export JOURNAL
rm -rf "$STATE_DIRECTORY"
rm -f "$OUT"
sh "$fx/triage.sh" wpa-gh-watch owner >/dev/null 2>&1 || true
first=$(cat "$OUT" 2>/dev/null || echo "")
rm -f "$OUT"
sh "$fx/triage.sh" wpa-gh-watch owner >/dev/null 2>&1 || true
second=$(cat "$OUT" 2>/dev/null || echo "")
if [ -n "$first" ]; then printf '  ok   %s\n' "the first alert for a cause is sent"
else printf '  FAIL %s\n' "the first alert for a cause is sent"; fails=$((fails + 1)); fi
if [ -z "$second" ]; then printf '  ok   %s\n' "an identical repeat within the window is suppressed"
else printf '  FAIL %s\n' "an identical repeat within the window is suppressed"; fails=$((fails + 1)); fi

# A DIFFERENT failure of the same unit must still get through — suppression is
# keyed on the cause, not on the unit, or the second outage of the day is silent.
run wpa-gh-watch 'curl: (6) Could not resolve host: api.github.com'
ok "a different cause on the same unit is not suppressed" "DNS or connectivity"

# And the window is honoured rather than being a permanent mute.
# Deliberately NOT via run(), which resets the suppressor: the whole assertion is
# that a stamp already on disk stops mattering once the window has elapsed. Send
# once, then send the identical thing again with the window at zero.
JOURNAL='curl: (6) Could not resolve host: api.github.com' ; export JOURNAL
rm -rf "$STATE_DIRECTORY"
rm -f "$OUT"
sh "$fx/triage.sh" wpa-gh-watch owner >/dev/null 2>&1 || true
rm -f "$OUT"
WPA_TRIAGE_REPEAT_SECONDS=0 ; export WPA_TRIAGE_REPEAT_SECONDS
sh "$fx/triage.sh" wpa-gh-watch owner >/dev/null 2>&1 || true
sent=$(cat "$OUT" 2>/dev/null || echo "")
ok "the same cause sends again once the window has passed" "DNS or connectivity"
unset WPA_TRIAGE_REPEAT_SECONDS

# Suppression must FAIL OPEN: if the state dir cannot be created, still alert.
STATE_DIRECTORY=/proc/nonexistent/state ; export STATE_DIRECTORY
run wpa-gh-watch 'curl: (22) The requested URL returned error: 401'
ok "an unwritable state dir still sends the alert" "rejected (401)"
STATE_DIRECTORY="$fx/state" ; export STATE_DIRECTORY

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
