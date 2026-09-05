#!/usr/bin/env bash
# Put this checkout on the box. One command, idempotent, safe to re-run.
#
#   cd /opt/wpa && sudo git pull && sudo deploy/install.sh
#
# WHAT THIS DOES NOT DO, because it cannot: the gateway's own config lives at
# /var/lib/openclaw/.openclaw/openclaw.json, outside this repo and outside git.
# `config/openclaw.example.json5` is the documented mirror of it, not its source —
# nothing here applies it. Agent and tool-policy changes are still edited on the box
# and then reflected back into the example. See runbook 06.
#
# It also never restarts a long-running service. Installing a unit file does not
# change the process already running from the old one, so this reports what needs a
# restart and leaves the timing to you — restarting the gateway interrupts every
# agent mid-conversation, which is not a thing a deploy script should decide.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
repo=$(cd "$(dirname "$0")/.." && pwd)

# ---------------------------------------------------------------------------
# The checkout is the deployed artifact for wpa-gate and wpa-reader — both run
# `python3 -m` straight out of /opt/wpa/src — so a stale or half-merged tree is a
# deploy, not a detail. Reported rather than corrected: fixing it is a decision.
# ---------------------------------------------------------------------------
if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
	branch=$(git -C "$repo" rev-parse --abbrev-ref HEAD)
	echo "checkout: $branch @ $(git -C "$repo" rev-parse --short HEAD)"
	[ "$branch" = main ] || echo "  ! not on main — deploying a branch"
	git -C "$repo" diff --quiet || echo "  ! working tree has uncommitted changes"
	git -C "$repo" fetch -q origin 2>/dev/null || true
	behind=$(git -C "$repo" rev-list --count "HEAD..origin/$branch" 2>/dev/null || echo 0)
	[ "$behind" = 0 ] || echo "  ! $behind commit(s) behind origin/$branch — did you pull?"
fi

# Unit contents before, so we can name what actually changed rather than telling the
# operator to restart everything on every deploy. A restart notice nobody believes is
# the same as no restart notice.
units_before=$(md5sum /etc/systemd/system/wpa-*.service /etc/systemd/system/wpa-*.timer \
	/etc/systemd/system/wpa-*.path 2>/dev/null | sort || true)

echo
echo "== code, config skeleton and unit files =="
# Also installs every unit file (*.service *.timer *.path), creates the service
# users, and enables the reader, staleness and config-backup units.
"$repo/deploy/install-reader.sh"

# ---------------------------------------------------------------------------
# The Python dependencies, which until NVB-35 did not exist. wpa-reader and wpa-gate
# still run `python3 -m` against system Python and need nothing; the `wpa` MCP server
# needs the `mcp` SDK, so /opt/wpa gets a venv and the gateway spawns that interpreter.
#
# WHY uv AND NOT pip. uv.lock is committed and CI runs --locked precisely so a stale
# lock fails the build instead of quietly resolving a different dependency set. That
# argument is stronger here than in CI: `pip install mcp` on the box would resolve ~28
# packages that nothing tested, into the process Phase 4 gives a sudo right.
#
# WHY THIS SCRIPT DOES NOT INSTALL uv. It has never fetched anything from the internet,
# and a deploy script that curls a binary into /usr/local/bin is a bigger change than
# the feature asking for it. Runbook 01 carries the one-time install; this checks.
#
# --python /usr/bin/python3 pins the Pi's system 3.11 so a deploy cannot silently
# download a managed CPython. --no-dev because the checks below are shell and node
# only — pytest never runs on the box.
#
# Non-fatal, like the checks at the bottom: a failure here breaks one MCP server, and
# aborting half way through leaves the box in a state nobody described.
# ---------------------------------------------------------------------------
echo
echo "== python dependencies =="
if ! command -v uv >/dev/null 2>&1; then
	echo "  ! uv not installed — /opt/wpa/.venv cannot be built"
	echo "    the wpa MCP server will fail to spawn; see docs/runbooks/01-pi-base-setup.md"
