"""Decide which Signal messages are commands, and drop everything else.

The channel carries traffic in both directions; this is the only thing that says
which of it counts. Nothing downstream re-checks, because nothing downstream ever
sees a message this process did not accept — the gate is the sole forwarder, so a
capability to be invoked by anyone else does not exist rather than being filtered.

No model runs here. It is host code, stdlib only, and the three rules it applies
come from ADR 0004, ADR 0007 and runbook 03 section 5:

1. **A known conversation.** The sender must be a configured principal, and the
   message must be one-to-one. A group has no single sender, so it has no
   principal; groups are dropped. An empty principal list refuses to start rather
   than defaulting to "accept anyone", the same posture as the chat allowlist.
2. **A trigger is a `dataMessage` with a non-empty body.** The receive stream also
   carries `typingMessage` and `receiptMessage` envelopes — verified on hardware
   2026-08-11, a plain "someone is typing" arrives as a `receive` notification
   with no `dataMessage` at all. Fire on any `receive` and the assistant is
   invocable by anyone who knows the number and can type at it, and checking the
   sender does not save you on an envelope carrying no command. The daemon cannot
   filter these: `--ignore-*` covers attachments, stories, avatars and stickers.
3. **Confirmation replies are commands too.** A `YES` authorising an action has to
   be matched to the action it answers, never treated as a global "proceed". The
   pending-action registry belongs to M4; what this owes it is `reply_to`, the id
   of the quoted message, so the link survives the trip through here.

Logs carry decisions and counts, never bodies and never numbers (threat model R4,
the same rule that put `--no-receive-stdout` and `--scrub-log` on the daemon). The
per-reason drop counters are what will show someone probing the number.

Run by hand:

    python3 -m gate.signal [config.toml] [commands.jsonl]
"""

from __future__ import annotations

import json
import socket
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("/opt/wpa/config/config.toml")
DEFAULT_SOCKET = Path("/run/wpa-signal/socket")
# systemd StateDirectory=wpa-gate creates and owns this.
DEFAULT_COMMANDS = Path("/var/lib/wpa-gate/commands.jsonl")

# `active` is not `ready`: Type=simple marks signal-cli started when the JVM
# launches, and the socket appeared ~19s later on a cold boot. So connecting is a
# retry loop, not a single attempt — otherwise the gate dies at every reboot while
# looking perfectly healthy. Same loop covers the daemon's own Restart=on-failure.
BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)


@dataclass(frozen=True)
class Principal:
    """Someone allowed to talk to the assistant, and under which profile.

    `profile` is a label this process never interprets — M4 decides what tools,
    whose credentials and which confirmation route it means (ADR 0007). Carrying
    it from here means adding a person is a config edit rather than a redesign.
    """

    number: str
    name: str
    profile: str


@dataclass(frozen=True)
class Command:
    """The only shape that leaves the gate."""

    timestamp: int
    principal: str
    profile: str
    body: str
    # id of the quoted message: which pending action a "YES" is answering.
    reply_to: int | None


def _field(obj: object, *path: str) -> object:
    """Walk nested JSON without ever naming `Any` — mypy is strict here on purpose.

    Anything missing, or a non-object where an object was expected, is None. The
    input is attacker-shaped: it must not be able to raise its way past a check.
    """
    for key in path:
        if not isinstance(obj, dict):
            return None
        value: object = obj.get(key)
        obj = value
    return obj


def decide(notification: object, principals: Mapping[str, Principal]) -> Command | str:
    """A `Command` to forward, or a one-word reason it was dropped.

    Order matters only for what the log says; every rule is a hard refusal.
    """
    if _field(notification, "method") != "receive":
        return "not a receive"

    envelope = _field(notification, "params", "envelope")

    # sourceNumber is the usual one. sourceUuid covers a contact signal-cli knows
    # only by ACI, and `source` is what older versions called it; a principal is
    # keyed under whichever of these the config gave.
    principal: Principal | None = None
    for key in ("sourceNumber", "sourceUuid", "source"):
        value = _field(envelope, key)
        if isinstance(value, str) and value in principals:
            principal = principals[value]
            break
    if principal is None:
        return "sender"

    body = _field(envelope, "dataMessage", "message")
    if not isinstance(body, str) or not body.strip():
        return "no body"

    # A group message has a sender but not a conversation we own, and no single
    # principal to attribute it to.
    if _field(envelope, "dataMessage", "groupInfo") is not None:
        return "group"

    timestamp = _field(envelope, "timestamp")
    quoted = _field(envelope, "dataMessage", "quote", "id")
    return Command(
        timestamp=timestamp if isinstance(timestamp, int) else 0,
        principal=principal.name,
        profile=principal.profile,
        body=body.strip(),
        reply_to=quoted if isinstance(quoted, int) else None,
    )


