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
systemctl daemon-reload
systemctl enable --now signal-cli.service

echo
echo "installed. check with:"
echo "  systemctl status signal-cli --no-pager"
echo "  sudo ls -l /run/wpa-signal/socket"
