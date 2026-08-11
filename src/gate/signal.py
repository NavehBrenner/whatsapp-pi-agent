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

Traffic also goes the other way, and the same process carries it (ADR 0009). An
agent never touches the socket: a JSON-RPC client of signal-cli receives the
inbound stream as well as sending, so an agent holding it would see every envelope
the gate refused. Instead the agent writes `{"to": "<name>", "text": "..."}` into
its own outbox directory and this process resolves the **name** through its own
roster, checks it against that principal's send list, and sends. The agent handles
no identifier, so it cannot forge one; and the addressable set is exactly the
roster, so a name that is not in it resolves to nothing at all.

Logs carry decisions and counts, never bodies and never numbers (threat model R4,
the same rule that put `--no-receive-stdout` and `--scrub-log` on the daemon). The
per-reason drop counters are what will show someone probing the number. Outbound
refusals log no identifier and **not the requested name either** — that name is
free text an agent chose, on its way to journald.

Run by hand:

    python3 -m gate.signal [config.toml] [commands.jsonl]
"""

from __future__ import annotations

import json
import os
import select
import socket
import stat
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("/opt/wpa/config/config.toml")
DEFAULT_SOCKET = Path("/run/wpa-signal/socket")
# systemd StateDirectory=wpa-gate creates and owns this.
DEFAULT_COMMANDS = Path("/var/lib/wpa-gate/commands.jsonl")

# How long a blocked `select` waits before the outbox is looked at. It is the
# worst-case latency on a reply and the cost of a directory scan; 250ms is cheap
# on both counts. The receive loop is single-threaded on purpose — two threads
# sharing one socket, inside the one process whose job is to be trustworthy, is a
# bad trade for a quarter second.
POLL_SECONDS = 0.25

# Three bounds, because outbound traffic fails in three different ways.
MAX_ENTRY_BYTES = 64 * 1024  # one entry; anything larger is not a message
MAX_PENDING = 32  # entries kept per outbox directory
DRAIN_PER_CYCLE = 4  # sent per poll cycle, so the receive stream is never starved
# ponytail: these bound a burst, not a steady loop — write one entry, watch the
# gate send it, repeat forever. The bound for that is a per-profile token bucket,
# deferred to NVB-15: nothing can write to an outbox until an agent exists to be
# granted the directory, so today there is no writer to loop.

# Requests whose response is still wanted, and lines kept in the sent log. Both
# are placeholders for NVB-16's expiring registry; both are capped so a daemon
# that stops answering cannot grow the process without limit.
MAX_AWAITING = 64
SENT_KEEP = 512

# A peer that never sends a newline must not be able to grow the read buffer
# until the gate dies. The daemon is not the threat here; what it relays is.
MAX_BUFFER_BYTES = 1024 * 1024

# `active` is not `ready`: Type=simple marks signal-cli started when the JVM
# launches, and the socket appeared ~26s later on a cold boot (2026-08-11). So
# connecting is a retry loop, not a single attempt — otherwise the gate dies at
# every reboot while looking perfectly healthy. Same loop covers the daemon's own
# Restart=on-failure.
#
# The ceiling is 10s and not 30s because it is the worst-case delay between the
# socket existing and the assistant answering: at 30s the first post-reboot
# connect landed ~25s after the socket appeared, all of it spent asleep.
BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0)


@dataclass(frozen=True)
class Principal:
    """Someone allowed to talk to the assistant, and under which profile.

    `profile` is a label this process never interprets — M4 decides what tools,
    whose credentials and which confirmation route it means (ADR 0007). Carrying
    it from here means adding a person is a config edit rather than a redesign.

    `send_to` is the set of names this principal's agent may address, and it is
    checked by name, never by identifier — the agent has no identifier to offer.
    The default is `("self",)`: an agent granted nothing can still answer its own
    principal and reach no one else. A listed name must be another principal;
    messaging someone who cannot message back is a separate capability with its
    own decision to come (ADR 0009).

    **No identifier lives here, and which direction that protects matters.** On the
    inbound path it is load-bearing: the identifiers are the keys of the map this
    is looked up in, and an ack goes back to the address the message arrived from,
    never to a value copied out of config, so a typo in the allowlist means
    "nobody matches" rather than "the ack went to a stranger". An agent-initiated
    send has no envelope to copy from, so there the roster — built from the same
    config rows — is the only source, which is ADR 0009's "the addressable set is
    exactly the roster".
    """

    name: str
    profile: str
    send_to: tuple[str, ...] = ("self",)


@dataclass(frozen=True)
class Config:
    """Everything `[signal]` decides: where the daemon is, and who is who.

    `principals` is keyed by **identifier** — that is the inbound question, "did
    this envelope come from someone we know". `roster` is keyed by **name** and
    answers the outbound one, "the agent said `mom`, who is that". Two maps over
    the same rows, because they are asked opposite questions.
    """

    socket: Path
    principals: dict[str, Principal] = field(default_factory=dict)
    roster: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Command:
    """The only shape that leaves the gate."""

    timestamp: int
    principal: str
    profile: str
    body: str
    # id of the quoted message: which pending action a "YES" is answering.
    reply_to: int | None


@dataclass(frozen=True)
class Outbound:
    """One outbox entry that survived resolution, ready to go on the wire.

    `entry` is the filename it came from, which is also the correlation id: the
    daemon echoes it back on the response carrying the timestamp NVB-16 keys on.
    """

    entry: str
    principal: str
    profile: str
    recipient: str
    text: str


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

    # `sourceUuid` is the identifier that actually arrives: current Signal does not
    # share phone numbers by default, so `sourceNumber` is null on real traffic
    # (hardware, 2026-08-11) and a number-keyed allowlist matches nothing. Both are
    # accepted because a contact who does share a number sends both. `sourceName`
    # is NOT consulted at any point — it is a profile name its owner chooses.
    principal: Principal | None = None
    for key in ("sourceUuid", "sourceNumber", "source"):
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


def load_config(path: Path) -> Config:
    """Read `[signal]` out of the config: the socket, and who may use it.

    A principal is keyed by `uuid` and/or `number`, and needs at least one. In
    practice it needs the **uuid**: Signal stopped sharing phone numbers by
    default, so real envelopes carry `sourceNumber: null` and nothing else will
    ever match. The number is kept as a second key because it is the part a human
    can check by eye, and because a contact who does share one sends both.

    A missing file raises and an empty principal list raises: running with no
    principals would mean either "accept everyone", which is not an allowlist, or
    "accept nobody", which is a silently dead assistant.

    The same rows build the outbound roster, and two things that are merely untidy
    inbound are refusals once names address people: a **duplicate name**, because
    then `to: "mom"` has two answers and the config does not say which, and a
    `send_to` naming someone who is not a principal, because the send list may
    hold only principals (ADR 0009) and a typo would otherwise read as a silently
    narrower grant. Both raise rather than resolving to something plausible.
    """
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    section: object = raw.get("signal")
    if not isinstance(section, dict):
        raise ValueError(f"no [signal] section in {path}")

    socket_value: object = section.get("socket")
    socket_path = Path(socket_value) if isinstance(socket_value, str) else DEFAULT_SOCKET

    principals: dict[str, Principal] = {}
    roster: dict[str, str] = {}
    entries: object = section.get("principals")
    for entry in entries if isinstance(entries, list) else []:
        name = _field(entry, "name")
        profile = _field(entry, "profile")
        if not (isinstance(name, str) and isinstance(profile, str)):
            raise ValueError(f"principal in {path} needs a name and a profile")
        if name in roster:
            raise ValueError(f"two principals named {name} in {path} — a name must address one")

        listed: object = _field(entry, "send_to")
        if listed is None:
            send_to: tuple[str, ...] = ("self",)
        elif isinstance(listed, list) and all(isinstance(item, str) for item in listed):
            send_to = tuple(item for item in listed if isinstance(item, str))
        else:
            raise ValueError(f"send_to for {name} in {path} must be a list of names")

        who = Principal(name=name, profile=profile, send_to=send_to)
        keys = [_field(entry, "uuid"), _field(entry, "number")]
        if not any(isinstance(key, str) for key in keys):
            raise ValueError(f"principal {name} in {path} needs a uuid or a number")
        for key in keys:
            if isinstance(key, str):
                principals[key] = who
                # The uuid comes first and wins: it is what actually arrives, and
                # it names the account rather than the line. A number that has
                # since been re-registered points at whoever holds it next.
                roster.setdefault(name, key)

    if not principals:
        raise ValueError(f"no [[signal.principals]] in {path} — the gate would accept nobody")

    for who in set(principals.values()):
        for target in who.send_to:
            if target != "self" and target not in roster:
                raise ValueError(
                    f"{who.name} may send to {target} in {path}, who is not a principal"
                )
    return Config(socket=socket_path, principals=principals, roster=roster)


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


def _read_entry(path: Path) -> tuple[str, str] | str:
    """`(to, text)` out of one outbox file, or a one-word reason it was refused.

    `O_NOFOLLOW` because a symlink placed here must not become something the gate
    reads — `/var/lib/wpa-signal` is the account, and the agent that writes this
    directory is the least trusted thing on the box. `O_NONBLOCK` and the regular-
    file check because opening a fifo read-only otherwise blocks until a writer
    appears, which is a whole control channel stopped by one `mkfifo`.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "malformed"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "malformed"
        if info.st_size > MAX_ENTRY_BYTES:
            return "too long"
        raw = os.read(fd, MAX_ENTRY_BYTES)
    except OSError:
        return "malformed"
    finally:
        os.close(fd)

    try:
        entry: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "malformed"
    to = _field(entry, "to")
    text = _field(entry, "text")
    if not (isinstance(to, str) and isinstance(text, str) and text.strip()):
        return "malformed"
    return to, text