def load_config(path: Path) -> tuple[Path, dict[str, Principal]]:
    """Read `[signal]` out of the config: the socket, and who may use it.

    Keyed by number and, when given, by uuid — signal-cli identifies a contact by
    whichever it knows. A missing file raises and an empty principal list raises:
    running with no principals would mean either "accept everyone", which is not
    an allowlist, or "accept nobody", which is a silently dead assistant.
    """
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    section: object = raw.get("signal")
    if not isinstance(section, dict):
        raise ValueError(f"no [signal] section in {path}")

    socket_value: object = section.get("socket")
    socket_path = Path(socket_value) if isinstance(socket_value, str) else DEFAULT_SOCKET

    principals: dict[str, Principal] = {}
    entries: object = section.get("principals")
    for entry in entries if isinstance(entries, list) else []:
        number = _field(entry, "number")
        name = _field(entry, "name")
        profile = _field(entry, "profile")
        if not (isinstance(number, str) and isinstance(name, str) and isinstance(profile, str)):
            raise ValueError(f"principal in {path} needs number, name and profile")
        who = Principal(number=number, name=name, profile=profile)
        principals[number] = who
        uuid = _field(entry, "uuid")
        if isinstance(uuid, str):
            principals[uuid] = who

    if not principals:
        raise ValueError(f"no [[signal.principals]] in {path} — the gate would accept nobody")
    return socket_path, principals


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def connect(socket_path: Path, *, attempts: int = 0) -> socket.socket:
    """Connect, retrying with backoff. `attempts` of 0 means keep trying forever."""
    delay_index = 0
    tries = 0
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(str(socket_path))
            return conn
        except OSError as exc:
            conn.close()
            tries += 1
            if attempts and tries >= attempts:
                raise
            # Every tenth attempt, not just the first: a permanent failure —
            # EACCES because the socket isn't group-writable, say — otherwise
            # prints one line at boot and then looks identical to a healthy gate
            # sitting quietly. At the 30s cap that is a line every five minutes.
            if tries == 1 or tries % 10 == 0:
                _log(f"waiting for {socket_path}: {exc.strerror} (attempt {tries})")
            time.sleep(BACKOFF_SECONDS[delay_index])
            delay_index = min(delay_index + 1, len(BACKOFF_SECONDS) - 1)


def _ack(conn: socket.socket, recipient: str, command: Command) -> None:
    """Tell the sender their command was accepted, and which one.

    The timestamp is the handle: a later reply quoting this ack is how M4 will
    match a confirmation to its pending action.
    """
    request = {
        "jsonrpc": "2.0",
        "id": f"ack-{command.timestamp}",
        "method": "send",
        "params": {"recipient": [recipient], "message": f"ack {command.timestamp}"},
    }
    conn.sendall((json.dumps(request) + "\n").encode())


def run(
    socket_path: Path,
    principals: Mapping[str, Principal],
    commands: Path,
    *,
    cycles: int = 0,
) -> None:
    """Read the receive stream forever, forwarding what passes and counting the rest.

    `cycles` bounds how many times the socket is reconnected; 0 is forever, which
    is what the unit file wants. Reconnection is not optional: signal-cli has
    Restart=on-failure, so the socket goes away and comes back under a live gate.
    """
    numbers = {p.name: p.number for p in principals.values()}
    dropped: dict[str, int] = {}
    connections = 0
    # systemd's StateDirectory= makes this; by-hand runs need it made.
    commands.parent.mkdir(parents=True, exist_ok=True)

    while True:
        conn = connect(socket_path)
        connections += 1
        _log(f"connected to {socket_path}")
        try:
            for line in conn.makefile("r", encoding="utf-8"):
                if not line.strip():
                    continue
                try:
                    notification: object = json.loads(line)
                except json.JSONDecodeError:
                    _log("dropped: unparseable line")
                    continue

                # Objects with no `method` are JSON-RPC responses — our own acks
                # coming back, mostly. Not decisions, so not counted as drops.
                if _field(notification, "method") is None:
                    continue

                verdict = decide(notification, principals)
                if isinstance(verdict, str):
                    dropped[verdict] = dropped.get(verdict, 0) + 1
                    _log(f"dropped: {verdict} ({dropped[verdict]} total)")
                    continue

                with commands.open("a", encoding="utf-8") as fh:
                    json.dump(asdict(verdict), fh, ensure_ascii=False)
                    fh.write("\n")
                _log(
                    f"accepted principal={verdict.principal} profile={verdict.profile} "
                    f"len={len(verdict.body)} reply_to={verdict.reply_to}"
                )
                _ack(conn, numbers[verdict.principal], verdict)
        except OSError as exc:
            _log(f"connection lost: {exc.strerror}")
        finally:
            conn.close()

        if cycles and connections >= cycles:
            return
        _log("reconnecting")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    config = Path(args[0]) if args else DEFAULT_CONFIG
    commands = Path(args[1]) if len(args) > 1 else DEFAULT_COMMANDS

    socket_path, principals = load_config(config)
    _log(f"gate up: {len(set(p.name for p in principals.values()))} principal(s)")
    run(socket_path, principals, commands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
