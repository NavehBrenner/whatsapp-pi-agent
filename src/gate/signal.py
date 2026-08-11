"""Decide which Signal messages are commands, and drop everything else.

The channel carries traffic in both directions; this is the only thing that says
which of it counts. Nothing downstream re-checks, because nothing downstream ever
sees a message this process did not accept — the gate is the sole forwarder, so a
capability to be invoked by anyone else does not exist rather than being filtered.

No model runs here. It is host code, stdlib only, and the rules it applies come from
ADR 0004, ADR 0007, ADR 0008, ADR 0010 and runbook 03 section 5:

1. **A known conversation, and a known sender in it.** Authority is a property of the
   **(conversation, sender) pair**, never of the sender alone: the same person in a
   group and in their own chat are two pairs with two profiles, and the group one is
   narrower because a reply there is disclosed to everyone in the room. A
   conversation not in the table is dropped whoever sent it; a sender not listed for
   that conversation is dropped exactly like a stranger. An empty table refuses to
   start rather than defaulting to "accept anyone", the same posture as the chat
   allowlist.
2. **A trigger is a `dataMessage` with a non-empty body.** The receive stream also
   carries `typingMessage` and `receiptMessage` envelopes — verified on hardware
   2026-08-11, a plain "someone is typing" arrives as a `receive` notification with
   no `dataMessage` at all. Fire on any `receive` and the assistant is invocable by
   anyone who knows the number and can type at it, and checking the sender does not
   save you on an envelope carrying no command. The daemon cannot filter these:
   `--ignore-*` covers attachments, stories, avatars and stickers.
3. **Group membership is pinned, and drift refuses rather than degrades.** Somebody
   added to an allowlisted group is inside a channel that was allowlisted before they
   arrived. The live member set is read back from the daemon and compared; a group
   that differs refuses everything and says so. The failure mode to build is *this
   stops working and you are told*, never *this keeps working with an extra person
   present*.
4. **Confirmation replies are commands too.** A `YES` authorising an action has to be
   matched to the action it answers, never treated as a global "proceed". The
   pending-action registry belongs to NVB-16; what this owes it is `reply_to`, the id
   of the quoted message, so the link survives the trip through here. A conversation
   whose `purpose` is `confirmation` accepts *only* quoted replies, so a stray
   message there is nothing by construction rather than by an agent choosing well.

Traffic also goes the other way, and the same process carries it (ADR 0009). An
agent never touches the socket: a JSON-RPC client of signal-cli receives the inbound
stream as well as sending, so an agent holding it would see every envelope the gate
refused. Instead the agent writes `{"to": "<label>", "text": "..."}` into its own
outbox directory and this process resolves the **conversation label** through its own
table, checks it against that profile's send list, and sends. The agent handles no
identifier, so it cannot forge one; and the addressable set is exactly the
conversation table, so a label that is not in it resolves to nothing at all.

What the gate does **not** do is start anything. It names the agent that should run
and the profile it holds (ADR 0010); `deploy/render-agents.py` turns those names into
units and mounted credentials, and the enforcement is that a container without a
credential cannot use the tool at all — not a string compared at runtime.

Logs carry decisions and counts, never bodies and never numbers (threat model R4,
the same rule that put `--no-receive-stdout` and `--scrub-log` on the daemon). The
per-reason drop counters are what will show someone probing the number, and
`unlisted sender` is where probing inside an allowlisted group appears. Outbound
refusals log no identifier and **not the requested label either** — that label is
free text an agent chose, on its way to journald.

Run by hand:

    python3 -m gate.signal [config.toml] [commands.jsonl]
    python3 -m gate.signal --check [config.toml]
"""

from __future__ import annotations

import json
import os
import re
import select
import socket
import stat
import sys
import time
import tomllib
import uuid as uuidlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent.naming import agent_unit
from agent.registry import TOOLS

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

# How often the live group membership is read back. The ceiling on how long a group
# can carry an extra member before commands from it stop, *if* the update envelope
# below is not recognised; when it is, drift is caught in seconds.
MEMBERS_REFRESH_SECONDS = 900.0

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

# An agent name becomes a systemd unit instance and a directory under the outbox
# root; a pair name is a roster key that appears in logs. Neither may contain a
# path separator, a space, or anything systemd would want escaped — so both are
# held to one boring charset rather than sanitised after the fact.
NAME_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")


