#!/usr/bin/env bash
# Encrypted backup of /var/lib/wpa-signal — the directory that IS the Signal
# account. Run weekly by wpa-signal-backup.timer, or by hand for the first one:
#
#   sudo systemctl start wpa-signal-backup.service
#
# Encrypts to an age recipient whose private key is NOT on this machine (it is
# in the password manager). The Pi can therefore write backups and cannot read
# them back — which is the point: a blob encrypted with a key stored beside it
# protects against disk loss and nothing else.
#
# Restoring is a human step on the machine that holds the private key.
# See docs/runbooks/03-signal-cli.md section 3.
set -euo pipefail

# From /etc/wpa-signal-backup.env (root-owned, 0600). Kept out of the repo: the
# repo is public and RCLONE_CONFIG_WPABACKUP_KEY is a live bucket credential.
: "${AGE_RECIPIENT:?set in /etc/wpa-signal-backup.env}"
: "${BACKUP_REMOTE:?set in /etc/wpa-signal-backup.env}"

src=/var/lib/wpa-signal
dir=/var/backups/wpa-signal
out="$dir/wpa-signal-$(date +%F).tar.gz.age"
keep=4

install -d -m 0700 -o root -g root "$dir"

# The daemon holds the account lock and rewrites session state as messages
# arrive, so a copy taken while it runs is not guaranteed coherent. Stop it.
# Costs ~30s of control channel downtime once a week — signal-cli needs ~19s to
# come back to a listening socket, and messages sent meanwhile are delivered on
# reconnect, not lost.
restart=no
if systemctl is-active --quiet signal-cli; then
  restart=yes
  systemctl stop signal-cli
fi
resume() { [[ $restart == yes ]] && systemctl start signal-cli; }
trap resume EXIT

# Write to .tmp and rename, so an interrupted run never leaves a truncated blob
# wearing today's name. Nothing is ever overwritten either — the date is in the
# filename — so a bad backup can't destroy the previous good one.
tar czf - -C "$(dirname "$src")" "$(basename "$src")" | age -r "$AGE_RECIPIENT" -o "$out.tmp"
mv "$out.tmp" "$out"
chmod 0600 "$out"

resume
trap - EXIT

# The one check available here: the private key is elsewhere, so this script
# cannot verify the blob decrypts. That is what the restore drill is for.
# Size is the crude proxy — a truncated tar still encrypts happily.
size=$(stat -c%s "$out")
[[ $size -gt 500000 ]] || { echo "backup is only ${size}B — refusing to upload" >&2; exit 1; }

rclone --config "" copyto "$out" "$BACKUP_REMOTE/$(basename "$out")"

# ponytail: local retention only. 1.6MB a week against a 10GB free tier means
# the remote can hold a century of these; prune it when that stops being true.
ls -1t "$dir"/wpa-signal-*.tar.gz.age | tail -n +$((keep + 1)) | xargs -r rm --

echo "backed up ${size}B to $BACKUP_REMOTE/$(basename "$out")"