def resolve(to: str, who: Principal, roster: Mapping[str, str]) -> str | None:
    """The identifier to send to, or None if this principal may not address `to`.

    The name is checked against the send list *before* it is looked up, so an
    unlisted name and an unknown one are the same refusal — the agent learns
    nothing about who exists from which way it was told no.
    """
    if to not in who.send_to:
        return None
    # ponytail: `self` is the principal's own 1:1 conversation, which today is the
    # only kind there is. NVB-12 makes authority a (conversation, sender) pair, and
    # this is the line that has to become "the conversation the command arrived
    # from" — a group reply is disclosed to everyone in it (ADR 0008).
    return roster.get(who.name if to == "self" else to)


def drain(outbox: Path, config: Config, refused: dict[str, int]) -> list[Outbound]:
    """Take what the agents wrote, resolve it, and return what may be sent.

    **Delivery is at-most-once: the file is unlinked before the send, not after.**
    A crash in between loses a reply; the other order sends a person the same
    message twice. A gap is visible to whoever asked, and a duplicate is not.

    Refused entries are consumed too. Leaving them would turn one malformed file
    into a permanent retry loop, and the agent is told nothing either way — what
    it may carry back is NVB-15's decision, when there is an agent to receive it.
    """
    ready: list[Outbound] = []
    for name, identifier in config.roster.items():
        who = config.principals[identifier]
        # Dotfiles are skipped so the write-then-rename the agent does is never
        # read half-written; pathlib's glob matches them, unlike the shell's.
        entries = sorted(
            path for path in (outbox / name).glob("*.json") if not path.name.startswith(".")
        )
        # The *newest* go, the oldest are kept: past this many pending, the likely
        # explanation is a loop or an injection rather than a person's traffic,
        # and the first few entries are the ones written before it went wrong.
        for extra in entries[MAX_PENDING:]:
            extra.unlink(missing_ok=True)
            _refuse("too many", refused)

        for path in entries[:DRAIN_PER_CYCLE]:
            parsed = _read_entry(path)
            path.unlink(missing_ok=True)
            if isinstance(parsed, str):
                _refuse(parsed, refused)
                continue
            to, text = parsed
            recipient = resolve(to, who, config.roster)
            if recipient is None:
                _refuse("not in list", refused)
                continue
            ready.append(
                Outbound(
                    entry=path.stem,
                    principal=name,
                    profile=who.profile,
                    recipient=recipient,
                    text=text,
                )
            )
    return ready