@dataclass(frozen=True)
class Profile:
    """A grant bundle: what an agent holding it may do, and who it may talk to.

    `tools` names entries in `agent.registry.TOOLS`, each already bound to one
    account and one verb. The fineness is in which instances are listed —
    `calendar.family.create_event` and `calendar.family.rw` are two grants over one
    calendar — so there is no scope language to parse and no policy to evaluate. The
    gate never interprets this list; it validates it and passes on the profile's
    name. Enforcement is that the container is built with exactly these tools and
    exactly their credentials, so a tool outside the bundle is not refused at runtime,
    it is absent (ADR 0010).

    `send_to` is a capability like any other: which conversations an agent may
    address, checked by label, never by identifier — the agent has no identifier to
    offer. The default is `("self",)`, the conversation the command arrived from,
    which is what an agent granted nothing can still do.
    """

    name: str
    tools: tuple[str, ...] = ()
    send_to: tuple[str, ...] = ("self",)


@dataclass(frozen=True)
class Sender:
    """One permitted sender in one conversation: the (conversation, sender) pair.

    `name` identifies the pair, not the person — the same human in two conversations
    is two rows with two names, which is what makes "mom in the family group" a
    different principal from "mom in her own chat". It is what the emitted command
    carries, so attribution survives even when several senders share one agent.
    """

    name: str
    agent: str
    profile: str


@dataclass(frozen=True)
class Recipient:
    """Where a message goes on the wire, and which JSON-RPC parameter carries it."""

    id: str
    group: bool


@dataclass(frozen=True)
class Conversation:
    """A room the assistant is in, and everyone in it who may command it.

    One shape for both kinds. A one-to-one is a conversation whose `id` is the other
    party's ACI and whose member set is exactly that one person; the duplication is
    the price of every conversation having an identical schema, and it buys back the
    second lookup table, the second code path, and the bug where a group is handled
    by one-to-one logic. `load_config` checks the duplicated halves agree, because
    duplicated data is only safe when something is verifying it.

    Direct versus group is **derived** — a UUID parses, a base64 group id does not —
    so config cannot contradict itself about which a conversation is.
    """

    label: str
    id: str
    members: frozenset[str]
    agent: str
    profile: str
    senders: dict[str, Sender]
    purpose: str | None = None

    @property
    def is_group(self) -> bool:
        return _is_group(self.id)

    @property
    def recipient(self) -> Recipient:
        return Recipient(id=self.id, group=self.is_group)


@dataclass(frozen=True)
class Binding:
    """What one agent is: the single conversation it serves, and its profile.

    An agent name may appear in exactly one conversation (ADR 0010), which is what
    makes this a value rather than a list — and what makes outbound `self` have one
    answer for an agent shared by several senders.
    """

    agent: str
    conversation: str
    profile: str


@dataclass(frozen=True)
class Config:
    """Everything the gate reads: where the daemon is, who is who, and as what.

    `conversations` is keyed by **identifier** — group id, or the other party's ACI
    for a one-to-one, plus any `number` alias — because that is the inbound question,
    "which room did this arrive in". `by_label` answers the outbound one, "the agent
    said `family`, where is that". `agents` answers the dispatch one, "an entry
    appeared in this outbox, whose is it". Three maps over the same rows, because
    they are asked different questions.
    """

    socket: Path
    conversations: dict[str, Conversation] = field(default_factory=dict)
    by_label: dict[str, Conversation] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    agents: dict[str, Binding] = field(default_factory=dict)
    # Where a membership-drift notice goes. Unset means log only — a gate that cannot
    # reach anyone should still refuse loudly in the journal.
    notify: str | None = None


@dataclass(frozen=True)
class Command:
    """The only shape that leaves the gate.

    It names the pair that spoke, the agent that should run and the profile that
    agent holds. It does **not** carry the tool list: a capability list in a JSON
    line is a label the runner would have to trust, and the boundary that holds is
    the credential the container was never given (ADR 0010).
    """

    timestamp: int
    conversation: str
    principal: str
    agent: str
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
    agent: str
    profile: str
    recipient: Recipient
    text: str


def _is_group(identifier: str) -> bool:
    """True for a group id, False for an ACI — decided by parsing, not by config.

    A Signal group id is base64 of 32 bytes and will not parse as a UUID; an ACI
    always will. One stdlib call therefore settles which JSON-RPC parameter an
    outbound send uses, with nothing in config able to disagree with itself.
    """
    try:
        uuidlib.UUID(identifier)
    except ValueError:
        return True
    return False


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


