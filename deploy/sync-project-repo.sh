#!/usr/bin/env bash
# Keep a read-only mirror of the project repo inside the agent's workspace, so it can
# read the code with the `read` tool it already has.
#
# WHY A MIRROR RATHER THAN TOOLS. Letting the agent fetch would need either exec plus
# a network in the sandbox — undoing NVB-14/23/25 — or GitHub repo tools, which read
# one file per call against a 5000-char window and need a wider PAT. A directory
# appearing in a workspace it already reads grants nothing at all.
#
# THE AGENT DECIDES WHEN. It cannot run this, but it can ask: writing
# `repo-sync.request` into its workspace makes the next tick sync immediately. That
# is ADR 0009's shape — the agent expresses an intent, the host runs a fixed command
# with no agent-controlled arguments, so there is nothing to smuggle through.
#
# See docs/runbooks/06-the-project-room.md.
set -euo pipefail

# From /etc/wpa-project.env (root-owned, 0600). Shared with wpa-gh-watch so the repo
# slug has one home: two files naming the same repo is the drift ADR 0011 regrets.
: "${GH_REPO:?set in /etc/wpa-project.env}"
: "${GH_WORKSPACE:?set in /etc/wpa-project.env}"

# How stale the mirror may get with nobody asking. The timer runs far more often than
# this; most ticks do nothing and cost one stat.
max_age=600

repo="$GH_WORKSPACE/repo"
status="$GH_WORKSPACE/repo-sync.json"
request="$GH_WORKSPACE/repo-sync.request"

now=$(date +%s)
last=0
[ -s "$status" ] && last=$(jq -r '.synced_at_epoch // 0' "$status" 2>/dev/null || echo 0)

if [ -e "$request" ]; then
  reason=request
  # Removed BEFORE the sync, not after: if the fetch fails, the next tick should try
  # on its own schedule rather than replaying a request forever.
  rm -f "$request"
elif [ $(( now - last )) -ge "$max_age" ]; then
  reason=schedule
else
  exit 0
fi

# git goes through libcurl, which does Happy Eyeballs and falls back to IPv4 by
# itself — this is NOT the Go-resolver trap that needs GODEBUG=netdns=cgo on this box.
if [ -d "$repo/.git" ]; then
  git -C "$repo" fetch --quiet --depth 50 origin main
  # reset+clean rather than pull: the workspace is read-write to the agent, so the
  # checkout can have been edited. A mirror that force-heals is simpler to reason
  # about than one that can wedge on a conflict at 04:00.
  git -C "$repo" reset --quiet --hard origin/main
  git -C "$repo" clean -qfd
else
  # Self-healing: also the path taken if the agent deletes .git.
  rm -rf "$repo"
  git clone --quiet --depth 50 --single-branch --branch main \
    "https://github.com/$GH_REPO" "$repo"
fi

sha=$(git -C "$repo" rev-parse HEAD)
subject=$(git -C "$repo" log -1 --pretty=%s)
synced=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# The status file is the point of the exercise as much as the checkout is. A mirror
# that merely looks current is worse than none: the agent must be able to say which
# commit it is reasoning from rather than implying the tip. Written outside the
# checkout so `git clean` does not remove it.
jq -n --arg repo "$GH_REPO" --arg sha "$sha" --arg subject "$subject" \
      --arg synced "$synced" --arg reason "$reason" --argjson epoch "$now" \
  '{repo: $repo, branch: "main", commit: $sha, subject: $subject,
    synced_at: $synced, synced_at_epoch: $epoch, reason: $reason}' > "$status"

echo "synced $GH_REPO@${sha:0:12} ($reason): $subject"