def _refuse(reason: str, refused: dict[str, int]) -> None:
    """Count it and say so — with no identifier, and not the requested name either.

    The name came out of a file an agent wrote. It is free text, chosen by the
    least trusted process here, on its way to journald (threat model R4).
    """
    refused[reason] = refused.get(reason, 0) + 1
    _log(f"refused send: {reason} ({refused[reason]} total)")


def _prepare(outbox: Path, config: Config) -> None:
    """One directory per principal, gate-owned, made before anything writes.

    0700 today because no agent user exists yet to grant. NVB-14/15 gives each
    principal its own group (`wpa-out-<name>`) and mode 0730 — write and execute
    so the agent can create and rename, no read so it cannot enumerate what else
    is queued. **One group per principal, never one shared group**: a shared one
    lets agent A drop an entry in agent B's directory and send under B's send
    list, which is ADR 0009's isolation undone by a group name.
    """
    for name in config.roster:
        directory = outbox / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)  # mkdir's mode is masked by umask; this is not


def _record(
    response: object, awaiting: dict[str, Outbound], sent: Path, refused: dict[str, int]
) -> int | None:
    """Match a JSON-RPC response to the send it answers and log its timestamp.

    That timestamp is the whole point of capturing responses at all: a quoted
    reply arrives carrying `quote.id` set to the timestamp of the assistant's own
    message, so without recording it a later YES cannot be matched to the prompt
    it answers. NVB-16 replaces this file with a registry that expires entries.

    Both shapes were captured off the daemon on 2026-08-11 rather than assumed:

        {"result": {"timestamp": 1786473936544, "results": [{"type": "SUCCESS", …}]}}
        {"error": {"code": -1, "message": "Failed to send message",
                   "data": {"response": {"results": [{"type": "UNREGISTERED_FAILURE", …}],
                                         "timestamp": …}}}}

    A failure carries **no top-level `result`**, so "did this land" is the same
    question as "is there a timestamp here". The gate sends to exactly one
    recipient at a time, so there is no partial success to disentangle — and the
    reason worth logging is the per-recipient `type`, since `code` is -1 for
    everything.
    """
    request_id = _field(response, "id")
    if not isinstance(request_id, str):
        return None
    outbound = awaiting.pop(request_id, None)
    if outbound is None:
        return None

    timestamp = _field(response, "result", "timestamp")
    if not isinstance(timestamp, int):
        results = _field(response, "error", "data", "response", "results")
        first = results[0] if isinstance(results, list) and results else None
        reason = _field(first, "type") or _field(response, "error", "code")
        # A delivery status is not an identifier, so it may be logged. Not
        # retried: a resend needs to know whether the first attempt landed, and
        # nothing here does.
        _refuse(f"send failed: {reason if isinstance(reason, (str, int)) else 'unknown'}", refused)
        return None

    with sent.open("a", encoding="utf-8") as fh:
        json.dump(
            {
                "timestamp": timestamp,
                "principal": outbound.principal,
                "profile": outbound.profile,
                "entry": outbound.entry,
            },
            fh,
        )
        fh.write("\n")
    # ponytail: rewrite the tail rather than rotate. At SENT_KEEP lines the file is
    # tens of KB, and NVB-16 replaces it with something that expires by age.
    lines = sent.read_text(encoding="utf-8").splitlines()
    if len(lines) > SENT_KEEP:
        sent.write_text("\n".join(lines[-SENT_KEEP:]) + "\n", encoding="utf-8")
    return timestamp