def decide(
    notification: object, config: Config, refusing: frozenset[str] = frozenset()
) -> Command | str:
    """A `Command` to forward, or a short reason it was dropped.

    Order matters only for what the log says; every rule is a hard refusal. The
    reasons are kept distinct on purpose — `sender` is a stranger at the door,
    `unlisted sender` is someone already inside an allowlisted room trying the
    handle, and those two want different reactions from whoever reads the counters.
    """
    if _field(notification, "method") != "receive":
        return "not a receive"

    envelope = _field(notification, "params", "envelope")

    # `sourceUuid` is the identifier that actually arrives: current Signal does not
    # share phone numbers by default, so `sourceNumber` is null on real traffic
    # (hardware, 2026-08-11) and a number-keyed allowlist matches nothing. Both are
    # accepted because a contact who does share a number sends both. `sourceName` is
    # NOT consulted at any point — it is a profile name its owner chooses.
    identifiers = [
        value
        for key in ("sourceUuid", "sourceNumber", "source")
        if isinstance(value := _field(envelope, key), str)
    ]

    group_id = _field(envelope, "dataMessage", "groupInfo", "groupId")
    if isinstance(group_id, str):
        conversation = config.conversations.get(group_id)
        if conversation is None or not conversation.is_group:
            return "group"
    else:
        conversation = next(
            (
                found
                for identifier in identifiers
                if (found := config.conversations.get(identifier)) is not None
                and not found.is_group
            ),
            None,
        )
        if conversation is None:
            return "sender"

    # Before the sender is even looked up: a drifted room is refused for everyone in
    # it, including people who were listed before the membership changed.
    if conversation.id in refusing:
        return "membership"

    sender = next(
        (listed for identifier in identifiers if (listed := conversation.senders.get(identifier))),
        None,
    )
    if sender is None:
        return "unlisted sender"

    body = _field(envelope, "dataMessage", "message")
    if not isinstance(body, str) or not body.strip():
        return "no body"

    timestamp = _field(envelope, "timestamp")
    quoted = _field(envelope, "dataMessage", "quote", "id")
    reply_to = quoted if isinstance(quoted, int) else None

    # A confirmation room holds no commands at all, only answers to prompts the
    # assistant sent. Nothing here is authority-bearing unless it names the thing it
    # is authorising, which is ADR 0008's whole argument against "newest wins".
    if conversation.purpose == "confirmation" and reply_to is None:
        return "not a confirmation"

    return Command(
        timestamp=timestamp if isinstance(timestamp, int) else 0,
        conversation=conversation.label,
        principal=sender.name,
        agent=sender.agent,
        profile=sender.profile,
        body=body.strip(),
        reply_to=reply_to,
    )


def _require_name(value: object, what: str, path: Path) -> str:
    """A name that is about to become a unit instance or a directory, or a refusal."""
    if not isinstance(value, str) or not NAME_PATTERN.match(value):
        raise ValueError(
            f"{what} in {path} must be lowercase letters, digits and dashes "
            f"(it becomes a systemd unit and a directory name)"
        )
    return value


def _load_profiles(raw: Mapping[str, object], path: Path) -> dict[str, Profile]:
    """The grant bundles, checked against the tool vocabulary.

    A tool name that is not in the registry refuses to start. The alternative is a
    profile that looks like it grants something and grants nothing, which is the
    failure that is invisible until the day someone relies on it.
    """
    section: object = raw.get("agent")
    entries: object = _field(section, "profiles")
    profiles: dict[str, Profile] = {}
    for entry in entries if isinstance(entries, list) else []:
        name = _field(entry, "name")
        if not isinstance(name, str):
            raise ValueError(f"a profile in {path} has no name")
        if name in profiles:
            raise ValueError(f"two profiles named {name} in {path}")

        listed: object = _field(entry, "tools")
        if listed is None:
            tools: tuple[str, ...] = ()
        elif isinstance(listed, list) and all(isinstance(item, str) for item in listed):
            tools = tuple(item for item in listed if isinstance(item, str))
        else:
            raise ValueError(f"tools for profile {name} in {path} must be a list of names")
        for tool in tools:
            if tool not in TOOLS:
                raise ValueError(f"profile {name} in {path} grants {tool}, which is not a tool")

        allowed: object = _field(entry, "send_to")
        if allowed is None:
            send_to: tuple[str, ...] = ("self",)
        elif isinstance(allowed, list) and all(isinstance(item, str) for item in allowed):
            send_to = tuple(item for item in allowed if isinstance(item, str))
        else:
            raise ValueError(f"send_to for profile {name} in {path} must be a list of labels")

        profiles[name] = Profile(name=name, tools=tools, send_to=send_to)
    return profiles


