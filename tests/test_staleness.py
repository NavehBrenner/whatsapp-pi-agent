"""Tests for the staleness check.

The one thing that must not break: the marker's mtime measures when `max(_id)`
last *changed*, not when it was last *looked at*. Get that wrong — rewrite the
file on every run — and the check reports zero silence forever, which is exactly
the silent failure it was built to catch.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from reader.staleness import main, max_message_id, quiet_seconds


def _snapshot(tmp_path: Path, top_id: int) -> Path:
    """A snapshot dir holding a msgstore whose highest _id is `top_id`."""
    snap = tmp_path / "snap"
    snap.mkdir(exist_ok=True)
    conn = sqlite3.connect(snap / "msgstore.db")
    conn.execute("CREATE TABLE IF NOT EXISTS message(_id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO message(_id) VALUES (?)", (top_id,))
    conn.commit()
    conn.close()
    return snap


def _backdate(path: Path, hours: float) -> None:
    when = time.time() - hours * 3600
    os.utime(path, (when, when))


def test_quiet_time_measures_the_last_change_not_the_last_check(tmp_path: Path) -> None:
    marker = tmp_path / "liveness"
    now = time.time()

    assert quiet_seconds(marker, 45280, now) == 0.0, "a fresh marker is not silence"

    # Same id, three hours later. If the previous call had rewritten the marker
    # unconditionally this would read as 0 and the alert could never fire.
    _backdate(marker, hours=3)
    assert quiet_seconds(marker, 45280, now) > 3 * 3600 - 5

    # A moved id resets the clock and is written down.
    assert quiet_seconds(marker, 45281, now) == 0.0
    assert marker.read_text().strip() == "45281"


def test_alert_fires_only_once_the_ids_stop_moving(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, 45280)
    marker = tmp_path / "liveness"
    argv = [str(snap), str(marker), "6"]

    assert main(argv) == 0, "first run baselines, it does not alert"

    _backdate(marker, hours=7)
    assert main(argv) == 1, "same max(_id) seven hours on is the failure being caught"

    _snapshot(tmp_path, 45281)  # a message lands
    assert main(argv) == 0


def test_unreadable_snapshot_is_not_reported_as_silence(tmp_path: Path) -> None:
    """wpa-snapshot rewrites the snapshot every 30s, so an hourly check will
    eventually open one mid-copy. That must not alert, and must not re-baseline
    away a real stall."""
    snap = _snapshot(tmp_path, 45280)
    marker = tmp_path / "liveness"
    argv = [str(snap), str(marker), "6"]
    assert main(argv) == 0

    (snap / "msgstore.db").write_bytes(b"this is not a database")
    _backdate(marker, hours=7)
    assert max_message_id(snap / "msgstore.db") is None
    assert main(argv) == 0, "a torn read is not evidence of anything"
    assert marker.read_text().strip() == "45280", "the baseline must survive"


def test_missing_snapshot_is_not_silence(tmp_path: Path) -> None:
    assert max_message_id(tmp_path / "absent.db") is None