elif uv sync --project /opt/wpa --no-dev --locked --python /usr/bin/python3 >/dev/null 2>&1; then
	# The gateway spawns this interpreter as `openclaw`, so it has to be traversable
	# by a user that owns none of it. Same reason the plugin install chmods a+rX.
	chmod -R a+rX /opt/wpa/.venv
	echo "  /opt/wpa/.venv  $("/opt/wpa/.venv/bin/python" -V 2>&1)"
else
	echo "  ! uv sync FAILED — rerun by hand to see why:"
	echo "      sudo uv sync --project /opt/wpa --no-dev --locked --python /usr/bin/python3"
	echo "    a stale uv.lock is the usual cause; --locked refuses to resolve around it"
fi

# ---------------------------------------------------------------------------
# A SECOND venv, and this box will never run what is in it (NVB-42).
#
# It exists so the sandboxed agent can run this repo's tests. The sandbox image
# carries `git` and `python3` and nothing else — no uv, no pytest, no node, no
# compiler — and `network: none` means a container can never fetch them.
#
# WHY IT LIVES IN THE AGENT'S WORKSPACE AND NOT SOMEWHERE TIDIER. The obvious
# answer is `agents.defaults.sandbox.docker.binds`, and it cannot be done:
# `createSandboxContainer` passes `bindSourceRoots: [workspaceDir,
# agentWorkspaceDir]`, hardcoded, and the security validator rejects any bind
# whose source is outside them. Worse, that check runs at container creation, so
# one bad global bind fails every sandboxed turn for EVERY agent until it is
# removed. The workspace is already mounted, so putting the venv inside it needs
# no config at all — it simply appears at /workspace/.venv-sandbox.
#
# It must be invoked as `bin/python -m pytest`. The venv is built here at one
# absolute path and read at another inside the container; sys.prefix follows the
# runtime executable, but the console scripts in `bin/` carry the build path in
# their shebangs and do not survive the move.
#
# --group dev is the difference from the venv above: that one is the MCP server's
# runtime and has no business carrying pytest.
# ---------------------------------------------------------------------------
echo
echo "== sandbox test venv =="
builder_ws=/var/lib/openclaw/.openclaw/workspace-builder
sandbox_venv="$builder_ws/.venv-sandbox"
if [ ! -d "$builder_ws" ]; then
	echo "  skipped — no builder workspace yet (deploy the agent first: runbook 06)"
elif ! command -v uv >/dev/null 2>&1; then
	echo "  ! uv not installed — the agent loses its test loop, nothing else breaks"
elif UV_PROJECT_ENVIRONMENT="$sandbox_venv" \
	uv sync --project /opt/wpa --group dev --locked --python /usr/bin/python3 >/dev/null 2>&1; then
	# The agent reads it as `openclaw`, and the container's uid 0 maps back to that
	# same user under the rootless daemon (NVB-25).
	chown -R openclaw:openclaw "$sandbox_venv"
	echo "  $sandbox_venv  $("$sandbox_venv/bin/python" -V 2>&1)"
	echo "    in-container: /workspace/.venv-sandbox/bin/python -m pytest"
else
	echo "  ! uv sync FAILED for the sandbox venv — the agent loses its test loop,"
	echo "    nothing else breaks. Rerun by hand to see why:"
	echo "      sudo env UV_PROJECT_ENVIRONMENT=$sandbox_venv \\"
	echo "        uv sync --project /opt/wpa --group dev --locked --python /usr/bin/python3"
fi

# ---------------------------------------------------------------------------
# The privileged helpers. install-reader.sh handles wpa-config-backup because the
# path unit it feeds is useless without it; the rest live here.
#
# Format: <source under deploy/> <installed name> <mode>
# ---------------------------------------------------------------------------
echo
echo "== helper scripts =="
while read -r src dest mode; do
	[ -n "$src" ] || continue
	install -m "$mode" "$repo/deploy/$src" "/usr/local/bin/$dest"
	printf '  %-22s %s  (%s)\n' "$dest" "$mode" "$src"