def _load_conversation(
    entry: object, profiles: Mapping[str, Profile], path: Path
) -> tuple[Conversation, dict[str, Sender]]:
    """One conversation and its identifier keys, or a refusal.

    The returned map is keyed by every identifier that may address this room's
    senders — the uuid, and the optional `number` a human can check by eye.
    """
    label = _field(entry, "label")
    identifier = _field(entry, "id")
    if not isinstance(label, str) or not isinstance(identifier, str):
        raise ValueError(f"a conversation in {path} needs a label and an id")

    default_agent = _require_name(_field(entry, "agent"), f"conversation {label}'s agent", path)
    default_profile = _field(entry, "profile")
    if not isinstance(default_profile, str):
        raise ValueError(f"conversation {label} in {path} needs a profile")

    listed: object = _field(entry, "members")
    if not (isinstance(listed, list) and listed and all(isinstance(item, str) for item in listed)):
        raise ValueError(f"conversation {label} in {path} needs a pinned members list")
    members = frozenset(item for item in listed if isinstance(item, str))

    purpose = _field(entry, "purpose")
    if purpose is not None and not isinstance(purpose, str):
        raise ValueError(f"purpose for conversation {label} in {path} must be a string")

    senders: dict[str, Sender] = {}
    keys: dict[str, Sender] = {}
    rows: object = _field(entry, "senders")
    for row in rows if isinstance(rows, list) else []:
        name = _require_name(_field(row, "name"), f"a sender name in conversation {label}", path)
        agent = _field(row, "agent")
        agent_name = (
            default_agent
            if agent is None
            else _require_name(agent, f"sender {name}'s agent", path)
        )
        profile = _field(row, "profile")
        if profile is None:
            profile = default_profile
        if not isinstance(profile, str):
            raise ValueError(f"profile for sender {name} in {path} must be a name")
        if profile not in profiles:
            raise ValueError(f"sender {name} in {path} has profile {profile}, which is not defined")

        sender = Sender(name=name, agent=agent_name, profile=profile)
        identifiers = [_field(row, "uuid"), _field(row, "number")]
        if not any(isinstance(key, str) for key in identifiers):
            raise ValueError(f"sender {name} in {path} needs a uuid or a number")
        for key in identifiers:
            if isinstance(key, str):
                senders[key] = sender
                keys[key] = sender

    if not senders:
        raise ValueError(f"conversation {label} in {path} lists no senders")

    conversation = Conversation(
        label=label,
        id=identifier,
        members=members,
        agent=default_agent,
        profile=default_profile,
        senders=senders,
        purpose=purpose if isinstance(purpose, str) else None,
    )

    # A one-to-one duplicates its id into `members` and into its single sender row.
    # That duplication is only safe while something checks the copies agree.
    if not conversation.is_group:
        rows_seen = {sender.name for sender in senders.values()}
        if members != {identifier} or len(rows_seen) != 1:
            raise ValueError(
                f"conversation {label} in {path} has a uuid for an id, so it is a one-to-one: "
                f"members must be exactly [id] and it must list exactly one sender"
            )
        if identifier not in senders:
            raise ValueError(
                f"conversation {label} in {path} is a one-to-one, so its sender's uuid "
                f"must be the conversation id"
            )

    return conversation, keys


