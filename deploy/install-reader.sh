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

# config.toml holds the authority table — which chats are read, which conversations
# may command, and as whom. It is not a secret file, but it is the answer to "what is
# this thing allowed to do", so it is not world-readable either: only the two services
# that need it. `wpa-config` exists because both of them need it and they are
# deliberately different users (ADR 0006).
groupadd -f --system wpa-config
for u in wpa-gate wpa-reader; do
  id "$u" >/dev/null 2>&1 && usermod -aG wpa-config "$u"
done

# config.toml is gitignored, so a fresh install has none. The reader refuses to
# start without one rather than defaulting to "read every chat".
if [[ ! -f /opt/wpa/config/config.toml ]]; then
  install -m 0640 -o root -g wpa-config /opt/wpa/config/example.config.toml /opt/wpa/config/config.toml
  echo "wrote /opt/wpa/config/config.toml from the example — chats = [] reads NOTHING until you fill it in"
else
  # An existing file predates this rule; tighten it in place.
  chown root:wpa-config /opt/wpa/config/config.toml
  chmod 0640 /opt/wpa/config/config.toml
fi

# The gitignored config above is the only copy of the authority table, so it gets a
# backup that fires when it changes. Installed here rather than left to a hand-run
# command: a safety net nobody remembers to deploy is not one.
install -m 0755 "$repo"/deploy/backup-gate-config.sh /usr/local/bin/wpa-config-backup

# *.path as well as *.service and *.timer — a path unit missing from this glob installs
# nothing, reports nothing, and the backup silently never runs.
install -m 0644 "$repo"/deploy/systemd/*.service "$repo"/deploy/systemd/*.timer \
  "$repo"/deploy/systemd/*.path /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wpa-reader.timer
systemctl enable --now wpa-staleness.timer
systemctl enable --now wpa-config-backup.path
# Take one immediately: on a box that already has a live config, the path unit does not
# fire until the next edit, so without this the first backup could be months away.
systemctl start wpa-config-backup.service

echo
echo "installed. check with:"
echo "  systemctl list-timers 'wpa-*.timer'"
echo "  systemctl start wpa-reader.service && journalctl -u wpa-reader -n 20"
echo "  wc -l /var/lib/wpa-reader/messages.jsonl"
echo "  journalctl -u wpa-staleness -n 20      # one status line per hour"
echo "  journalctl -p err -u wpa-staleness     # the alert, if messages stopped"