def _send(conn: socket.socket, request_id: str, recipient: str, message: str) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "send",
        "params": {"recipient": [recipient], "message": message},
    }
    conn.sendall((json.dumps(request) + "\n").encode())


def _ack(conn: socket.socket, recipient: str, command: Command) -> Outbound:
    """Tell the sender their command was accepted, and which one.

    The timestamp is the handle: a later reply quoting this ack is how M4 will
    match a confirmation to its pending action. Which is why the ack is registered
    for response capture like any other send — the message a person quotes when
    they answer YES is usually this one.
    """
    request_id = f"ack-{command.timestamp}"
    message = f"ack {command.timestamp}"
    _send(conn, request_id, recipient, message)
    return Outbound(
        entry=request_id,
        principal=command.principal,
        profile=command.profile,
        recipient=recipient,
        text=message,
    )


def _await(awaiting: dict[str, Outbound], request_id: str, outbound: Outbound) -> None:
    """Remember a request until its response arrives, oldest evicted first.

    Bounded because a daemon that stops answering must not grow this process. An
    evicted send still happened; only the record of its timestamp is lost.
    """
    awaiting[request_id] = outbound
    while len(awaiting) > MAX_AWAITING:
        awaiting.pop(next(iter(awaiting)))


def run(
    config: Config,
    commands: Path,
    *,
    outbox: Path | None = None,
    sent: Path | None = None,
    cycles: int = 0,
) -> None:
    """Read the receive stream forever, forwarding what passes and counting the rest.

    The same loop drains the outbox, which is why it is a `select` with a timeout
    rather than iterating `conn.makefile("r")`: that call blocks, and a blocked
    read cannot also poll a directory.

    `cycles` bounds how many times the socket is reconnected; 0 is forever, which
    is what the unit file wants. Reconnection is not optional: signal-cli has
    Restart=on-failure, so the socket goes away and comes back under a live gate.
    """
    dropped: dict[str, int] = {}
    refused: dict[str, int] = {}
    awaiting: dict[str, Outbound] = {}
    connections = 0
    # systemd's StateDirectory= makes this; by-hand runs need it made.
    commands.parent.mkdir(parents=True, exist_ok=True)
    outbox = commands.parent / "outbox" if outbox is None else outbox
    sent = commands.parent / "sent.jsonl" if sent is None else sent
    _prepare(outbox, config)

    while True:
        conn = connect(config.socket)
        connections += 1
        _log(f"connected to {config.socket}")
        buffer = b""
        due = 0.0
        try:
            while True:
                readable, _, _ = select.select([conn], [], [], POLL_SECONDS)
                if readable:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break  # the daemon went away; reconnect
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        _handle(raw, conn, config, commands, sent, dropped, refused, awaiting)
                    # Checked on what is *left*, not on what arrived: a large batch
                    # of complete lines is ordinary, one endless line is not.
                    if len(buffer) > MAX_BUFFER_BYTES:
                        _log("dropped: line longer than the buffer")
                        break

                now = time.monotonic()
                if now >= due:
                    # By the clock rather than only on an idle `select`, so a busy
                    # receive stream cannot starve the outbox indefinitely.
                    due = now + POLL_SECONDS
                    for outbound in drain(outbox, config, refused):
                        request_id = f"send-{outbound.entry}"
                        _send(conn, request_id, outbound.recipient, outbound.text)
                        _await(awaiting, request_id, outbound)
                        _log(
                            f"sent principal={outbound.principal} "
                            f"profile={outbound.profile} len={len(outbound.text)}"
                        )
        except OSError as exc:
            _log(f"connection lost: {exc.strerror}")
        finally:
            conn.close()

        if cycles and connections >= cycles:
            return
        _log("reconnecting")