def load_config(path: Path) -> Config:
    """Read `[signal]` and `[agent]`: the socket, the rooms, and the grants.

    A missing file raises and an empty conversation table raises: running with no
    conversations would mean either "accept everyone", which is not an allowlist, or
    "accept nobody", which is a silently dead assistant.

    Four refusals here are the ones doing security work, and each is a config
    mistake that would otherwise read as a quietly different grant:

    * **A duplicate pair name or conversation label**, because then `to: "family"`
      has two answers and the config does not say which.
    * **An agent named in two conversations.** Within one room a shared agent costs
      nothing already shared — everyone reads everyone's messages — but a session
      spanning two rooms carries one room's text into the other and lets an injection
      there act with the other's credentials (ADR 0010).
    * **Senders resolving to one agent but different profiles.** An agent *is* its
      tools and its mounted credentials; switching capability per speaker inside one
      container would be a runtime string check, which is exactly the enforcement
      this design removes. It is also what catches the half-override — a sender given
      a profile but no agent of their own.
    * **A `send_to` naming a conversation that does not exist**, since the addressable
      set is the conversation table and a typo would otherwise read as a silently
      narrower grant.
    """
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    section: object = raw.get("signal")
    if not isinstance(section, dict):
        raise ValueError(f"no [signal] section in {path}")

    socket_value: object = section.get("socket")
    socket_path = Path(socket_value) if isinstance(socket_value, str) else DEFAULT_SOCKET

    profiles = _load_profiles(raw, path)

    conversations: dict[str, Conversation] = {}
    by_label: dict[str, Conversation] = {}
    agents: dict[str, Binding] = {}
    pair_names: set[str] = set()

    entries: object = section.get("conversations")
    for entry in entries if isinstance(entries, list) else []:
        conversation, keys = _load_conversation(entry, profiles, path)
        if conversation.label in by_label:
            raise ValueError(f"two conversations labelled {conversation.label} in {path}")
        if conversation.id in conversations:
            raise ValueError(f"two conversations share an id in {path}")
        by_label[conversation.label] = conversation
        conversations[conversation.id] = conversation
        # A number alias reaches the same one-to-one. Group ids are never aliased:
        # the only thing that addresses a group is its id.
        if not conversation.is_group:
            for key in keys:
                conversations.setdefault(key, conversation)

        for sender in set(conversation.senders.values()):
            if sender.name in pair_names:
                raise ValueError(f"two senders named {sender.name} in {path} — a name must be one")
            pair_names.add(sender.name)

            bound = agents.get(sender.agent)
            if bound is None:
                agents[sender.agent] = Binding(
                    agent=sender.agent,
                    conversation=conversation.label,
                    profile=sender.profile,
                )
                continue
            if bound.conversation != conversation.label:
                raise ValueError(
                    f"agent {sender.agent} in {path} is used in both {bound.conversation} "
                    f"and {conversation.label} — an agent session may not span conversations"
                )
            if bound.profile != sender.profile:
                raise ValueError(
                    f"agent {sender.agent} in {path} would hold both {bound.profile} and "
                    f"{sender.profile} — senders sharing an agent share its profile, so give "
                    f"{sender.name} an agent of its own"
                )

    if not conversations:
        raise ValueError(f"no [[signal.conversations]] in {path} — the gate would accept nobody")

    for profile in profiles.values():
        for target in profile.send_to:
            if target != "self" and target not in by_label:
                raise ValueError(
                    f"profile {profile.name} in {path} may send to {target}, "
                    f"which is not a conversation"
                )

    notify_value: object = section.get("notify")
    if notify_value is not None and (
        not isinstance(notify_value, str) or notify_value not in by_label
    ):
        raise ValueError(f"notify in {path} must name a conversation")

    return Config(
        socket=socket_path,
        conversations=conversations,
        by_label=by_label,
        profiles=profiles,
        agents=agents,
        notify=notify_value if isinstance(notify_value, str) else None,
    )


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


