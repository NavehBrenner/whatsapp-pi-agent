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
	outbox-notify.sh       wpa-outbox-notify   0700
EOF
# 0700 on the notifier is not a typo: it writes into an agent outbox owned by the
# gate, and nothing but root has any business invoking it.

systemctl daemon-reload

echo
echo "== timers =="
# Every timer this repo owns. install-reader.sh enables the three it is responsible
# for; enabling twice is a no-op, and listing them all here means a new box needs one
# command rather than one command plus a memory of which timers exist.
for t in wpa-reader.timer wpa-staleness.timer wpa-signal-backup.timer \
         wpa-oc-auth.timer wpa-project-sync.timer wpa-gh-watch.timer \
         wpa-agent-auth.timer; do
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
echo
/usr/local/bin/wpa-agent-auth || echo "  ! credential isolation check FAILED (see above)"

echo
echo "done."
