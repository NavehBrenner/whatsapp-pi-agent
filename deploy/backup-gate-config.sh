#!/usr/bin/env bash
# Keep dated copies of the gate's live config outside /opt/wpa.
#
# WHY THIS EXISTS. `config/config.toml` is gitignored — it names real people, real
# group ids and the profile each pair gets — so the deploy directory holds the ONLY
# copy. On 2026-08-17 an `rsync -a --delete` from an incomplete staging directory
# emptied /opt/wpa and took it with it. Nothing failed at the time: the gate had it in
# memory and kept running, so the loss was invisible until the next restart, which
# would have been a reboot at an hour nobody chose. It was recovered only because an
# unrelated OpenClaw snapshot from the night before happened to contain a copy.
#
# WHY ON CHANGE AND NOT ON A TIMER. Weekly would not have helped: the config had been
# edited the previous day. A path unit fires when the file is written, so what is kept
# is exactly the set of versions that ever went live.
#
# ponytail: local copies only. This defends against the failure that actually
# happened — deletion — and not against the SD card dying, which takes /opt/wpa and
# /var/backups together. That one is the pending SSD migration's problem, and the
# weekly age-encrypted offsite blob in backup-signal.sh is the model to copy if this
# ever needs to survive the disk.
set -euo pipefail

src=/opt/wpa/config/config.toml
dir=/var/backups/wpa-config
keep=20

# Fired by a path unit, which also triggers on the file being REMOVED. Nothing to copy
# then, and the point of the exercise is that the older copies are still sitting here.
[ -f "$src" ] || { echo "no $src to back up" >&2; exit 0; }

install -d -m 0700 -o root -g root "$dir"

# Only when it actually differs from the newest copy. A path unit fires on every write
# and an editor can write several times per save, which would otherwise churn through
# the retention window and leave twenty copies of one afternoon.
latest=$(ls -1t "$dir"/config-*.toml 2>/dev/null | head -1 || true)
if [ -n "$latest" ] && cmp -s "$latest" "$src"; then
  exit 0
fi

install -m 0600 -o root -g root "$src" "$dir/config-$(date +%F-%H%M%S).toml"

# Newest kept, oldest dropped. Twenty covers months at the rate this file changes.
ls -1t "$dir"/config-*.toml | tail -n +$((keep + 1)) | xargs -r rm --

echo "backed up $src ($(ls -1 "$dir"/config-*.toml | wc -l) copies kept)"