def _handle(
    raw: bytes,
    conn: socket.socket,
    config: Config,
    commands: Path,
    sent: Path,
    dropped: dict[str, int],
    refused: dict[str, int],
    awaiting: dict[str, Outbound],
) -> None:
    """One line off the wire: a notification to judge, or a response to record."""
    if not raw.strip():
        return
    try:
        notification: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _log("dropped: unparseable line")
        return

    # Objects with no `method` are JSON-RPC responses — our own sends coming back.
    # Not decisions, so not counted as drops; the timestamp in one is what lets a
    # later confirmation be matched to the message it answers.
    if _field(notification, "method") is None:
        _record(notification, awaiting, sent, refused)
        return

    verdict = decide(notification, config.principals)
    if isinstance(verdict, str):
        dropped[verdict] = dropped.get(verdict, 0) + 1
        _log(f"dropped: {verdict} ({dropped[verdict]} total)")
        return

    with commands.open("a", encoding="utf-8") as fh:
        json.dump(asdict(verdict), fh, ensure_ascii=False)
        fh.write("\n")
    _log(
        f"accepted principal={verdict.principal} profile={verdict.profile} "
        f"len={len(verdict.body)} reply_to={verdict.reply_to}"
    )
    # Reply to where it came from, never to an identifier out of the config: the
    # message got here, so this address is known good.
    sender = _field(notification, "params", "envelope", "source")
    if isinstance(sender, str):
        _await(awaiting, f"ack-{verdict.timestamp}", _ack(conn, sender, verdict))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    commands = Path(args[1]) if len(args) > 1 else DEFAULT_COMMANDS

    config = load_config(config_path)
    _log(f"gate up: {len(config.roster)} principal(s)")
    run(config, commands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