done <<-'EOF'
	check-agent-auth.sh    wpa-agent-auth      0755
	gh-watch.sh            wpa-gh-watch        0755
	push-opencode-auth.sh  wpa-oc-auth         0755
	sync-project-repo.sh   wpa-project-sync    0755
	backup-signal.sh       wpa-signal-backup   0755
	mcp-server.sh          wpa-mcp             0755
	outbox-notify.sh       wpa-outbox-notify   0700
	triage.sh              wpa-triage          0700
	token-expiry.sh        wpa-token-expiry    0700
	wpa-apply              wpa-apply           0755
	wpa-apply-preview      wpa-apply-preview   0755
	wpa-config-pull        wpa-config-pull     0755
EOF
# 0700 on the notifier is not a typo: it writes into an agent outbox owned by the
# gate, and nothing but root has any business invoking it. wpa-triage inherits
# that for the same reason (it execs the notifier), and wpa-token-expiry because
# it reads two PATs.
#
# wpa-apply{,-preview} and wpa-config-pull are the NVB-37 root helpers. openclaw
# reaches them only through /etc/sudoers.d/wpa-openclaw (exact path, no args).

# ---------------------------------------------------------------------------
# sudoers for the deploy path. A compromised gateway holds these rights whether
# or not a human approved a call — NVB-22, named rather than papered over. The
# file is validated with visudo before it replaces anything live.
# ---------------------------------------------------------------------------
echo
echo "== sudoers (NVB-37 deploy path) =="
sudoers_src="$repo/deploy/sudoers.d/wpa-openclaw"
sudoers_dst=/etc/sudoers.d/wpa-openclaw
if [ ! -f "$sudoers_src" ]; then
	echo "  ! $sudoers_src missing — deploy helpers will not be invocable via sudo"
elif ! command -v visudo >/dev/null 2>&1; then
	echo "  ! visudo not installed — refusing to write $sudoers_dst"
elif ! visudo -cf "$sudoers_src" >/dev/null 2>&1; then
	echo "  ! $sudoers_src failed visudo -cf — left $sudoers_dst untouched"
	visudo -cf "$sudoers_src" || true
else
	install -m 0440 -o root -g root "$sudoers_src" "$sudoers_dst"
	echo "  $sudoers_dst  0440  (wpa-apply, wpa-apply-preview, wpa-config-pull)"
	echo "  a compromised gateway uid holds these rights — see runbook 07"
fi

# git's dubious-ownership check, which refuses root as hard as anyone else.
#
# /opt/wpa is owned by a login user, and root's exemption keys on $SUDO_UID: a human
# running `sudo git pull` is uid 1000 and matches, the gateway sudoing in is uid 991
# and does not. So every helper's `git fetch` failed while every manual deploy
# worked — the one asymmetry that hides a broken deploy path from the person testing
# it. Declaring the path here is narrower than chowning the checkout to root, which
# would take the human's own git with it.
if command -v git >/dev/null 2>&1; then
	if git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$repo"; then
		echo "  safe.directory already names $repo"
	else
		git config --system --add safe.directory "$repo"
		echo "  git config --system safe.directory += $repo"
	fi
fi

