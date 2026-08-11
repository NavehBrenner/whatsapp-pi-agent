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

Reading Waydroid's app-private directory needs root, so under systemd that copy
is done by a separate privileged unit (deploy/systemd/wpa-snapshot.service) and
this process only ever sees the snapshot. Run by hand it does its own copy and
therefore needs root:

    sudo PYTHONPATH=src python3 -m reader.msgstore <db_dir> <cursor> <snap_dir> [config.toml]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Waydroid bind-mounts Android's /data from the session user's home, NOT from
# /var/lib/waydroid (verified on hardware 2026-08-10).
DEFAULT_DB_DIR = Path.home() / ".local/share/waydroid/data/data/com.whatsapp/databases"

# Copied as a set — the WAL holds recent messages that aren't in the .db yet.
MSGSTORE_PARTS = ("msgstore.db", "msgstore.db-wal", "msgstore.db-shm")
# Same rule for wa.db: contact edits sit in its WAL too, so the .db alone is stale.
CONTACTS_PARTS = ("wa.db", "wa.db-wal", "wa.db-shm")
CONTACTS_DB = CONTACTS_PARTS[0]

EXCERPT_MAX = 200
DEFAULT_BATCH = 500

# Real tmpfs. NOT /tmp — on Raspberry Pi OS that is on the SD card.
DEFAULT_SNAPSHOT_DIR = Path("/dev/shm/wpa-snapshot")
# systemd StateDirectory=wpa-reader creates and owns this; no install-time chown.
DEFAULT_CURSOR_PATH = Path("/var/lib/wpa-reader/cursor")

# Sender names, in descending quality. The @lid -> phone-JID hop through jid_map
# is what makes this usable: group participants are identified by LID, LIDs do not
# join to wa_contacts, and without the hop only ~5% of received messages resolve
# to a name (NVB-7). Names still absent after the hop are strangers in public
# groups who were never in the address book — their pushname is not on the device
# at all, so those degrade to the bare LID by design.
_QUERY = """
SELECT m._id,
       COALESCE(NULLIF(c.subject, ''), cj.user, '?')                    AS chat,
       COALESCE(NULLIF(ldn.display_name, ''),
                NULLIF(w.wa_name, ''),
                NULLIF(w.display_name, ''),
                NULLIF(pw.wa_name, ''),
                NULLIF(pw.display_name, ''),
                sj.user)                                                AS sender,
       m.timestamp                                                      AS ts,
       m.text_data                                                      AS body,
       m.from_me                                                        AS from_me,
       cj.raw_string                                                    AS chat_jid
FROM message m
JOIN chat c              ON c._id  = m.chat_row_id
LEFT JOIN jid cj         ON cj._id = c.jid_row_id
-- In a 1:1 chat sender_jid_row_id is 0 and there is no jid row: the sender is
-- the chat. Resolving through the same joins names those too (13% of messages).
LEFT JOIN jid sj         ON sj._id = COALESCE(NULLIF(m.sender_jid_row_id, 0), c.jid_row_id)
LEFT JOIN lid_display_name ldn ON ldn.lid_row_id = sj._id
LEFT JOIN wa.wa_contacts w     ON w.jid = sj.raw_string
LEFT JOIN jid_map jm     ON jm.lid_row_id = sj._id
LEFT JOIN jid pj         ON pj._id = jm.jid_row_id
LEFT JOIN wa.wa_contacts pw    ON pw.jid = pj.raw_string
WHERE m._id > ?
  AND m.text_data IS NOT NULL
{chat_filter}
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

    When source and destination are the same directory the snapshot has already
    been taken by someone else — under systemd that is the privileged
    wpa-snapshot unit, which is the only thing allowed near Waydroid's data.
    """
    dest.mkdir(parents=True, exist_ok=True)
    main_db = db_dir / MSGSTORE_PARTS[0]
    if not main_db.is_file():
        raise FileNotFoundError(f"no msgstore.db under {db_dir}")
    if db_dir.resolve() == dest.resolve():
        return dest / MSGSTORE_PARTS[0]
    # Clear before copying: a -wal left from a previous poll would be applied to
    # a newer .db and silently return wrong rows (same reasoning as snapshot.sh).
    for name in MSGSTORE_PARTS + CONTACTS_PARTS:
        (dest / name).unlink(missing_ok=True)
    for name in MSGSTORE_PARTS:
        src = db_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    for name in CONTACTS_PARTS:
        src = db_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
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

    `chats` optionally restricts output to an allowlist of chat **JIDs**; None
    means all. Filtering here rather than downstream keeps unwanted content out
    of the pipeline entirely.

    The allowlist keys on the JID and not the group subject on purpose: subjects
    are chosen by whoever is in the group, so anyone could rename a group to
    match an allowlisted name and talk their way into the pipeline.

    The filter is applied in SQL rather than to the returned rows, because the
    caller advances the cursor to the last message it got back. Filtering in
    Python, a batch containing no allowlisted messages returns nothing, the
    cursor never moves, and the reader re-reads the same rows forever.
    """
    if chats is not None and not chats:
        return []
    if chats is None:
        sql = _QUERY.format(chat_filter="")
        params: tuple[str | int, ...] = (cursor, limit)
    else:
        sql = _QUERY.format(chat_filter=f"  AND cj.raw_string IN ({','.join('?' * len(chats))})")
        params = (cursor, *sorted(chats), limit)

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
        for row in conn.execute(sql, params):
            chat = str(row[1])
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


def load_chats(path: Path) -> frozenset[str]:
    """Read the `[whatsapp] chats` allowlist out of a config file.

    A missing file raises: running unattended with no config would otherwise
    quietly mean "read everything", and an allowlist that defaults to everything
    is not an allowlist. A config with no `chats` key means read nothing, which
    looks broken and is meant to — fill it in deliberately.
    """
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    section = raw.get("whatsapp")
    chats = section.get("chats") if isinstance(section, dict) else None
    return frozenset(str(c) for c in chats) if isinstance(chats, list) else frozenset()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    db_dir = Path(args[0]) if args else DEFAULT_DB_DIR
    cursor_path = Path(args[1]) if len(args) > 1 else DEFAULT_CURSOR_PATH
    snap_dir = Path(args[2]) if len(args) > 2 else DEFAULT_SNAPSHOT_DIR
    # No config path means by-hand invocation: every chat, as before. The unit
    # file always passes one.
    chats = load_chats(Path(args[3])) if len(args) > 3 else None

    msgstore = snapshot(db_dir, snap_dir)
    cursor = load_cursor(cursor_path)
    messages = read_since(msgstore, cursor, contacts=snap_dir / CONTACTS_DB, chats=chats)

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
