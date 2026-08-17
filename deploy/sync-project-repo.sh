#!/usr/bin/env bash
# Keep a read-only mirror of the project repo inside the agent's workspace, so it can
# read the code with the `read` tool it already has — `main` in `repo/`, and one
# checkout per open pull request in `pr-<N>/` beside a `pr-<N>.diff`.
#
# WHY A MIRROR RATHER THAN TOOLS. Letting the agent fetch would need either exec plus
# a network in the sandbox — undoing NVB-14/23/25 — or GitHub repo tools, which read
# one file per call against a 5000-char window and need a wider PAT. A directory
# appearing in a workspace it already reads grants nothing at all.
#
# WHY THE DIFF FILE AND NOT JUST THE CHECKOUT. The agent has no `exec`, so it cannot
# run `git diff` itself. Without the diff written out it would be comparing two trees
# by eye, which is not a code review.
#
# THE AGENT DECIDES WHEN. It cannot run this, but it can ask: writing
# `repo-sync.request` into its workspace makes the next tick sync immediately. That
# is ADR 0009's shape — the agent expresses an intent, the host runs a fixed command
# with no agent-controlled arguments, so there is nothing to smuggle through.
# WPA_SYNC_FORCE is the host's own version of that, set by wpa-gh-watch, and it is
# deliberately an environment flag rather than an argument for the same reason.
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

# wpa-gh-watch runs this inline every 60s while the timer fires independently, so two
# copies can reach the same .git. Concurrent fetch and `reset --hard` race on
# index.lock, and under `set -e` a lost race becomes an OnFailure DM about nothing.
# Bounded rather than blocking forever: the caller has its own timeout, and waiting
# past it would report a failure that had already succeeded in the other process.
exec 9>"$GH_WORKSPACE/.git.lock"
flock -w 45 9 || { echo "another sync holds the lock" >&2; exit 1; }

now=$(date +%s)
last=0
[ -s "$status" ] && last=$(jq -r '.synced_at_epoch // 0' "$status" 2>/dev/null || echo 0)

# Consumed before the reason is chosen, not inside the branch: a forced run serves the
# request too, and leaving the file would make the next tick sync a second time for a
# request already answered. Removed BEFORE the sync, not after — if the fetch fails,
# the next tick should try on its own schedule rather than replaying a request forever.
requested=0
if [ -e "$request" ]; then requested=1; rm -f "$request"; fi

if [ -n "${WPA_SYNC_FORCE:-}" ]; then
  reason=wake
elif [ "$requested" = 1 ]; then
  reason=request
elif [ $(( now - last )) -ge "$max_age" ]; then
  reason=schedule
else
  exit 0
fi

# `refs/pull/*/head` costs nothing to fetch and needs NO credential on a public repo,
# which is the whole reason the PR checkouts did not widen the PAT. The refs persist
# after a PR closes, so what is open is decided below by the API, never by their
# presence here.
pulls='+refs/pull/*/head:refs/remotes/pr/*'

# git goes through libcurl, which does Happy Eyeballs and falls back to IPv4 by
# itself — this is NOT the Go-resolver trap that needs GODEBUG=netdns=cgo on this box.
if [ -d "$repo/.git" ]; then
  git -C "$repo" fetch --quiet --prune --depth 50 origin main "$pulls"
  # reset+clean rather than pull: the workspace is read-write to the agent, so the
  # checkout can have been edited. A mirror that force-heals is simpler to reason
  # about than one that can wedge on a conflict at 04:00.
  git -C "$repo" reset --quiet --hard origin/main
  git -C "$repo" clean -qfd
else
  # Self-healing: also the path taken if the agent deletes .git. The PR checkouts are
  # linked worktrees of this store, so they go with it — left behind they would keep a
  # .git file pointing at a store that no longer exists, and the agent would read a
  # frozen tree as if it were current.
  rm -rf "$repo" "$GH_WORKSPACE"/pr-*
  git clone --quiet --depth 50 --single-branch --branch main \
    "https://github.com/$GH_REPO" "$repo"
  git -C "$repo" fetch --quiet --depth 50 origin "$pulls"