def resolve(to: str, binding: Binding, config: Config) -> Recipient | None:
    """Where `to` goes, or None if this agent may not address it.

    The label is checked against the send list *before* it is looked up, so an
    unlisted label and an unknown one are the same refusal — the agent learns
    nothing about who exists from which way it was told no.

    `self` is the conversation the agent serves, which is well defined even when
    several senders share the agent, because an agent may not span conversations.
    In a group that means the reply lands in the group: disclosed to everyone in the
    room, which is exactly why a group profile is the narrower one (ADR 0008).
    """
    profile = config.profiles.get(binding.profile)
    if profile is None or to not in profile.send_to:
        return None
    conversation = config.by_label.get(binding.conversation if to == "self" else to)
    return conversation.recipient if conversation is not None else None


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
    for agent, binding in config.agents.items():
        # Dotfiles are skipped so the write-then-rename the agent does is never
        # read half-written; pathlib's glob matches them, unlike the shell's.
        entries = sorted(
            path for path in (outbox / agent).glob("*.json") if not path.name.startswith(".")
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
            recipient = resolve(to, binding, config)
            if recipient is None:
                _refuse("not in list", refused)
                continue
            ready.append(
                Outbound(
                    entry=path.stem,
                    agent=agent,
                    profile=binding.profile,
                    recipient=recipient,
                    text=text,
                )
            )
    return ready


def _refuse(reason: str, refused: dict[str, int]) -> None:
    """Count it and say so — with no identifier, and not the requested label either.

    The label came out of a file an agent wrote. It is free text, chosen by the
    least trusted process here, on its way to journald (threat model R4).
    """
    refused[reason] = refused.get(reason, 0) + 1
    _log(f"refused send: {reason} ({refused[reason]} total)")


def _prepare(outbox: Path, config: Config) -> None:
    """One directory per agent, gate-owned, made before anything writes.

    Keyed by agent and not by pair, because the mailbox belongs to the container:
    senders who share an agent share its outbox, which is the same thing as sharing
    its session.

    0700 today because no agent user exists yet to grant. NVB-14/15 gives each agent
    its own group (`wpa-out-<agent>`) and mode 0730 — write and execute so the agent
    can create and rename, no read so it cannot enumerate what else is queued. **One
    group per agent, never one shared group**: a shared one lets agent A drop an
    entry in agent B's directory and send under B's send list, which is ADR 0009's
    isolation undone by a group name.
    """
    for agent in config.agents:
        directory = outbox / agent
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)  # mkdir's mode is masked by umask; this is not


def _members_of(group: object) -> frozenset[str] | None:
    """The member identifiers in one `listGroups` entry, whatever shape they took.

    **The shape is unverified against hardware** — the assistant was in no group when
    this was written, so `listGroups` returned `[]` and there was nothing to capture.
    Both plausible encodings are therefore accepted: a list of strings, and a list of
    objects carrying `uuid` and/or `number`. Anything else returns None, which the
    caller treats as "cannot confirm", and refuses. Fail closed is the only safe
    reading of a response we do not understand.
    """
    listed = _field(group, "members")
    if not isinstance(listed, list):
        return None
    members: set[str] = set()
    for member in listed:
        if isinstance(member, str):
            members.add(member)
            continue
        for key in ("uuid", "number"):
            value = _field(member, key)
            if isinstance(value, str):
                members.add(value)
    return frozenset(members) if members else None


def _drifted(response: object, config: Config) -> frozenset[str]:
    """Which group conversations must refuse, given a `listGroups` result.

    A group is refused when the daemon does not list it at all, when its member list
    cannot be read, or when that list differs from the pinned set in either
    direction. Someone leaving matters as much as someone joining: the pinned set is
    the room the owner allowlisted, and a different room is not it.
    """
    live: dict[str, frozenset[str] | None] = {}
    results = _field(response, "result")
    for group in results if isinstance(results, list) else []:
        identifier = _field(group, "id")
        if isinstance(identifier, str):
            live[identifier] = _members_of(group)

    return frozenset(
        conversation.id
        for conversation in config.by_label.values()
        if conversation.is_group and live.get(conversation.id) != conversation.members
    )


def _send(conn: socket.socket, request_id: str, recipient: Recipient, message: str) -> None:
    """One JSON-RPC `send`, addressed as a group or as a person.

    Which parameter carries the address is derived from the identifier itself, so a
    group can never be addressed as a recipient by a config that says the wrong
    thing about what it is.
    """
    target: dict[str, object] = (
        {"groupId": recipient.id} if recipient.group else {"recipient": [recipient.id]}
    )
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "send",
        "params": {**target, "message": message},
    }
    conn.sendall((json.dumps(request) + "\n").encode())


