#!/usr/bin/env bash
# Refresh the opencode xAI OAuth token and republish it to GitHub repo secrets.
# Run every 4h by wpa-oc-auth.timer, or by hand:
#
#   sudo systemctl start wpa-oc-auth.service
#
# WHY THIS EXISTS. Coding work is dispatched to GitHub Actions — a `/oc` comment
# on an issue runs opencode as grok inside the runner — and not to the agent on
# this Pi, which has no exec tool and a network-less sandbox. So the runner needs
# an xAI credential, and the one we use is a SUBSCRIPTION OAuth token rather than
# a metered API key.
#
# That choice is the whole reason this script is not a one-liner. The refresh
# token ROTATES on use (verified 2026-08-15), so whichever machine refreshes it
# invalidates every other copy. This box is the ONLY refresher, held by:
#
#   1. force a refresh every run, so what is published carries a full ~6h life
#      rather than whatever happened to be left of one;
#   2. prove the refresh took before publishing, because a silent failure would
#      otherwise put a nearly-dead token in the secret and 401 CI hours later;
#   3. strip the refresh token out of the published copy, so a runner cannot
#      rotate ours even if it tried;
#   4. run at 4h against a ~6h token, so a runner never reaches the near-expiry
#      skew that would make it want to refresh in the first place.
#
# Break any of them and the failure is remote and delayed: CI starts 401ing some
# hours later, or this box quietly loses its own login. See
# docs/runbooks/05-opencode-ci-token.md.
set -euo pipefail

# From /etc/wpa-oc.env (root-owned, 0600). Kept out of the repo: the repo is
# public and GH_TOKEN is a live credential. It is a fine-grained PAT carrying
# Secrets:write and nothing else, over exactly the repos in OC_REPOS.
: "${GH_TOKEN:?set in /etc/wpa-oc.env}"
: "${OC_MODEL:?set in /etc/wpa-oc.env}"
: "${OC_REPOS:?set in /etc/wpa-oc.env}"

auth="$HOME/.local/share/opencode/auth.json"
secret=OPENCODE_AUTH_JSON

# systemd hands over a minimal PATH and the curl installer puts opencode under
# the home directory, so a bare `opencode` in a unit is "command not found" —
# which reads like a broken install rather than a missing PATH.
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
for tool in opencode gh jq; do
  command -v "$tool" >/dev/null || { echo "$tool not on PATH" >&2; exit 1; }
done
[[ -s "$auth" ]] || { echo "no auth store at $auth — run 'opencode auth login'" >&2; exit 1; }

# Keep a copy: the refresh is provoked by corrupting the live store, so a failure
# between here and the check below would otherwise leave this box with a token
# marked expired and nothing to put back.
cp -p "$auth" "$auth.bak"
trap 'rm -f "$auth.bak"' EXIT

# ponytail: there is no `opencode auth refresh`. Backdating the expiry makes
# opencode treat the grant as stale and refresh on the next call, so this does
# not depend on whatever skew upstream happens to use. Replace if a real command
# lands. The `.xai` key path is what this provider writes today; `jq . "$auth"`
# if a version bump moves it.
jq '.xai.expires = 0' "$auth.bak" > "$auth"
chmod 600 "$auth"
# stderr is deliberately NOT discarded: when a refresh fails, the reason is the
# only thing that makes the failure below diagnosable. `|| true` because the
# expiry check, not the exit code, is what decides whether the refresh took.
opencode run --dir "$HOME" -m "$OC_MODEL" ok >/dev/null || true

# Rule 2. `expires` is in MILLISECONDS (13 digits, verified on hardware) — the
# same unit as `date +%s%3N`. Comparing against a seconds clock would make this
# always fail, and a check that always fails is a check nobody keeps.
now=$(date +%s%3N)
exp=$(jq -r '.xai.expires // 0' "$auth")
if (( exp <= now + 3600000 )); then
  echo "xai token expires in under 1h (exp=$exp now=$now) — the refresh did not take" >&2
  cp -p "$auth.bak" "$auth"   # put the working credential back for the next run
  exit 1
fi

# Rule 3, and it is the containment: a runner holding no usable refresh token
# cannot rotate ours, so the worst case is one CI run returning 401 rather than this
# box losing its login to a machine we do not control.
#
# ⚠️ BLANK THE KEY, DO NOT DELETE IT. opencode's OAuth credential schema declares
# `refresh: Schema.String` — required, unlike the `Schema.optional` fields beside it
# — and the loader is `Record.filterMap(data, v => Result.fromOption(decode(v)))`,
# which DROPS a credential that fails to decode with no error, no warning and no log
# line. So a deleted key silently unregisters the whole provider on the runner, and
# the failure surfaces two layers away as
#
#     Model not found: xai/grok-4.6. Did you mean: grok-4.6, grok-4.6-fast?
#
# because getModel falls back to suggesting from the static catalog. The model string
# was never wrong. An empty string decodes fine and is exactly as useless to a runner
# as an absent one. Verified against opencode v1.18.18, auth/index.ts.
runner_auth=$(jq -c 'map_values(.refresh = "")' "$auth")

# The guard has to change meaning along with the transform: "is the key gone" is now
# the wrong question, because the key is supposed to be there. Ask the one that
# matters — does the real token appear anywhere in what we are about to publish.
real_refresh=$(jq -r '.xai.refresh // ""' "$auth")
case "$real_refresh" in
  ""|null) echo "no refresh token in the local store — refusing to publish" >&2; exit 1 ;;
esac
case "$runner_auth" in
  *"$real_refresh"*) echo "the refresh token survived blanking — refusing to publish" >&2; exit 1 ;;
esac

# Published as raw JSON, NOT base64: the workflow's restore step writes the
# secret straight to auth.json. If one side changes, both must.
read -ra repos <<< "$OC_REPOS"
for repo in "${repos[@]}"; do
  printf '%s' "$runner_auth" | gh secret set "$secret" --repo "$repo"
  echo "pushed $secret -> $repo (valid until $(date -d "@$((exp / 1000))" '+%F %T %Z'))"
done