fi

# WHICH PRs ARE OPEN comes from the API, not from git. `main` is protected as linear
# history so PRs land squashed, which means a merged PR's head is never an ancestor of
# main and `--is-ancestor` would report nothing has ever merged. The open set is also
# the only signal that catches a PR closed WITHOUT merging, and it self-corrects after
# a tick that failed. Same token as wpa-gh-watch, read from the same single place —
# unauthenticated would work on a public repo but shares a 60/hour limit with a 60s
# timer, which is exactly the ceiling.
token=$(jq -r '.mcp.servers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN // empty' \
  "$HOME/.openclaw/openclaw.json")
[ -n "$token" ] || { echo "no GitHub token in the gateway config" >&2; exit 1; }
open=$(curl -4 --silent --show-error --fail-with-body --max-time 30 \
  -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$GH_REPO/pulls?state=open&per_page=50" \
  | jq -r '.[].number')

for n in $open; do
  git -C "$repo" rev-parse -q --verify "refs/remotes/pr/$n" >/dev/null || continue
  d="$GH_WORKSPACE/pr-$n"
  if [ -e "$d/.git" ]; then
    # Same force-heal policy as the mirror, and this is also how a PR that gained
    # commits since the last tick moves forward.
    git -C "$d" reset --quiet --hard "pr/$n"
    git -C "$d" clean -qfd
  else
    rm -rf "$d"
    git -C "$repo" worktree add --quiet --detach "$d" "pr/$n"
  fi
  # Three-dot: what the PR adds, not what main gained meanwhile. Both agree here
  # because main requires branches to be up to date, but the merge-base form is the
  # one that stays correct if that rule is ever relaxed.
  git -C "$repo" diff "origin/main...pr/$n" > "$GH_WORKSPACE/pr-$n.diff"
done

# Anything not in the open set goes: merged, closed, or a PR that vanished. One pass
# covers both the checkout and its diff, so neither can outlive the other.
for p in "$GH_WORKSPACE"/pr-*; do
  [ -e "$p" ] || continue
  n=$(basename "$p"); n=${n#pr-}; n=${n%.diff}
  printf '%s\n' $open | grep -qxF "$n" || rm -rf "$p"
done
git -C "$repo" worktree prune

sha=$(git -C "$repo" rev-parse HEAD)
subject=$(git -C "$repo" log -1 --pretty=%s)
synced=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# The head sha of every open PR, which wpa-gh-watch diffs against the previous run to
# notice new commits. It is here rather than in a state file of its own because the
# agent needs it for the same reason the watcher does: to say which revision it
# reviewed rather than implying the tip.
prs=$(for n in $open; do
        printf '%s %s\n' "$n" "$(git -C "$repo" rev-parse "pr/$n")"
      done | jq -Rn '[inputs | split(" ") | {number: (.[0]|tonumber), head: .[1]}]')

# The status file is the point of the exercise as much as the checkout is. A mirror
# that merely looks current is worse than none: the agent must be able to say which
# commit it is reasoning from rather than implying the tip. Written outside the
# checkout so `git clean` does not remove it.
jq -n --arg repo "$GH_REPO" --arg sha "$sha" --arg subject "$subject" \
      --arg synced "$synced" --arg reason "$reason" --argjson epoch "$now" \
      --argjson prs "$prs" \
  '{repo: $repo, branch: "main", commit: $sha, subject: $subject,
    synced_at: $synced, synced_at_epoch: $epoch, reason: $reason, prs: $prs}' > "$status"

echo "synced $GH_REPO@${sha:0:12} ($reason, $(printf '%s\n' $open | grep -c . || true) open PRs): $subject"
