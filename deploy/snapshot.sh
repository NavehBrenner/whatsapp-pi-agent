#!/usr/bin/env bash
# The privileged half of ADR 0006, and deliberately the whole of it: copy four
# files, hand them to the reader's user, exit. Nothing here parses message
# content — the process that reads untrusted text is never the process with the
# privilege.
#
# Run by wpa-snapshot.service as root, because Waydroid's app-private directory
# is mode drwxrwx--x owned by uid 10125 and nothing else can traverse it.
set -euo pipefail

src=${1:?source databases dir}
dest=${2:?snapshot dir}
owner=${3:?owning user}

# Must be tmpfs: this runs every 30s and /tmp on Raspberry Pi OS is the SD card.
install -d -m 0700 -o "$owner" -g "$owner" "$dest"

# A -wal left over from a previous poll would be applied to a newer .db and
# silently return wrong rows, so clear before copying rather than after.
# The reader's own read-only open of wa.db leaves a -wal/-shm behind too, so
# clear those as well or the next poll inherits them.
rm -f "$dest"/msgstore.db "$dest"/msgstore.db-wal "$dest"/msgstore.db-shm \
      "$dest"/wa.db "$dest"/wa.db-wal "$dest"/wa.db-shm

# Explicit names, no glob: as a non-root user msgstore.db* silently expands to
# nothing (the directory is traversable but not listable), and that failure mode
# looks exactly like "no new messages".
# wa.db gets its -wal/-shm too: contact and LID-name edits land in the WAL, so
# the .db on its own is as stale as msgstore.db would be (ADR 0003).
for f in msgstore.db msgstore.db-wal msgstore.db-shm wa.db wa.db-wal wa.db-shm; do
  if [[ -f "$src/$f" ]]; then
    install -m 0600 -o "$owner" -g "$owner" "$src/$f" "$dest/$f"
  fi
done

# The reader needs the WAL and its -shm together; the .db alone is stale (ADR 0003).
[[ -f "$dest/msgstore.db" ]] || { echo "no msgstore.db under $src" >&2; exit 1; }
