#!/bin/sh
# Put one message into an agent's outbox, so the gate delivers it to that agent's
# conversation. Wired as OnFailure= on the timers that would otherwise fail quietly.
#
#   wpa-outbox-notify <agent> <text>
#
# Generalised from the NVB-27-era oc-auth notifier once there were two callers. Runs
# as root because the outbox is 0700 wpa-gate (ADR 0009: one directory per agent,
# gate-owned). The entry format is the gate's — {"to": ..., "text": ...} in a *.json
# file — and "self" resolves to the conversation that agent serves.
set -eu

agent=${1:?usage: wpa-outbox-notify <agent> <text>}
text=${2:?usage: wpa-outbox-notify <agent> <text>}

out="/var/lib/wpa-gate/outbox/$agent"
# No outbox means no gate to deliver it, and failing here would only mask the real
# failure that triggered us.
[ -d "$out" ] || { echo "no outbox for agent $agent" >&2; exit 0; }

name="notify-$(date +%s)-$$.json"

# Staged under a dotted name and renamed into place: the gate's scan skips entries
# beginning with ".", so it can never read a half-written file. It also opens them
# O_NOFOLLOW and rejects anything that is not a regular file, so write it plainly.
jq -n --arg text "$text" '{to: "self", text: $text}' > "$out/.$name"
chown wpa-gate:wpa-gate "$out/.$name"
chmod 600 "$out/.$name"
mv "$out/.$name" "$out/$name"
