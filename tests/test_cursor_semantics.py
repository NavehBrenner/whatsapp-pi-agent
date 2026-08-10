"""The cursor must key on `_id`, never on `timestamp`.

Found on live hardware 2026-08-10: companion devices receive messages long after
they were sent (808 of 812 sampled messages arrived >60s late; worst case 823s),
and history backfill inserts messages years old. So rows are NOT inserted in
timestamp order, and a timestamp-based cursor silently drops late arrivals.

See docs/decisions/0003-local-db-read.md. These tests exist so that regression is
a CI failure rather than an intermittent "the assistant missed a message" bug
that costs days to trace.
"""

from __future__ import annotations

import sqlite3

# (_id, timestamp_ms, text) — mirrors the real msgstore shape closely enough.
# Row 2 is the trap: inserted last, but stamped EARLIER than row 1.
ROWS: list[tuple[int, int, str]] = [
    (1, 1_700_000_100_000, "sent at t=100, delivered promptly"),
    (2, 1_700_000_050_000, "sent at t=50, delivered LATE (arrives after row 1)"),
]


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE message("
        "_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "timestamp INTEGER NOT NULL,"
        "text_data TEXT)"
    )
    conn.executemany("INSERT INTO message(_id, timestamp, text_data) VALUES (?,?,?)", ROWS)
    conn.commit()
    return conn


def _fetch_after_id(conn: sqlite3.Connection, cursor_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT _id FROM message WHERE _id > ? ORDER BY _id", (cursor_id,)
    ).fetchall()
    return [int(r[0]) for r in rows]


def _fetch_after_timestamp(conn: sqlite3.Connection, cursor_ts: int) -> list[int]:
    rows = conn.execute(
        "SELECT _id FROM message WHERE timestamp > ? ORDER BY timestamp", (cursor_ts,)
    ).fetchall()
    return [int(r[0]) for r in rows]


def test_id_cursor_sees_every_message() -> None:
    """An `_id` cursor picks up the late-delivered message. This is the contract."""
    conn = _make_db()
    try:
        first = _fetch_after_id(conn, 0)
        assert first == [1, 2], f"expected both rows from a cold cursor, got {first}"

        # Process row 1 only, then advance the cursor as the reader would.
        seen = _fetch_after_id(conn, 1)
        assert seen == [2], f"late message must still be found by _id cursor, got {seen}"
    finally:
        conn.close()


def test_timestamp_cursor_silently_drops_late_messages() -> None:
    """Documents WHY we don't use timestamps: the late message vanishes.

    If this ever starts failing, WhatsApp changed its delivery semantics and the
    reasoning in ADR 0003 should be re-checked — don't just delete the test.
    """
    conn = _make_db()
    try:
        # Reader processes row 1 and records its timestamp as the high-water mark.
        cursor_ts = ROWS[0][1]
        missed = _fetch_after_timestamp(conn, cursor_ts)
        assert missed == [], (
            "expected the timestamp cursor to MISS the late row — if it found "
            f"something ({missed}), the test fixture no longer models the bug"
        )
    finally:
        conn.close()


def test_autoincrement_does_not_reuse_ids_after_delete() -> None:
    """`_id` is AUTOINCREMENT in the real schema, so deletes can't cause id reuse.

    Verified against live msgstore: `CREATE TABLE message(_id INTEGER PRIMARY KEY
    AUTOINCREMENT, ...)` with sqlite_sequence tracking it. Plain INTEGER PRIMARY
    KEY would reuse the max rowid after a delete, handing a NEW message an id at
    or below our cursor — which would drop it. WhatsApp does delete messages
    (disappearing messages, delete-for-everyone), so this matters.
    """
    conn = _make_db()
    try:
        conn.execute("DELETE FROM message WHERE _id = 2")
        conn.commit()
        conn.execute(
            "INSERT INTO message(timestamp, text_data) VALUES (?,?)",
            (1_700_000_200_000, "inserted after a delete"),
        )
        conn.commit()
        new_id_row = conn.execute("SELECT MAX(_id) FROM message").fetchone()
        new_id = int(new_id_row[0])
        assert new_id > 2, f"AUTOINCREMENT must not reuse id 2, got {new_id}"
    finally:
        conn.close()
