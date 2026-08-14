#!/usr/bin/env bash
# Install signal-cli as a JSON-RPC daemon. Idempotent. Run on the Pi:
#   sudo deploy/install-signal.sh +972XXXXXXXXX
#
# Does NOT register the account — that needs a captcha and an SMS code, so it is
# a human step. See docs/runbooks/03-signal-cli.md, and run it before this.
set -euo pipefail

number=${1:?assistant number in E.164, e.g. +972500000000}
repo=$(cd "$(dirname "$0")/.." && pwd)

id wpa-signal >/dev/null 2>&1 ||
  useradd --system --no-create-home --shell /usr/sbin/nologin wpa-signal

# NVB-27: the daemon RUNS AS openclaw and keeps wpa-signal only as its group.
# OpenClaw delivers attachments by path under a 0700 directory it re-chmods on
# every generation, so a shared uid is the only thing that can read them; the
# group is what keeps wpa-gate's socket access working unchanged. This makes the
# gateway a prerequisite of the Signal install, which it did not used to be.
id openclaw >/dev/null 2>&1 || {
  echo "no openclaw user — install the gateway first (runbook 04)" >&2
  exit 1
}

# Stop first, because the next two lines change who owns the account directory
# underneath a daemon that writes to it. The window is seconds, and what falls
# into it is an inbound message hitting EACCES on the account store.
systemctl stop signal-cli.service 2>/dev/null || true

install -d -m 0700 -o openclaw -g wpa-signal /var/lib/wpa-signal
# install -d does not touch an existing tree, and systemd will not recursively
# chown a StateDirectory it did not create — so an upgrade from the wpa-signal
# uid needs this, and it is idempotent on a fresh box.
chown -R openclaw:wpa-signal /var/lib/wpa-signal

# Kept out of the repo: the repo is public and this is a working phone number.
# Root-owned, group-readable by the daemon's user and nobody else. systemd reads
# EnvironmentFile as root before dropping privileges, so the group is defence in
# depth rather than a requirement — it follows the unit's User= so that "who can
# read this" has one answer instead of a stale one.
install -m 0640 -o root -g openclaw /dev/null /etc/wpa-signal.env
printf 'ASSISTANT_NUMBER=%s\n' "$number" > /etc/wpa-signal.env

# Account state may have been created by a `signal-cli register` run as a human
# user. Move it under the daemon's user rather than leaving the assistant's
# identity in somebody's home directory.
for home in /home/*; do
  src="$home/.local/share/signal-cli"
  if [[ -d "$src/data" && ! -d /var/lib/wpa-signal/data ]]; then
    echo "migrating account state from $src"
    cp -a "$src/." /var/lib/wpa-signal/
    chown -R openclaw:wpa-signal /var/lib/wpa-signal
  fi
done

if [[ ! -d /var/lib/wpa-signal/data ]]; then
  echo "no registered account under /var/lib/wpa-signal — register first (runbook 03)" >&2
  exit 1
fi

install -m 0644 "$repo/deploy/systemd/signal-cli.service" /etc/systemd/system/

# The loopback JSON-RPC port OpenClaw drives the channel through has no owner
# check of its own (NVB-27). Its own unit, so a firewall failure is a visible
# failed unit rather than a dead control channel.
install -d -m 0755 /etc/nftables.d
install -m 0644 "$repo/deploy/nftables/wpa-signal-8081.nft" /etc/nftables.d/
install -m 0644 "$repo/deploy/systemd/wpa-signal-firewall.service" /etc/systemd/system/

# A spike-era drop-in carried --http before it moved into the unit itself.
# Leaving it would silently win over the ExecStart above.
rm -f /etc/systemd/system/signal-cli.service.d/10-http.conf
rmdir --ignore-fail-on-non-empty /etc/systemd/system/signal-cli.service.d 2>/dev/null || true

# The gate: its own user, in the daemon's group so it can reach the socket and
# nothing else. It must NOT be wpa-signal — /var/lib/wpa-signal is the account,
# and the process that parses messages from strangers has no business reading it.
id wpa-gate >/dev/null 2>&1 ||
  useradd --system --no-create-home --shell /usr/sbin/nologin -G wpa-signal wpa-gate
install -m 0644 "$repo/deploy/systemd/wpa-gate.service" /etc/systemd/system/

# The code lives at /opt/wpa, put there by install-reader.sh — the gate runs with
# ProtectHome=yes and cannot see a checkout in /home.
if [[ ! -f /opt/wpa/src/gate/signal.py || ! -f /opt/wpa/config/config.toml ]]; then
  echo "run deploy/install-reader.sh first: it puts the code and config in /opt/wpa" >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable --now wpa-signal-firewall.service
systemctl enable --now signal-cli.service
# Restart rather than start: an already-running daemon still has the old 0077
# umask, so the socket stays unreadable to the gate until it is recreated.
systemctl restart signal-cli.service
systemctl enable --now wpa-gate.service

# The weekly encrypted backup (NVB-9). Installed here rather than with the reader
# because it is part of owning this account, not part of reading WhatsApp. Runs
# from /usr/local/bin so it does not go out of sync with an rsync of /opt/wpa.
install -m 0755 "$repo/deploy/backup-signal.sh" /usr/local/bin/wpa-signal-backup
install -m 0644 "$repo"/deploy/systemd/wpa-signal-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload

# Enabling it without credentials would fail every Sunday, and a backup that
# fails quietly is indistinguishable from one that never ran.
if [[ -f /etc/wpa-signal-backup.env ]]; then
  systemctl enable --now wpa-signal-backup.timer
else
  echo "no /etc/wpa-signal-backup.env — backup timer installed but NOT enabled (runbook 03 section 3)" >&2
fi

echo
echo "installed. check with:"
echo "  systemctl status signal-cli wpa-gate --no-pager"
echo "  systemctl list-timers wpa-signal-backup.timer"
echo "  sudo ls -l /run/wpa-signal/socket        # srwxrwx--- openclaw wpa-signal"
echo "  sudo nft list table inet wpa_signal     # 8081 open to uid 0 and 991 only"
echo "  journalctl -u wpa-gate -n 20            # decisions and counts, never bodies"
echo "  sudo tail /var/lib/wpa-gate/commands.jsonl"
