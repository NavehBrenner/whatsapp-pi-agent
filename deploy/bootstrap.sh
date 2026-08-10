#!/usr/bin/env bash
# Prereqs for the spike (docs/runbooks/02-waydroid-whatsapp.md). Idempotent.
# Does NOT init Waydroid or install WhatsApp — those are manual, by design:
# the spike is a decision point, not a script.
set -euo pipefail

[[ "$(uname -m)" == "aarch64" ]] || { echo "need a 64-bit OS (got $(uname -m))"; exit 1; }

sudo apt-get update
sudo apt-get install -y curl ca-certificates git sqlite3 python3 python3-venv rsync

if ! command -v waydroid >/dev/null; then
  curl -s https://repo.waydro.id | sudo bash
  sudo apt-get install -y waydroid
fi

# Reader state (cursor, message spool) is created by systemd StateDirectory=,
# and the snapshot lives on tmpfs — nothing to create here.

cat <<'EOF'

done. next, by hand:
  sudo waydroid init -s VANILLA      # VANILLA first — GAPPS drags in Play Integrity
  sudo systemctl enable --now waydroid-container
  waydroid session start & waydroid show-full-ui

then docs/runbooks/02-waydroid-whatsapp.md from step 2.
EOF
