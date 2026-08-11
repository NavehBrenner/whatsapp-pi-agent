"""Notice when messages stop arriving, as opposed to when services stop running.

The failure this exists for is silent. After the 2026-08-10 reboot, Waydroid
froze the container and WhatsApp received nothing for 1h40m while every health
signal looked fine: both waydroid units active, `com.whatsapp` in the process
list, `wpa-reader.timer` firing every 30s and correctly reporting no new
messages. The only thing that changed was that `max(_id)` stopped moving.

So this checks the one fact none of those cover — **are new rows landing in
msgstore** — and deliberately does not check whether anything is "running".
Container state was the misleading signal; a frozen container that stops
receiving is caught here because `max(_id)` stalls, and a check that asked
`waydroid status` would have said RUNNING throughout.

Three properties are load-bearing:

1. The signal is `max(_id)` over **every** chat, not the reader's cursor. The
   cursor only advances for allowlisted chats, so a single quiet group would
   report hours of silence every night.
2. `_id`, never `timestamp` — same reason as the cursor (ADR 0003). Backfill
   inserts years-old rows, so `max(timestamp)` can sit still while rows arrive.
3. It runs as its own unit rather than inside the reader. A liveness check
   hosted by the thing it checks dies with it; this one fires whether the
   container froze, the reader's timer stopped, or the snapshot went stale.

Run by hand:

    python3 -m reader.staleness [snap_dir] [marker] [max_quiet_hours]
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from reader.msgstore import DEFAULT_SNAPSHOT_DIR, MSGSTORE_PARTS

# Beside the cursor, in the reader's StateDirectory.
DEFAULT_MARKER = Path("/var/lib/wpa-reader/liveness")
MESSAGES_JSONL = Path("/var/lib/wpa-reader/messages.jsonl")

# Generous on purpose. The freeze it guards against persists until a human
# intervenes, so a late alert costs nothing while a nightly false one trains
# everybody to ignore the real one. A whole account going 6h without a single
# message is already unusual; tune with the argument in the unit file once the
# soak shows what a genuinely quiet night looks like.
DEFAULT_MAX_QUIET_HOURS = 6.0

# systemd reads a <N> prefix on a line as its syslog priority, so the alert
# lands under `journalctl -p err` rather than buried at info with the rest.
ERR, WARNING = "<3>", "<4>"


def max_message_id(msgstore: Path) -> int | None:
    """Highest `_id` in the snapshot, or None if it could not be read.

    None is distinct from 0 and the caller must not treat it as staleness: the
    snapshot is rewritten every 30s by wpa-snapshot.service, so an hourly check
    will occasionally open it mid-copy. Missing a beat is fine, and the next run
    settles it; concluding "no messages" from a half-copied file is not.
    """
    try:
        conn = sqlite3.connect(f"file:{msgstore}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT max(_id) FROM message").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return 0 if row is None or row[0] is None else int(row[0])


def quiet_seconds(marker: Path, max_id: int, now: float) -> float:
    """Record `max_id` and return how long it has been sitting still.

    The marker's *content* is the last id seen; its *mtime* is when that id last
    changed. That only works if the file is left alone when nothing has moved —
    rewriting it on every check would keep the mtime perpetually fresh and the
    alert could never fire. Hence the conditional write.

    An unreadable or missing marker re-baselines rather than alerting: the first
    run after an install has nothing to compare against, and inventing silence
    from a fresh file would page someone about a working system.
    """
    try:
        previous: int | None = int(marker.read_text().strip())
        since = now - marker.stat().st_mtime
    except (OSError, ValueError):
        previous, since = None, 0.0

    if previous == max_id:
        # A clock stepped backwards would make this negative; report no silence
        # rather than a nonsense duration, and let the next run measure it.
        return max(since, 0.0)

    tmp = marker.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(f"{max_id}\n")
    tmp.replace(marker)
    return 0.0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _mem_available_kb() -> int:
    """MemAvailable from /proc, readable even under ProtectSystem=strict.

    Worth a data point during the soak: signal-cli's JVM has to fit alongside an
    unfrozen container in M3, and this is the cheapest way to find out whether
    it will.
    """
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    snap_dir = Path(args[0]) if args else DEFAULT_SNAPSHOT_DIR
    marker = Path(args[1]) if len(args) > 1 else DEFAULT_MARKER
    max_quiet_hours = float(args[2]) if len(args) > 2 else DEFAULT_MAX_QUIET_HOURS

    max_id = max_message_id(snap_dir / MSGSTORE_PARTS[0])
    if max_id is None:
        print(f"{WARNING}snapshot unreadable, not counting it as silence", file=sys.stderr)
        return 0

    hours = quiet_seconds(marker, max_id, time.time()) / 3600

    # One line per run, and it is the whole soak record: ids only, never content
    # (threat-model R4). `journalctl -u wpa-staleness` after 24h shows whether
    # max_id kept climbing, whether messages.jsonl is growing without bound
    # (nothing rotates it until the M3 consumer drains it), and what memory
    # headroom there is with the container unfrozen.
    print(
        f"max_id={max_id} quiet={hours:.1f}h "
        f"jsonl_bytes={_size(MESSAGES_JSONL)} mem_available_kb={_mem_available_kb()}"
    )

    if hours > max_quiet_hours:
        print(
            f"{ERR}no new WhatsApp message in {hours:.1f}h — max(_id) stuck at {max_id}. "
            "Services being active does not mean messages are arriving: check "
            "`waydroid status` for a FROZEN container (persist.waydroid.suspend).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
