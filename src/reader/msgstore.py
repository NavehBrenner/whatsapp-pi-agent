"""Read new WhatsApp messages out of Waydroid's live msgstore.db.

This is the UNPRIVILEGED half of the system (ADR 0006): it sees untrusted message
content and therefore has no tools, no network, and no credentials. It emits
schema-fixed JSON on stdout and nothing else. Deterministic host code downstream
does the formatting — the model's prose is never the transport.

Two things here are load-bearing and were paid for with hardware debugging
(see docs/decisions/0003-local-db-read.md):

1. The cursor is `_id`, never `timestamp`. Companion devices deliver messages
   late and backfill inserts years-old rows, so rows are NOT in timestamp order.
2. The snapshot copies msgstore.db together with its -wal and -shm. Copying only
   the .db silently returns stale data.

Needs root to read Waydroid's app-private directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Waydroid bind-mounts Android's /data from the session user's home, NOT from
# /var/lib/waydroid (verified on hardware 2026-08-10).
DEFAULT_DB_DIR = Path.home() / ".local/share/waydroid/data/data/com.whatsapp/databases"

# Copied as a set — the WAL holds recent messages that aren't in the .db yet.
MSGSTORE_PARTS = ("msgstore.db", "msgstore.db-wal", "msgstore.db-shm")
CONTACTS_DB = "wa.db"

EXCERPT_MAX = 200
DEFAULT_BATCH = 500

# Real tmpfs. NOT /tmp — on Raspberry Pi OS that is on the SD card.
DEFAULT_SNAPSHOT_DIR = Path("/dev/shm/wpa-snapshot")
DEFAULT_CURSOR_PATH = Path("/var/lib/wpa/cursor/reader")

# Sender names come from three places, in descending quality. Group participants
# are mostly @lid and resolve poorly (~5% coverage) — see OPEN-QUESTIONS Q5.
_QUERY = """
SELECT m._id,
       COALESCE(NULLIF(c.subject, ''), cj.user, '?')                    AS chat,
       COALESCE(NULLIF(ldn.display_name, ''),
                NULLIF(w.wa_name, ''),
                NULLIF(w.display_name, ''),
                sj.user)                                                AS sender,
       m.timestamp                                                      AS ts,
       m.text_data                                                      AS body,
       m.from_me                                                        AS from_me
FROM message m
JOIN chat c              ON c._id  = m.chat_row_id
LEFT JOIN jid cj         ON cj._id = c.jid_row_id
LEFT JOIN jid sj         ON sj._id = m.sender_jid_row_id
LEFT JOIN lid_display_name ldn ON ldn.lid_row_id = sj._id
LEFT JOIN wa.wa_contacts w     ON w.jid = sj.raw_string
WHERE m._id > ?
  AND m.text_data IS NOT NULL
ORDER BY m._id
LIMIT ?
"""


@dataclass(frozen=True)
class Message:
    """The only shape that crosses the trust boundary."""

    id: int
    chat: str
    sender: str
    timestamp: str
    excerpt: str


def snapshot(db_dir: Path, dest: Path) -> Path:
    """Copy the databases somewhere readable and stable.

    Destination must be tmpfs, or every poll rewrites tens of MB to the SD card.
    Note `/tmp` is NOT tmpfs on Raspberry Pi OS — it lives on the boot medium and
    is merely cleared at boot by systemd. `/dev/shm` is the real tmpfs (verified
    on hardware 2026-08-10), hence DEFAULT_SNAPSHOT_DIR.

    Copies all WAL parts together; a missing -wal/-shm is tolerated only because
    a freshly-checkpointed DB legitimately has none.
    """
    dest.mkdir(parents=True, exist_ok=True)
    main_db = db_dir / MSGSTORE_PARTS[0]
    if not main_db.is_file():
        raise FileNotFoundError(f"no msgstore.db under {db_dir}")
    for name in MSGSTORE_PARTS:
        src = db_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    contacts = db_dir / CONTACTS_DB
    if contacts.is_file():
        shutil.copy2(contacts, dest / CONTACTS_DB)
    return dest / MSGSTORE_PARTS[0]


def _iso_utc(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


def _truncate(text: str, limit: int = EXCERPT_MAX) -> str:
    """Excerpts are length-capped so a compromised reader gets a small channel."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def read_since(
    msgstore: Path,
    cursor: int,
    *,
    contacts: Path | None = None,
    limit: int = DEFAULT_BATCH,
    chats: frozenset[str] | None = None,
) -> list[Message]:
    """Return messages with `_id` greater than `cursor`, oldest first.

    `chats` optionally restricts output to named chats; None means all. Filtering
    here rather than downstream keeps unwanted content out of the pipeline
    entirely.
    """
    uri = f"file:{msgstore}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        if contacts is not None and contacts.is_file():
            conn.execute("ATTACH DATABASE ? AS wa", (f"file:{contacts}?mode=ro",))
        else:
            # Keep the query valid when wa.db is absent; names degrade to JIDs.
            conn.execute("ATTACH DATABASE ':memory:' AS wa")
            conn.execute("CREATE TABLE wa.wa_contacts(jid TEXT, wa_name TEXT, display_name TEXT)")

        out: list[Message] = []
        for row in conn.execute(_QUERY, (cursor, limit)):
            chat = str(row[1])
            if chats is not None and chat not in chats:
                continue
            sender = "me" if int(row[5]) == 1 else (str(row[2]) if row[2] is not None else "?")
            out.append(
                Message(
                    id=int(row[0]),
                    chat=chat,
                    sender=sender,
                    timestamp=_iso_utc(int(row[3])),
                    excerpt=_truncate(str(row[4])),
                )
            )
        return out
    finally:
        conn.close()


def load_cursor(path: Path) -> int:
    """Missing or unreadable cursor means start from zero, not crash."""
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_cursor(path: Path, value: int) -> None:
    """Write atomically — a torn cursor file would replay or skip messages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{value}\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    db_dir = Path(args[0]) if args else DEFAULT_DB_DIR
    cursor_path = Path(args[1]) if len(args) > 1 else DEFAULT_CURSOR_PATH
    snap_dir = Path(args[2]) if len(args) > 2 else DEFAULT_SNAPSHOT_DIR

    msgstore = snapshot(db_dir, snap_dir)
    cursor = load_cursor(cursor_path)
    messages = read_since(msgstore, cursor, contacts=snap_dir / CONTACTS_DB)

    try:
        for msg in messages:
            json.dump(asdict(msg), sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        sys.stdout.flush()
    except BrokenPipeError:
        # The consumer went away mid-stream, so these messages were NOT delivered.
        # Leave the cursor alone and let the next run re-read them: replaying is
        # recoverable, silently skipping is not. Redirect stdout to devnull so
        # Python's own exit-time flush doesn't raise this again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1

    # Only advance once the batch is actually out the door.
    if messages:
        save_cursor(cursor_path, messages[-1].id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
