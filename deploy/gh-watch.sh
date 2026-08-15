#!/usr/bin/env bash
# Notice activity on the project repo and wake the agent that owns its room.
# Run every 60s by wpa-gh-watch.timer.
#
# WHY THIS EXISTS. The agent cannot watch GitHub itself: it has no scheduling tool
# (the built-in sandbox deny strips `cron` for sandboxed agents) and its sandbox has
# no network. GitHub cannot push either — the Pi is not reachable from outside. So
# something on this box has to look, and the only custom part of that is knowing
# what is *new*. OpenClaw supplies the rest: `openclaw agent --session-key` runs a
# turn in the room's own session, `--deliver` puts the reply in the room, and the
# agent's GitHub tools are already wired.
#
# The idle path costs one HTTPS call and no model tokens. A turn is only spent when
# something actually happened, which is the whole reason this is not an
# `openclaw cron` job waking the agent on a timer to find nothing.
#
# See docs/runbooks/06-the-project-room.md.
set -euo pipefail

# From /etc/wpa-project.env (root-owned, 0600). No secrets in it — the GitHub token
# is read out of the gateway's own config below rather than copied to a second place.
: "${GH_REPO:?set in /etc/wpa-project.env}"          # owner/name
: "${GH_SESSION_KEY:?set in /etc/wpa-project.env}"   # agent:<id>:signal:group:<id>

state="${STATE_DIRECTORY:-/var/lib/wpa-gh-watch}"
cursor="$state/cursor"        # ISO8601; the window start for the next poll
seen="$state/seen"            # event ids already reported, newest last
owner_file="$state/token-owner"

api="https://api.github.com"
# -4 because this Pi has no global IPv6 and a resolver that prefers AAAA stalls on
# an address that was never reachable. Same class of trap as the dockerd drop-in.
curl_opts=(-4 --silent --show-error --fail-with-body --max-time 30)

# The token lives in exactly one place: the MCP server's env in the gateway config.
# Reading it here rather than copying it into another file keeps that true.
token=$(jq -r '.mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN // empty' \
  "$HOME/.openclaw/openclaw.json")
[ -n "$token" ] || { echo "no GitHub token in the gateway config" >&2; exit 1; }

gh_api() { curl "${curl_opts[@]}" -H "Authorization: Bearer $token" \
  -H "Accept: application/vnd.github+json" "$@"; }

# Whose comments to ignore. The agent posts `/oc` comments as this account, so
# without this the detector wakes the agent to tell it what it just said.
if [ ! -s "$owner_file" ]; then
  gh_api "$api/user" | jq -r '.login' > "$owner_file"
fi
token_owner=$(cat "$owner_file")

# A five minute overlap on every poll. `since` is inclusive and clocks drift, so the
# cheap fix for a boundary event is to re-fetch it and dedupe by id — the opposite
# order (trusting the cursor) loses events silently, which is the failure that would
# take a week to notice.
if [ -s "$cursor" ]; then
  window=$(cat "$cursor")
else
  window=$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
fi

touch "$seen"
events=""

note() {   # id, text
  grep -qxF "$1" "$seen" && return 0
  echo "$1" >> "$seen"
  events="${events:+$events; }$2"
}

# Issue comments — this is where the /oc plan reply lands.
comments=$(gh_api "$api/repos/$GH_REPO/issues/comments?since=$window&sort=updated&direction=desc&per_page=50")
while IFS=$'\t' read -r id login updated number body; do
  [ -n "${id:-}" ] || continue
  [ "$login" = "$token_owner" ] && continue
  note "comment:$id" "new comment by $login on issue #$number: $(printf '%.140s' "$body")"
done < <(jq -r '.[] | [.id, .user.login, .updated_at,
                       (.issue_url | split("/") | last),
                       (.body // "" | gsub("[\n\t]"; " "))] | @tsv' <<<"$comments")

# Issues AND pull requests: the API treats a PR as an issue, so "PR opened" and
# "PR merged" arrive here too, with .pull_request present to tell them apart.
issues=$(gh_api "$api/repos/$GH_REPO/issues?state=all&since=$window&sort=updated&direction=desc&per_page=50")
while IFS=$'\t' read -r number kind stat login updated title; do
  [ -n "${number:-}" ] || continue
  # Same author filter as the comments above, and it costs nothing here: a PR is
  # authored by whoever opened it, so a PR the runner raised still reports when
  # Naveh merges it. What this drops is the agent being woken to be told about an
  # issue it just opened itself.
  [ "$login" = "$token_owner" ] && continue
  note "$kind:$number:$updated" "$kind #$number ($stat) by $login: $title"
done < <(jq -r '.[] | [.number,
                       (if .pull_request then "PR" else "issue" end),
                       .state, .user.login, .updated_at,
                       (.title // "" | gsub("[\n\t]"; " "))] | @tsv' <<<"$issues")

# The cursor advances to NOW minus the overlap, not to the newest event seen. Those
# differ when nothing happened: anchoring on the newest event freezes the window, so
# a quiet week means every poll re-fetches a week of history. With per_page=50 and no
# pagination that eventually drops events off the end — the quiet repo silently
# breaking the busy one. The overlap plus the seen-list is what makes advancing safe.
date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ > "$cursor"
# ponytail: a flat file, trimmed. Ids are small and 500 covers days of activity.
tail -n 500 "$seen" > "$seen.tmp" && mv "$seen.tmp" "$seen"

[ -n "$events" ] || exit 0

# Wake the agent inside the room's own session, so it keeps its memory and the
# conversation so far, and `--deliver` puts the reply in the room.
#
# NOT `openclaw system event`, which is the obvious-looking call and does not work
# here: it enqueues an event and returns "ok" without running a turn, because the
# heartbeat that would consume the queue is suppressed by a comments-only
# HEARTBEAT.md. `--expect-final` still returns "ok". Verified 2026-08-15.
#
# `--deliver` alone is sufficient; `--channel signal --reply-to group:<id>` also
# works and is redundant. Note the gateway logs NO "delivered reply" line for a
# CLI-initiated turn, so that log is not proof of delivery either way — the room is.
#
# A failure here must be loud: a detector that silently stops reporting is
# indistinguishable from a quiet repo, which is the whole failure this guards.
echo "waking $GH_SESSION_KEY: $events"
openclaw agent --session-key "$GH_SESSION_KEY" --deliver --timeout 120 \
  --message "GitHub activity on $GH_REPO — $events. Work out what it means for the work in flight, and tell Naveh only if it needs him."