def _ack(conn: socket.socket, recipient: Recipient, command: Command) -> Outbound:
    """Tell the conversation the command was accepted, and which one.

    The timestamp is the handle: a later reply quoting this ack is how NVB-16 will
    match a confirmation to its pending action. Which is why the ack is registered
    for response capture like any other send — the message a person quotes when they
    answer YES is usually this one.

    It goes to the conversation, not to the sender, so that in a group the message a
    confirmation quotes exists in the room where the confirmation will be typed.

    ponytail: still the placeholder `ack <timestamp>` from NVB-10. It becomes real
    text when a runner exists to have an opinion about what it says (NVB-15).
    """
    request_id = f"ack-{command.timestamp}"
    message = f"ack {command.timestamp}"
    _send(conn, request_id, recipient, message)
    return Outbound(
        entry=request_id,
        agent=command.agent,
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
                "agent": outbound.agent,
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


@dataclass
class Membership:
    """What the gate believes about who is in each group, and what it has said so far.

    Starts refusing **every** group and learns otherwise, so the window between
    connecting and the daemon answering is closed rather than open. An unanswered
    daemon must never read as "membership is fine".
    """

    refusing: frozenset[str]
    announced: frozenset[str] = frozenset()
    requests: int = 0

    @classmethod
    def closed(cls, config: Config) -> Membership:
        return cls(
            refusing=frozenset(
                conversation.id for conversation in config.by_label.values() if conversation.is_group
            )
        )


def _request_members(conn: socket.socket, membership: Membership) -> str:
    """Ask the daemon who is in the groups. The id is how the answer is recognised."""
    membership.requests += 1
    request_id = f"groups-{membership.requests}"
    request = {"jsonrpc": "2.0", "id": request_id, "method": "listGroups"}
    conn.sendall((json.dumps(request) + "\n").encode())
    return request_id


def _apply_members(
    response: object,
    conn: socket.socket,
    config: Config,
    membership: Membership,
    awaiting: dict[str, Outbound],
) -> None:
    """Update what is refused, and tell the owner once about anything newly refused.

    Once, not every fifteen minutes: a drift that keeps being re-announced trains
    whoever reads it to ignore the notice. And a notice is not optional — a refusing
    group is otherwise indistinguishable from a quiet one, which is the difference
    between "this stopped working and you were told" and a silence you notice in a
    week (ADR 0008).
    """
    membership.refusing = _drifted(response, config)
    for identifier in sorted(membership.refusing - membership.announced):
        label = next(
            (
                conversation.label
                for conversation in config.by_label.values()
                if conversation.id == identifier
            ),
            "?",
        )
        _log(f"membership drift: {label} — commands from it are refused")
        if config.notify is None:
            continue
        target = config.by_label[config.notify]
        request_id = f"drift-{label}-{membership.requests}"
        message = f"{label}: membership changed, commands from it are refused"
        _send(conn, request_id, target.recipient, message)
        _await(
            awaiting,
            request_id,
            Outbound(
                entry=request_id,
                agent=target.agent,
                profile=target.profile,
                recipient=target.recipient,
                text=message,
            ),
        )
    # Cleared drifts are forgettable: if it happens again it is news again.
    membership.announced = membership.refusing


def run(
    config: Config,
    commands: Path,
    *,
    outbox: Path | None = None,
    sent: Path | None = None,
    cycles: int = 0,
) -> None:
    """Read the receive stream forever, forwarding what passes and counting the rest.

    The same loop drains the outbox and re-reads group membership, which is why it is
    a `select` with a timeout rather than iterating `conn.makefile("r")`: that call
    blocks, and a blocked read cannot also poll a directory or run a clock.

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
        # A fresh connection knows nothing about membership, including after the
        # daemon restarted and possibly missed group updates while it was down.
        membership = Membership.closed(config)
        members_due = 0.0
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
                        if _handle(
                            raw, conn, config, commands, sent, dropped, refused, awaiting, membership
                        ):
                            members_due = 0.0  # a group changed: ask again now
                    # Checked on what is *left*, not on what arrived: a large batch
                    # of complete lines is ordinary, one endless line is not.
                    if len(buffer) > MAX_BUFFER_BYTES:
                        _log("dropped: line longer than the buffer")
                        break

                now = time.monotonic()
                if now >= members_due:
                    members_due = now + MEMBERS_REFRESH_SECONDS
                    _request_members(conn, membership)

                if now >= due:
                    # By the clock rather than only on an idle `select`, so a busy
                    # receive stream cannot starve the outbox indefinitely.
                    due = now + POLL_SECONDS
                    for outbound in drain(outbox, config, refused):
                        request_id = f"send-{outbound.entry}"
                        _send(conn, request_id, outbound.recipient, outbound.text)
                        _await(awaiting, request_id, outbound)
                        _log(
                            f"sent agent={outbound.agent} "
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
    membership: Membership,
) -> bool:
    """One line off the wire. True if it is a reason to re-read group membership.

    That return is the difference between catching an added member in seconds and
    catching them within the refresh interval: a group update arrives on this same
    stream, so the cheapest possible trigger is already in hand.
    """
    if not raw.strip():
        return False
    try:
        notification: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _log("dropped: unparseable line")
        return False

    # Objects with no `method` are JSON-RPC responses — our own requests coming back.
    # Not decisions, so not counted as drops; the timestamp in a send response is what
    # lets a later confirmation be matched to the message it answers.
    if _field(notification, "method") is None:
        request_id = _field(notification, "id")
        if isinstance(request_id, str) and request_id.startswith("groups-"):
            _apply_members(notification, conn, config, membership, awaiting)
        else:
            _record(notification, awaiting, sent, refused)
        return False

    verdict = decide(notification, config, membership.refusing)
    if isinstance(verdict, str):
        dropped[verdict] = dropped.get(verdict, 0) + 1
        _log(f"dropped: {verdict} ({dropped[verdict]} total)")
        return _is_group_update(notification, config)

    with commands.open("a", encoding="utf-8") as fh:
        json.dump(asdict(verdict), fh, ensure_ascii=False)
        fh.write("\n")
    _log(
        f"accepted conversation={verdict.conversation} principal={verdict.principal} "
        f"agent={verdict.agent} profile={verdict.profile} "
        f"len={len(verdict.body)} reply_to={verdict.reply_to}"
    )
    # Reply to the conversation it came from, taken off the envelope rather than out
    # of config: the message got here, so this address is known good.
    recipient = _recipient_of(notification)
    if recipient is not None:
        _await(awaiting, f"ack-{verdict.timestamp}", _ack(conn, recipient, verdict))
    return _is_group_update(notification, config)


def _recipient_of(notification: object) -> Recipient | None:
    """The conversation an envelope arrived in, as somewhere to send a reply."""
    envelope = _field(notification, "params", "envelope")
    group_id = _field(envelope, "dataMessage", "groupInfo", "groupId")
    if isinstance(group_id, str):
        return Recipient(id=group_id, group=True)
    source = _field(envelope, "source")
    return Recipient(id=source, group=False) if isinstance(source, str) else None


def _is_group_update(notification: object, config: Config) -> bool:
    """Whether this envelope suggests a group's membership just changed.

    A delivered message carries `groupInfo.type == "DELIVER"`; anything else in that
    field is some flavour of group change. **Unverified against hardware** — no group
    existed to produce one — so this is treated as a hint that costs one `listGroups`
    if it is wrong, and the 15-minute refresh remains the guarantee if it never
    fires. Only groups the gate knows about can trigger it, so a stranger's group
    cannot be used to make the gate chat to the daemon.
    """
    envelope = _field(notification, "params", "envelope")
    group_id = _field(envelope, "dataMessage", "groupInfo", "groupId")
    if not isinstance(group_id, str) or group_id not in config.conversations:
        return False
    return _field(envelope, "dataMessage", "groupInfo", "type") != "DELIVER"


def check(config: Config) -> str:
    """The resolved matrix: who may command what, as whom, with which tools.

    This is the artefact for the question "what can this thing actually do", asked
    before a restart or asked by a family member. It is generated from the same
    structures the gate runs on, so it cannot drift from them the way a hand-written
    table would.
    """
    lines: list[str] = []
    for label, conversation in config.by_label.items():
        kind = "group" if conversation.is_group else "direct"
        purpose = f" purpose={conversation.purpose}" if conversation.purpose else ""
        lines.append(f"{label} ({kind}, {len(conversation.members)} pinned members){purpose}")
        for sender in sorted(set(conversation.senders.values()), key=lambda row: row.name):
            profile = config.profiles[sender.profile]
            lines.append(f"  {sender.name}")
            lines.append(f"    agent   {sender.agent}  [{agent_unit(sender.agent)}]")
            lines.append(f"    profile {sender.profile}")
            for tool in profile.tools:
                entry = TOOLS[tool]
                credential = entry.credential or "none"
                lines.append(f"      {tool:<32} {credential:<18} {entry.label}")
            if not profile.tools:
                lines.append("      (no tools — it can answer, and nothing else)")
            lines.append(f"    may message {', '.join(profile.send_to)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    checking = "--check" in args
    args = [arg for arg in args if arg != "--check"]

    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    commands = Path(args[1]) if len(args) > 1 else DEFAULT_COMMANDS

    config = load_config(config_path)
    if checking:
        print(check(config))
        return 0

    _log(f"gate up: {len(config.by_label)} conversation(s), {len(config.agents)} agent(s)")
    run(config, commands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
