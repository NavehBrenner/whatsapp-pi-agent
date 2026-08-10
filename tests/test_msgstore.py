"""Tests for the reader, against a synthetic msgstore with the real schema shape.

The fixture deliberately reproduces the two things that bit us on hardware:
a late-delivered message inserted out of timestamp order, and a group sender
identified only by @lid.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from reader.msgstore import (
    Message,
    _truncate,
    load_chats,
    load_cursor,
    read_since,
    save_cursor,
    snapshot,
)

SCHEMA = """
CREATE TABLE message(
  _id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_row_id INTEGER NOT NULL,
  from_me INTEGER NOT NULL,
  key_id TEXT NOT NULL,
  sender_jid_row_id INTEGER,
  timestamp INTEGER,
  text_data TEXT);
CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
CREATE TABLE jid(_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
CREATE TABLE lid_display_name(lid_row_id INTEGER, display_name TEXT, username TEXT);
"""


def _build(tmp_path: Path) -> Path:
    db = tmp_path / "msgstore.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO jid(_id,user,server,raw_string) VALUES (?,?,?,?)",
        [
            (1, "1234567890-1", "g.us", "1234567890-1@g.us"),
            (2, "972500000001", "s.whatsapp.net", "972500000001@s.whatsapp.net"),
            (3, "249808233197636", "lid", "249808233197636@lid"),
        ],
    )
    conn.execute("INSERT INTO chat(_id,jid_row_id,subject) VALUES (1,1,'Rideshare')")
    conn.execute("INSERT INTO lid_display_name(lid_row_id,display_name) VALUES (3,'Dana')")
    conn.executemany(
        "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,text_data)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            (1, 1, 0, "k1", 2, 1_700_000_100_000, "first, delivered promptly"),
            # The trap: higher _id, EARLIER timestamp (late companion delivery).
            (2, 1, 0, "k2", 3, 1_700_000_050_000, "late arrival from a lid sender"),
            (3, 1, 1, "k3", None, 1_700_000_200_000, "something I sent"),
            (4, 1, 0, "k4", 2, 1_700_000_300_000, None),  # non-text: must be skipped
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_reads_all_from_cold_cursor(tmp_path: Path) -> None:
    db = _build(tmp_path)
    msgs = read_since(db, 0)
    assert [m.id for m in msgs] == [1, 2, 3], "non-text row must be excluded"


def test_late_message_is_not_skipped(tmp_path: Path) -> None:
    """The whole point of the _id cursor: _id=2 has an older timestamp than _id=1."""
    db = _build(tmp_path)
    by_id = {m.id: m for m in read_since(db, 0)}
    assert by_id[2].timestamp < by_id[1].timestamp, "fixture must model late delivery"

    # Having processed _id=1, the late row must still come back.
    assert [m.id for m in read_since(db, 1)] == [2, 3]


def test_lid_sender_resolves_via_lid_display_name(tmp_path: Path) -> None:
    db = _build(tmp_path)
    msgs = read_since(db, 1)
    assert msgs[0].sender == "Dana"


def test_unresolvable_sender_falls_back_to_jid_user(tmp_path: Path) -> None:
    db = _build(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM lid_display_name")
    conn.commit()
    conn.close()
    msgs = read_since(db, 1)
    assert msgs[0].sender == "249808233197636", "must degrade to the LID, not crash"


def test_own_messages_are_labelled_me(tmp_path: Path) -> None:
    db = _build(tmp_path)
    msgs = read_since(db, 2)
    assert msgs[0].sender == "me"


def test_group_subject_used_as_chat_name(tmp_path: Path) -> None:
    db = _build(tmp_path)
    assert read_since(db, 0)[0].chat == "Rideshare"


def test_chat_filter_keys_on_jid_not_subject(tmp_path: Path) -> None:
    """The allowlist is JIDs. Group subjects are attacker-settable — anyone in a
    group can rename it, so a name-keyed allowlist can be talked into."""
    db = _build(tmp_path)
    assert read_since(db, 0, chats=frozenset({"1234567890-1@g.us"})) != []
    assert read_since(db, 0, chats=frozenset({"other@g.us"})) == []
    assert read_since(db, 0, chats=frozenset({"Rideshare"})) == [], "subject must not match"


def test_allowlist_does_not_stall_the_cursor(tmp_path: Path) -> None:
    """The filter runs in SQL so the cursor advances past non-allowlisted rows.

    Filtered in Python instead, a batch of purely non-allowlisted messages comes
    back empty, the caller has no id to advance to, and the reader re-reads the
    same rows every 30s forever.
    """
    db = _build(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO jid(_id,user,server,raw_string) VALUES (4,'other','g.us','other@g.us')")
    conn.execute("INSERT INTO chat(_id,jid_row_id,subject) VALUES (2,4,'Other')")
    # Noise in the allowlisted chat's id range, then one real message above it.
    conn.executemany(
        "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,text_data)"
        " VALUES (?,?,?,?,?,?,?)",
        [(_id, 2, 0, f"n{_id}", 2, 1_700_000_400_000, "noise") for _id in range(5, 10)]
        + [(10, 1, 0, "k10", 2, 1_700_000_500_000, "the one we want")],
    )
    conn.commit()
    conn.close()

    allowed = frozenset({"1234567890-1@g.us"})
    batch = read_since(db, 0, chats=allowed, limit=2)
    cursor = batch[-1].id
    assert read_since(db, cursor, chats=allowed, limit=2)[-1].id == 10, "must reach past the noise"


def test_empty_allowlist_reads_nothing(tmp_path: Path) -> None:
    db = _build(tmp_path)
    assert read_since(db, 0, chats=frozenset()) == []
    assert read_since(db, 0, chats=None) != [], "None still means every chat"


def test_load_chats(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[whatsapp]\nchats = ["1234567890-1@g.us"]\n')
    assert load_chats(cfg) == frozenset({"1234567890-1@g.us"})

    cfg.write_text("[whatsapp]\n")
    assert load_chats(cfg) == frozenset(), "no chats key means read nothing"

    try:
        load_chats(tmp_path / "absent.toml")
    except FileNotFoundError:
        return
    raise AssertionError("a missing config must fail, not silently read everything")


def test_limit_is_respected(tmp_path: Path) -> None:
    db = _build(tmp_path)
    assert len(read_since(db, 0, limit=1)) == 1


def test_excerpt_is_truncated_and_flattened() -> None:
    assert _truncate("a\nb  c") == "a b c"
    long = "x" * 500
    out = _truncate(long, limit=10)
    assert len(out) == 10 and out.endswith("…")


def test_message_is_json_safe(tmp_path: Path) -> None:
    """Only the fixed schema crosses the boundary — no surprise fields."""
    db = _build(tmp_path)
    msg = read_since(db, 0)[0]
    assert isinstance(msg, Message)
    assert set(vars(msg)) == {"id", "chat", "sender", "timestamp", "excerpt"}


def test_cursor_roundtrip_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cursor"
    assert load_cursor(path) == 0, "missing cursor starts from zero, not a crash"
    save_cursor(path, 42)
    assert load_cursor(path) == 42
    path.write_text("garbage")
    assert load_cursor(path) == 0, "corrupt cursor must not raise"


def test_snapshot_copies_wal_parts_together(tmp_path: Path) -> None:
    src = tmp_path / "dbs"
    src.mkdir()
    _build(src)
    (src / "msgstore.db-wal").write_bytes(b"wal")
    (src / "msgstore.db-shm").write_bytes(b"shm")
    (src / "wa.db").write_bytes(b"")
    dest = tmp_path / "snap"
    out = snapshot(src, dest)
    assert out.is_file()
    assert (dest / "msgstore.db-wal").read_bytes() == b"wal", "WAL must be copied"
    assert (dest / "msgstore.db-shm").read_bytes() == b"shm"


def test_snapshot_is_a_noop_when_src_is_dest(tmp_path: Path) -> None:
    """Under systemd the privileged unit has already copied; copying a directory
    onto itself would raise SameFileError."""
    src = tmp_path / "dbs"
    src.mkdir()
    _build(src)
    (src / "msgstore.db-wal").write_bytes(b"wal")
    out = snapshot(src, tmp_path / "dbs")
    assert out == src / "msgstore.db"
    assert (src / "msgstore.db-wal").read_bytes() == b"wal"


def test_snapshot_rejects_missing_db(tmp_path: Path) -> None:
    try:
        snapshot(tmp_path, tmp_path / "out")
    except FileNotFoundError:
        return
    raise AssertionError("snapshot must fail loudly when msgstore.db is absent")
