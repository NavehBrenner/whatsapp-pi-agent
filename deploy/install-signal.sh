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

install -d -m 0700 -o wpa-signal -g wpa-signal /var/lib/wpa-signal

# Kept out of the repo: the repo is public and this is a working phone number.
# Root-owned, readable by the daemon's user and nobody else.
install -m 0640 -o root -g wpa-signal /dev/null /etc/wpa-signal.env
printf 'ASSISTANT_NUMBER=%s\n' "$number" > /etc/wpa-signal.env

# Account state may have been created by a `signal-cli register` run as a human
# user. Move it under the daemon's user rather than leaving the assistant's
# identity in somebody's home directory.
for home in /home/*; do
  src="$home/.local/share/signal-cli"
  if [[ -d "$src/data" && ! -d /var/lib/wpa-signal/data ]]; then
    echo "migrating account state from $src"
    cp -a "$src/." /var/lib/wpa-signal/
    chown -R wpa-signal:wpa-signal /var/lib/wpa-signal
  fi
done

if [[ ! -d /var/lib/wpa-signal/data ]]; then
  echo "no registered account under /var/lib/wpa-signal — register first (runbook 03)" >&2
  exit 1
fi

install -m 0644 "$repo/deploy/systemd/signal-cli.service" /etc/systemd/system/

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
echo "  sudo ls -l /run/wpa-signal/socket        # srwxr-x--- wpa-signal wpa-signal"
echo "  journalctl -u wpa-gate -n 20            # decisions and counts, never bodies"
echo "  sudo tail /var/lib/wpa-gate/commands.jsonl"
