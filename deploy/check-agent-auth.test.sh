#!/usr/bin/env bash
# The alarm test for check-agent-auth.sh, on a fixture rather than the live box.
#
# ponytail: a shell script beside the thing it tests, not a pytest — it needs the
# sqlite3 CLI, which exists on the Pi and not in CI, and a test that only ever
# skips is theatre. Run it on the Pi after changing the checker:
#
#   bash deploy/check-agent-auth.test.sh
#
# Asserts both directions: a green tree exits 0, and an agent missing one of the
# default agent's profile ids exits 1 and is named.

set -u

script=${1:-$(dirname "$0")/check-agent-auth.sh}
command -v sqlite3 >/dev/null || { echo "SKIP: no sqlite3 (run this on the Pi)"; exit 0; }

fx=$(mktemp -d)
trap 'rm -rf "$fx"' EXIT
mkdir -p "$fx/agents/main/agent" "$fx/agents/bob/agent"
printf '%s' '{"agents":{"list":[{"id":"main","default":true},{"id":"bob"}]}}' >"$fx/openclaw.json"

seed() {
	sqlite3 "$fx/agents/$1/agent/openclaw-agent.sqlite" \
		"CREATE TABLE auth_profile_store(store_key TEXT, store_json TEXT, updated_at INT);
		 INSERT INTO auth_profile_store VALUES('primary','{\"profiles\":{$2}}',1);"
}

run() { OPENCLAW_CONFIG="$fx/openclaw.json" OPENCLAW_AGENTS_DIR="$fx/agents" bash "$script"; }

rc=0

# 1. bob holds everything main does — nothing inherits.
seed main '"xai:a@b.com":{}'
seed bob '"xai:a@b.com":{}'
if run >/dev/null 2>&1; then
	echo "ok   matching profile ids exit 0"
else
	echo "FAIL matching profile ids should exit 0"; rc=1
fi

# 2. main gains a profile bob lacks — bob inherits it. This is the NVB-17 shape:
#    one agent's calendar token mirrored into main and read through by everyone.
rm -f "$fx/agents"/*/agent/openclaw-agent.sqlite
seed main '"xai:a@b.com":{},"google:cal":{}'
seed bob '"xai:a@b.com":{}'
out=$(run 2>&1) && { echo "FAIL an inherited profile should exit 1"; rc=1; } || {
	case "$out" in
		*"VIOLATION"*"google:cal"*) echo "ok   inherited profile named and exits 1" ;;
		*) echo "FAIL violation did not name google:cal"; rc=1 ;;
	esac
}

# 3. A tool credential held by EVERY agent inherits nothing — but it still must not
#    be in the auth store at all (ADR 0013). Rule 2 alone would pass this.
rm -f "$fx/agents"/*/agent/openclaw-agent.sqlite
seed main '"xai:a@b.com":{},"google:cal":{}'
seed bob '"xai:a@b.com":{},"google:cal":{}'
out=$(run 2>&1) && { echo "FAIL a tool credential in the store should exit 1"; rc=1; } || {
	case "$out" in
		*"TOOL credential"*"google:cal"*) echo "ok   tool credential caught even when nothing inherits" ;;
		*) echo "FAIL did not report google:cal as a tool credential"; rc=1 ;;
	esac
}

# 4. A store that exists but cannot be read must not read as "holds nothing".
#    A corrupt file, because the cause does not matter — permissions, corruption or
#    a bad disk all produce the same silent pass if the error is swallowed.
rm -f "$fx/agents"/*/agent/openclaw-agent.sqlite
seed main '"xai:a@b.com":{}'
printf 'not a database' >"$fx/agents/bob/agent/openclaw-agent.sqlite"
out=$(run 2>&1); rc_run=$?
if [ "$rc_run" = 2 ] && case "$out" in *"Could not read"*bob*) true ;; *) false ;; esac; then
	echo "ok   unreadable store exits 2 rather than passing silently"
else
	echo "FAIL unreadable store should exit 2 and name bob (got $rc_run)"; rc=1
fi

# 5. Model providers other than the default one are not strays.
rm -f "$fx/agents"/*/agent/openclaw-agent.sqlite
seed main '"anthropic:a@b.com":{}'
seed bob '"anthropic:a@b.com":{}'
if run >/dev/null 2>&1; then
	echo "ok   a second model provider is not a stray"
else
	echo "FAIL anthropic: should be an allowed model prefix"; rc=1
fi

exit "$rc"