# ---------------------------------------------------------------------------
# OpenClaw plugins we own. These run INSIDE the gateway process, so they are the
# most privileged code this repo installs — and OpenClaw enforces that: it refuses
# a plugin directory owned by anyone but root or the gateway user, reporting
# "blocked plugin candidate: suspicious ownership", which reads exactly like an
# allowlist problem and is not one. Hence the explicit chown.
#
# Installing them changes nothing on its own. A plugin is loaded only if
# `plugins.load.paths` names this directory AND `plugins.allow` names the plugin,
# both in the gateway's own config, which this repo does not deploy (runbook 07).
# ---------------------------------------------------------------------------
echo
echo "== openclaw plugins =="
oc_plugins=/usr/local/lib/wpa/openclaw-plugins
install -d -m 0755 -o root -g root "$oc_plugins"
for p in "$repo"/deploy/openclaw-plugins/*/; do
	[ -d "$p" ] || continue
	name=$(basename "$p")
	rsync -a --delete --exclude '*.test.mjs' "$p" "$oc_plugins/$name/"
	chown -R root:root "$oc_plugins/$name"
	chmod -R a+rX "$oc_plugins/$name"
	printf '  %-22s %s\n' "$name" "$oc_plugins/$name"
done
echo "  a changed plugin needs a gateway restart — code is loaded at startup"

systemctl daemon-reload

echo
echo "== timers =="
# Every timer this repo owns. install-reader.sh enables the three it is responsible
# for; enabling twice is a no-op, and listing them all here means a new box needs one
# command rather than one command plus a memory of which timers exist.
for t in wpa-reader.timer wpa-staleness.timer wpa-signal-backup.timer \
         wpa-oc-auth.timer wpa-project-sync.timer wpa-gh-watch.timer \
         wpa-agent-auth.timer wpa-token-expiry.timer; do
	systemctl enable --now "$t" >/dev/null 2>&1 && printf '  %-26s enabled\n' "$t" \
		|| printf '  %-26s FAILED — systemctl status %s\n' "$t" "$t"
done
systemctl enable --now wpa-config-backup.path >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# What is running from an old unit file. A timer-driven oneshot picks its new
# script up on the next tick and needs nothing; a long-running service does not.
# ---------------------------------------------------------------------------
units_after=$(md5sum /etc/systemd/system/wpa-*.service /etc/systemd/system/wpa-*.timer \
	/etc/systemd/system/wpa-*.path 2>/dev/null | sort || true)
changed=$(comm -13 <(printf '%s\n' "$units_before") <(printf '%s\n' "$units_after") \
	| awk '{print $2}' | xargs -r -n1 basename || true)

echo
echo "== restarts you may need =="
needed=""
for svc in wpa-gate.service wpa-openclaw.service signal-cli.service; do
	printf '%s\n' "$changed" | grep -qxF "$svc" && needed="$needed $svc"
done
if [ -n "$needed" ]; then
	echo "  unit file changed for:$needed"
	echo "  the running process still has the old one — restart when it suits you:"
	for s in $needed; do echo "      sudo systemctl restart $s"; done
else
	echo "  none — no long-running service's unit changed"
fi
[ -n "$changed" ] && { echo "  (changed units this run:"; printf '     %s\n' $changed; echo "  )"; } || true
echo "  timer-driven jobs need nothing: the next tick runs the new script."

echo
echo "== checks =="
# Non-fatal on purpose. A failing invariant is worth reporting loudly, but it must
# not abort a deploy half way through and leave the box in a state nobody described.
bash "$repo/deploy/check-agent-auth.test.sh" || echo "  ! check-agent-auth.test.sh FAILED"
bash "$repo/deploy/gh-watch.test.sh"         || echo "  ! gh-watch.test.sh FAILED"
bash "$repo/deploy/triage.test.sh"           || echo "  ! triage.test.sh FAILED"
bash "$repo/deploy/token-expiry.test.sh"     || echo "  ! token-expiry.test.sh FAILED"
# CI is mypy + pytest, which cannot see a JavaScript plugin, so its one check runs
# here instead — the same reason the shell tests above do.
for t in "$repo"/deploy/openclaw-plugins/*/*.test.mjs; do
	[ -f "$t" ] || continue
	node "$t" || echo "  ! $(basename "$t") FAILED"
done
echo
/usr/local/bin/wpa-agent-auth || echo "  ! credential isolation check FAILED (see above)"

echo
echo "done."
