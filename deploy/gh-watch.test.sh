#!/usr/bin/env bash
# The one thing worth testing in gh-watch.sh: state advances only after the wake.
#
# ponytail: stubs on PATH rather than a mocking framework. curl and openclaw are
# called by name, and WPA_SYNC_BIN exists so the sync can be stubbed too.
#
#   bash deploy/gh-watch.test.sh
#
# Asserts a failed wake leaves the cursor, the seen list and the head record
# untouched so the next run replays it — the bug where an event could be marked
# reported and then never reported.

set -u

script=${1:-$(dirname "$0")/gh-watch.sh}
command -v jq >/dev/null || { echo "SKIP: no jq"; exit 0; }

fx=$(mktemp -d)
trap 'rm -rf "$fx"' EXIT
mkdir -p "$fx/state" "$fx/ws" "$fx/bin" "$fx/home/.openclaw"

# The script reads the GitHub token out of the gateway's own config, so HOME has to
# point somewhere that has one. Without this the run dies on a missing file and
# "committed nothing" passes for entirely the wrong reason — which it did once.
cat >"$fx/home/.openclaw/openclaw.json" <<'JSON'
{"mcp":{"servers":{"github":{"env":{"GITHUB_PERSONAL_ACCESS_TOKEN":"test-token"}}}}}
JSON

cat >"$fx/ws/repo-sync.json" <<'JSON'
{"repo":"o/r","prs":[{"number":7,"head":"abc123def456"}]}
JSON

# Nothing from the real API: the head-sha path alone produces the event.
cat >"$fx/bin/curl" <<'SH'
#!/usr/bin/env bash
for a in "$@"; do case "$a" in
  *actions/runs*) echo '{"workflow_runs":[]}'; exit 0 ;;
  *issues*)       echo '[]';                   exit 0 ;;
esac; done
echo '[]'
SH
printf '#!/usr/bin/env bash\nexit %s\n' 0 >"$fx/bin/wpa-project-sync"
chmod +x "$fx/bin/curl" "$fx/bin/wpa-project-sync"

echo 'somebodyelse' >"$fx/state/token-owner"

run() {  # $1 = exit code the openclaw stub should return
	printf '#!/usr/bin/env bash\nexit %s\n' "$1" >"$fx/bin/openclaw"
	chmod +x "$fx/bin/openclaw"
	PATH="$fx/bin:$PATH" HOME="$fx/home" \
	GH_REPO=o/r GH_SESSION_KEY=k GH_WORKSPACE="$fx/ws" \
	STATE_DIRECTORY="$fx/state" WPA_SYNC_BIN="$fx/bin/wpa-project-sync" \
		bash "$script" 2>&1
}

rc=0
state_is_clean() {
	[ ! -s "$fx/state/cursor" ] && [ ! -s "$fx/state/heads" ] && [ ! -s "$fx/state/seen" ]
}

# 1. The wake fails: nothing may be committed, or the event is lost forever.
out=$(run 1); code=$?
if [ "$code" != 0 ] && state_is_clean; then
	echo "ok   failed wake commits nothing"
else
	echo "FAIL failed wake left state behind (exit $code)"
	ls -la "$fx/state"; rc=1
fi

# 2. It must still be reported on the retry, not swallowed by the seen list.
out=$(run 0); code=$?
if [ "$code" = 0 ] && case "$out" in *"waking"*"PR #7"*) true ;; *) false ;; esac; then
	echo "ok   the same event is replayed once the wake succeeds"
else
	echo "FAIL event not replayed after a successful wake (exit $code): $out"; rc=1
fi

# 3. And having landed, it is not reported again.
out=$(run 0); code=$?
if [ "$code" = 0 ] && case "$out" in *"waking"*) false ;; *) true ;; esac; then
	echo "ok   a landed event is not repeated"
else
	echo "FAIL event repeated after it had already been delivered: $out"; rc=1
fi

# 4. A new head sha on the same PR is a new event.
printf '%s' '{"repo":"o/r","prs":[{"number":7,"head":"fff999888777"}]}' >"$fx/ws/repo-sync.json"
out=$(run 0); code=$?
if [ "$code" = 0 ] && case "$out" in *"waking"*"fff99988"*) true ;; *) false ;; esac; then
	echo "ok   a moved head sha reports again"
else
	echo "FAIL a moved head sha did not report: $out"; rc=1
fi

# 5. A tick that arrives while a previous wake is still running must skip, and must
#    commit nothing — otherwise the pile-up that triggers the embedded-agent
#    fallback also loses whatever this tick found.
if command -v flock >/dev/null; then
	rm -f "$fx/state/cursor" "$fx/state/heads" "$fx/state/seen"
	exec 9>"$fx/state/wake.lock"
	flock -n 9
	out=$(run 0); code=$?
	exec 9>&-
	if [ "$code" = 0 ] && case "$out" in *"skipping this tick"*) true ;; *) false ;; esac \
	   && state_is_clean; then
		echo "ok   a tick skips while a wake is in flight, committing nothing"
	else
		echo "FAIL concurrent tick did not skip cleanly (exit $code): $out"; rc=1
	fi
else
	echo "SKIP no flock"
fi

exit "$rc"
