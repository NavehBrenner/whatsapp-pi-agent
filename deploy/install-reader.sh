#!/usr/bin/env bash
# Install the reader as a systemd timer on the Pi. Idempotent; run from a
# checkout on the Pi itself:  sudo deploy/install-reader.sh
#
# The code lives in /opt/wpa and not in a home directory, because the reader
# runs with ProtectHome=yes and cannot see /home at all.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)

id wpa-reader >/dev/null 2>&1 ||
  useradd --system --no-create-home --shell /usr/sbin/nologin wpa-reader

install -d -m 0700 -o wpa-reader -g wpa-reader /var/lib/wpa-reader

install -d -m 0755 /opt/wpa
# config.toml is gitignored, so it is not in the source tree — without excluding
# it, --delete removes the live allowlist on every deploy and the block below
# silently puts back an empty one, i.e. the reader stops reading anything.
rsync -a --delete --exclude .git --exclude .venv --exclude '__pycache__' \
  --exclude config/config.toml "$repo/" /opt/wpa/
chmod 0755 /opt/wpa/deploy/snapshot.sh

# config.toml is gitignored, so a fresh install has none. The reader refuses to
# start without one rather than defaulting to "read every chat".
if [[ ! -f /opt/wpa/config/config.toml ]]; then
  install -m 0644 /opt/wpa/config/example.config.toml /opt/wpa/config/config.toml
  echo "wrote /opt/wpa/config/config.toml from the example — chats = [] reads NOTHING until you fill it in"
fi

install -m 0644 "$repo"/deploy/systemd/*.service "$repo"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wpa-reader.timer

echo
echo "installed. check with:"
echo "  systemctl list-timers wpa-reader.timer"
echo "  systemctl start wpa-reader.service && journalctl -u wpa-reader -n 20"
echo "  wc -l /var/lib/wpa-reader/messages.jsonl"
