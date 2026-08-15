"""Tests for the Signal trigger gate.

The fixtures in `fixtures/signal/` are real envelopes off the daemon's socket,
redacted (see the README there). That matters more than usual here: the hole this
gate closes — a typing indicator arriving as a `receive` notification with no
`dataMessage` — is not in signal-cli's documentation. It was found by watching the
wire, so the test that it stays closed reads from the wire too.

The group fixtures were captured on 2026-08-12 once a group existed, and they moved
two guesses into facts: a `listGroups` member carries **both** a uuid and a number,
and a group change arrives as `groupInfo.type == "UPDATE"` with `message: null` — so
it is dropped as `no body` and the membership re-read has to happen on the drop path.
Only the sender identity is edited on three of them, because there was still only one
other person to send from.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from gate.signal import (
    Command,
    Config,
    Outbound,
    Recipient,
    _drifted,
    _record,
    check,
    decide,
    drain,
    load_config,
    resolve,
    run,
)

FIXTURES = Path(__file__).parent / "fixtures" / "signal"

OWNER_UUID = "11111111-1111-4111-8111-111111111111"
MOM_UUID = "22222222-2222-4222-8222-222222222222"
DAD_UUID = "44444444-4444-4444-8444-444444444444"
STRANGER_UUID = "99999999-9999-4999-8999-999999999999"
GROUP_ID = "Z3JvdXAtaWQtcmVkYWN0ZWQ="

# One world, used by most tests and built through the real parser rather than by
# constructing dataclasses — the validation in `load_config` is half of what this
# module is testing, so bypassing it would test a config shape nothing accepts.
#
# It is deliberately the interesting arrangement: the owner has a private chat, the
# family group runs one shared agent for everyone in it, and the owner overrides that
# in the group to get their own session with a wider profile.
WORLD = f"""
[signal]
socket = "/tmp/sock"

[[agent.profiles]]
name = "owner-full"
tools = ["whatsapp.read", "calendar.personal.rw", "email.personal.draft"]
send_to = ["self", "family"]

[[agent.profiles]]
name = "family-shared"
tools = ["whatsapp.read", "calendar.family.rw"]

[[agent.profiles]]
name = "calendar-only"
tools = ["calendar.family.create_event"]

[[signal.conversations]]
label = "owner-1to1"
id = "{OWNER_UUID}"
members = ["{OWNER_UUID}"]
agent = "owner"
profile = "owner-full"
[[signal.conversations.senders]]
uuid = "{OWNER_UUID}"
number = "+15555550100"
name = "owner"

[[signal.conversations]]
label = "family"
id = "{GROUP_ID}"
members = ["{OWNER_UUID}", "{MOM_UUID}", "{DAD_UUID}"]
agent = "family"
profile = "calendar-only"
[[signal.conversations.senders]]
uuid = "{MOM_UUID}"
name = "mom-family"
[[signal.conversations.senders]]
uuid = "{DAD_UUID}"
name = "dad-family"
[[signal.conversations.senders]]
uuid = "{OWNER_UUID}"
name = "owner-family"
agent = "owner-family"
profile = "family-shared"
"""


def written(tmp_path: Path, body: str) -> Config:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(path)


@pytest.fixture
def world(tmp_path: Path) -> Config:
    return written(tmp_path, WORLD)


def envelope(name: str) -> object:
    parsed: object = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return parsed


def test_a_message_from_a_principal_is_a_command(world: Config) -> None:
    verdict = decide(envelope("message"), world)
    assert isinstance(verdict, Command)
    assert verdict.conversation == "owner-1to1"
    assert verdict.principal == "owner"
    assert verdict.profile == "owner-full"
    assert verdict.agent == "owner"
    assert verdict.body == "ping"
    assert verdict.reply_to is None


def test_a_permitted_sender_in_a_group_is_a_command(world: Config) -> None:
    """The refusal NVB-12 exists to lift: a group has no single sender, but a
    (conversation, sender) pair does."""
    verdict = decide(envelope("group-family"), world)
    assert isinstance(verdict, Command)
    assert verdict.conversation == "family"
    assert verdict.principal == "mom-family"
    assert verdict.profile == "calendar-only"
    assert verdict.agent == "family"


def test_the_same_person_is_two_principals_in_two_conversations(world: Config) -> None:
    """ADR 0008's whole claim, as an assertion. The owner in the family group is not
    the owner in their own chat, and the emitted command says which applied — a group
    reply is read by everyone in the room, so it must be the narrower grant."""
    private = decide(envelope("message"), world)
    shared = decide(envelope("group"), world)
    assert isinstance(private, Command) and isinstance(shared, Command)
    assert (private.principal, private.profile) == ("owner", "owner-full")
    assert (shared.principal, shared.profile) == ("owner-family", "family-shared")
    assert private.agent != shared.agent, "two sessions, so the group cannot see the private one"


def test_senders_may_share_one_agent_and_still_be_told_apart(world: Config) -> None:
    """A family agent anyone in the room can activate. Sharing a session is allowed
    inside one conversation; attribution survives it because the pair name travels
    with the command."""
    mom = decide(envelope("group-family"), world)
    assert isinstance(mom, Command)
    assert (mom.agent, mom.principal) == ("family", "mom-family")
    assert world.agents["family"].conversation == "family"


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("typing", "no body"),
        ("receipt", "no body"),
        ("stranger", "sender"),
        ("family", "sender"),
        ("group-unknown", "group"),
        ("group-stranger", "unlisted sender"),
    ],
)
def test_everything_else_is_dropped(world: Config, fixture: str, reason: str) -> None:
    """Four distinct counters, because they want four different reactions. `sender`
    is a stranger at the door; `unlisted sender` is someone already inside an
    allowlisted room trying the handle, which is where probing in a family group
    shows up."""
    assert decide(envelope(fixture), world) == reason


def test_a_drifted_group_refuses_everyone_including_listed_senders(world: Config) -> None:
    """Refuses rather than degrades: the person added is not the only one who stops
    being able to use it, because nobody can know what the extra member saw."""
    assert decide(envelope("group-family"), world, frozenset({GROUP_ID})) == "membership"
    assert decide(envelope("group"), world, frozenset({GROUP_ID})) == "membership"
    # The owner's own chat is untouched: drift is a property of one room.
    assert isinstance(decide(envelope("message"), world, frozenset({GROUP_ID})), Command)


def test_a_reply_remembers_what_it_answered(world: Config) -> None:
    """A `YES` is a command, but only useful if it still points at the action it
    authorises. The registry is NVB-16's; not losing the link is this gate's job."""
    verdict = decide(envelope("quote-reply"), world)
    assert isinstance(verdict, Command)
    assert verdict.reply_to == 1754912300000


def test_a_confirmation_room_holds_no_commands_at_all(tmp_path: Path) -> None:
    """Nothing in a confirmation conversation is authority-bearing unless it names
    the thing it authorises. By construction, not by an agent choosing well."""
    confirmations = written(tmp_path, WORLD.replace('label = "family"', 'label = "family"\npurpose = "confirmation"'))
    assert decide(envelope("group"), confirmations) == "not a confirmation"
    accepted = decide(envelope("group-quote-reply"), confirmations)
    assert isinstance(accepted, Command)
    # The timestamp of the assistant's own message, captured off a real quoted reply:
    # this is the handle a pending action is matched on (NVB-16).
    assert accepted.reply_to == 1786482619131


def test_junk_never_raises_its_way_past_a_check(world: Config) -> None:
    junk: list[object] = [None, [], "receive", {"method": "receive"}, {"params": 3}]
    for item in junk:
        assert isinstance(decide(item, world), str)


# --- What the config refuses to start with --------------------------------------


def test_a_config_with_no_conversations_refuses_to_start(tmp_path: Path) -> None:
    """An allowlist that defaults to everyone is not an allowlist, and one that
    defaults to nobody is a dead assistant that looks alive. Both are refusals."""
    with pytest.raises(ValueError):
        written(tmp_path, '[signal]\nsocket = "/run/wpa-signal/socket"\n')


def test_a_sender_row_inherits_its_conversations_agent_and_profile(world: Config) -> None:
    """The common case is meant to cost one line per person: a family group is one
    agent and one profile, and everybody in it inherits both."""
    family = world.by_label["family"]
    assert family.senders[MOM_UUID].agent == "family"
    assert family.senders[MOM_UUID].profile == "calendar-only"


def test_a_number_alias_reaches_the_same_conversation(world: Config) -> None:
    """The uuid is what arrives; the number is the part a human can check by eye."""
    assert world.conversations["+15555550100"] is world.conversations[OWNER_UUID]


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        pytest.param(
            ('agent = "family"\nprofile = "calendar-only"', 'profile = "calendar-only"'),
            "agent",
            id="conversation with no agent",
        ),
        pytest.param(
            ('name = "mom-family"', 'name = "mom-family"\nprofile = "family-shared"'),
            "share",
            id="one agent, two profiles",
        ),
        pytest.param(
            ('agent = "owner-family"', 'agent = "owner"'),
            "span conversations",
            id="one agent, two conversations",
        ),
        pytest.param(
            ('name = "mom-family"', 'name = "Mom Family"'),
            "lowercase",
            id="a name that is not a safe directory or unit",
        ),
        pytest.param(
            ('tools = ["calendar.family.create_event"]', 'tools = ["calendar.family.delete_all"]'),
            "not a tool",
            id="a tool that does not exist",
        ),
        pytest.param(
            ('send_to = ["self", "family"]', 'send_to = ["self", "the-plumber"]'),
            "the-plumber",
            id="a send list naming nobody",
        ),
        pytest.param(
            (f'members = ["{OWNER_UUID}"]', f'members = ["{OWNER_UUID}", "{MOM_UUID}", "{DAD_UUID}"]'),
            "one-to-one",
            id="a direct conversation whose duplicated halves disagree",
        ),
        pytest.param(
            ('name = "owner-family"', 'name = "owner"'),
            "two senders named owner",
            id="two pairs with one name",
        ),
    ],
)
def test_a_config_mistake_refuses_to_start(
    tmp_path: Path, edit: tuple[str, str], expected: str
) -> None:
    """Each of these would otherwise read as a silently different grant, which is
    the failure that stays invisible until the day somebody relies on it."""
    before, after = edit
    assert before in WORLD, "the edit must actually apply"
    with pytest.raises(ValueError, match=expected):
        written(tmp_path, WORLD.replace(before, after, 1))


# --- Membership pinning ---------------------------------------------------------


def _listed(members: Sequence[object], group: str = GROUP_ID, **extra: object) -> object:
    entry: dict[str, object] = {"id": group, "isMember": True, "members": list(members)}
    entry.update(extra)
    return {"jsonrpc": "2.0", "id": "groups-1", "result": [entry]}


def test_membership_matching_the_pinned_set_refuses_nothing(world: Config) -> None:
    """The object form is the real one, captured on hardware 2026-08-12. The bare
    string form is still accepted because it costs one branch."""
    as_objects = [
        {"uuid": uuid, "number": None, "isAdmin": False}
        for uuid in (OWNER_UUID, MOM_UUID, DAD_UUID)
    ]
    assert _drifted(_listed([OWNER_UUID, MOM_UUID, DAD_UUID]), world) == frozenset()
    assert _drifted(_listed(as_objects), world) == frozenset()


def test_a_member_with_both_a_uuid_and_a_number_counts_once(world: Config) -> None:
    """The captured shape carries both for anyone who shares their number — the
    assistant's own row did. Taking both would mean a pinned list has to name a
    person twice to match, which nobody would write; the uuid wins."""
    both = [
        {"uuid": OWNER_UUID, "number": "+15555550100", "isAdmin": True},
        {"uuid": MOM_UUID, "number": "+15555550101", "isAdmin": False},
        {"uuid": DAD_UUID, "number": None, "isAdmin": False},
    ]
    assert _drifted(_listed(both), world) == frozenset()


def test_being_removed_from_the_group_is_drift(world: Config) -> None:
    """`isMember: false` still lists the members. Reading it as "membership matches"
    would leave a room the assistant is no longer in looking perfectly healthy."""
    members = [OWNER_UUID, MOM_UUID, DAD_UUID]
    assert _drifted(_listed(members, isMember=False), world) == frozenset({GROUP_ID})


@pytest.mark.parametrize(
    ("response", "why"),
    [
        pytest.param(_listed([OWNER_UUID, MOM_UUID, DAD_UUID, STRANGER_UUID]), "someone joined", id="joined"),
        pytest.param(_listed([OWNER_UUID, MOM_UUID]), "someone left", id="left"),
        pytest.param(_listed([], group="b3RoZXI="), "the group is not listed", id="absent"),
        pytest.param({"jsonrpc": "2.0", "id": "groups-1", "result": []}, "no groups", id="empty"),
        pytest.param({"error": {"code": -1}}, "the daemon refused", id="an error"),
        pytest.param(_listed([{"isAdmin": True}]), "unnameable", id="a member with no id"),
        pytest.param({"result": [{"id": GROUP_ID}]}, "no member list", id="no members key"),
    ],
)
def test_anything_other_than_the_pinned_set_refuses(
    world: Config, response: object, why: str
) -> None:
    """Fail closed in every direction, including "we could not tell". A response the
    gate does not understand must not read as "membership is fine"."""
    assert _drifted(response, world) == frozenset({GROUP_ID}), why


def test_a_one_to_one_has_nothing_to_drift(world: Config) -> None:
    """Its member set is pinned structurally — `members == [id]`, checked at load —
    so the check degenerates rather than needing a special case."""
    assert world.by_label["owner-1to1"].id not in _drifted(_listed([OWNER_UUID, MOM_UUID, DAD_UUID]), world)


# --- Outbound: the agent asks by name, the gate decides (ADR 0009) --------------


def _entry(outbox: Path, agent: str, name: str, payload: object) -> Path:
    directory = outbox / agent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_entry_is_resolved_through_the_conversation_table(
    world: Config, tmp_path: Path
) -> None:
    """The whole indirection in one assertion: the agent wrote a *label*, and what
    goes on the wire is an identifier it has never seen — here a group id, so the
    reply lands in the room rather than in somebody's private chat."""
    _entry(tmp_path, "owner", "001.json", {"to": "family", "text": "on my way"})

    refused: dict[str, int] = {}
    ready = drain(tmp_path, world, refused)

    assert ready == [
        Outbound(
            entry="001",
            agent="owner",
            profile="owner-full",
            recipient=Recipient(id=GROUP_ID, group=True),
            text="on my way",
        )
    ]
    assert refused == {}


def test_self_is_the_default_and_needs_no_configuration(world: Config, tmp_path: Path) -> None:
    """An agent granted nothing can still answer the conversation it serves — and
    reach nowhere else."""
    assert world.profiles["calendar-only"].send_to == ("self",)
    _entry(tmp_path, "family", "001.json", {"to": "self", "text": "done"})

    ready = drain(tmp_path, world, {})
    assert [(item.agent, item.recipient) for item in ready] == [
        ("family", Recipient(id=GROUP_ID, group=True))
    ]


@pytest.mark.parametrize("label", ["owner-1to1", "nobody", "", "self "])
def test_a_label_outside_the_send_list_is_refused(
    world: Config, tmp_path: Path, label: str
) -> None:
    """Unlisted and unknown are the *same* refusal: the agent must not be able to
    learn who exists by watching which way it is told no."""
    path = _entry(tmp_path, "family", "001.json", {"to": label, "text": "hello"})

    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {"not in list": 1}
    assert not path.exists(), "a refused entry is consumed, not retried forever"


def test_a_drifted_group_drops_outbound(world: Config, tmp_path: Path) -> None:
    """The egress half of the pin, and the half a notification depends on.

    `decide` already refuses commands from a drifted room, but that only silences
    *replies*. An entry with no inbound command behind it — a PR notice, a scheduled
    report — has nothing to refuse on the way in, and would otherwise land in a room
    whose membership changed. Pinned membership exists because a member added since
    the pin reads every reply put in the room, which is a claim about egress."""
    path = _entry(tmp_path, "owner", "001.json", {"to": "family", "text": "PR is up"})

    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused, frozenset({GROUP_ID})) == []
    assert refused == {"membership": 1}
    assert not path.exists(), "a refused entry is consumed, not retried forever"


def test_outbound_to_a_group_is_held_until_membership_is_known(
    world: Config, tmp_path: Path
) -> None:
    """The startup window, which is the one case an entry survives a cycle.

    Membership starts refusing every group, so before the daemon answers there is no
    way to tell a drifted room from an unasked one. Consuming here would destroy a
    notification written before a restart — delivery is at-most-once, so the file is
    the only copy — and refusing an outbound is not recoverable the way refusing a
    command is."""
    path = _entry(tmp_path, "owner", "001.json", {"to": "family", "text": "PR is up"})

    refused: dict[str, int] = {}
    unknown = frozenset({GROUP_ID})  # what Membership.closed() starts with
    assert drain(tmp_path, world, refused, unknown, known=False) == []
    assert refused == {}, "held is not refused — nothing to count and nothing to log"
    assert path.exists(), "the entry must survive to be sent once membership is known"

    # And once the daemon has answered with the pinned set, it goes.
    ready = drain(tmp_path, world, refused, frozenset(), known=True)
    assert [item.text for item in ready] == ["PR is up"]
    assert not path.exists()


def test_a_one_to_one_is_not_held_by_an_unanswered_daemon(
    world: Config, tmp_path: Path
) -> None:
    """Holding is for groups. A private chat has no membership to drift, so making it
    wait on `listGroups` would turn a slow daemon into a silent assistant."""
    _entry(tmp_path, "owner", "001.json", {"to": "self", "text": "still here"})

    ready = drain(tmp_path, world, {}, frozenset({GROUP_ID}), known=False)
    assert [item.recipient for item in ready] == [Recipient(id=OWNER_UUID, group=False)]


def test_drift_in_one_room_does_not_silence_another(world: Config, tmp_path: Path) -> None:
    """Drift is a property of one room. A drifted group must not take the owner's
    own chat down with it — that would turn a membership change into an outage."""
    _entry(tmp_path, "owner", "001.json", {"to": "self", "text": "still here"})

    ready = drain(tmp_path, world, {}, frozenset({GROUP_ID}))
    assert [item.recipient for item in ready] == [Recipient(id=OWNER_UUID, group=False)]


def test_resolution_is_pure_and_never_invents_a_recipient(world: Config) -> None:
    owner = world.agents["owner"]
    family = world.agents["family"]
    assert resolve("self", owner, world) == Recipient(id=OWNER_UUID, group=False)
    assert resolve("family", owner, world) == Recipient(id=GROUP_ID, group=True)
    assert resolve("owner-1to1", family, world) is None
    # A send list may name only conversations, so this cannot happen through
    # `load_config` — but resolution must not depend on that check having run.
    ghosted = dataclasses.replace(world.profiles["owner-full"], send_to=("ghost",))
    assert resolve("ghost", owner, dataclasses.replace(world, profiles={"owner-full": ghosted})) is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"to": "self"}, "malformed"),
        ({"text": "no recipient"}, "malformed"),
        ({"to": "self", "text": ""}, "malformed"),
        ({"to": "self", "text": "   "}, "malformed"),
        ({"to": ["self"], "text": "a list is not a name"}, "malformed"),
        ({"to": "self", "text": 7}, "malformed"),
        ([{"to": "self", "text": "hi"}], "malformed"),
    ],
)
def test_a_malformed_entry_is_refused_rather_than_raising(
    world: Config, tmp_path: Path, payload: object, reason: str
) -> None:
    _entry(tmp_path, "owner", "001.json", payload)
    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {reason: 1}


def test_unparseable_bytes_are_refused(world: Config, tmp_path: Path) -> None:
    (tmp_path / "owner").mkdir(parents=True)
    (tmp_path / "owner" / "001.json").write_bytes(b"\xff\xfe not json")
    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {"malformed": 1}


def test_an_oversize_entry_is_refused_without_being_read(world: Config, tmp_path: Path) -> None:
    (tmp_path / "owner").mkdir(parents=True)
    (tmp_path / "owner" / "001.json").write_text(
        json.dumps({"to": "self", "text": "x" * (64 * 1024)}), encoding="utf-8"
    )
    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {"too long": 1}


def test_a_symlink_is_never_followed(world: Config, tmp_path: Path) -> None:
    """`/var/lib/wpa-signal` is the Signal account, and the agent writing this
    directory is the least trusted thing on the box. O_NOFOLLOW, or a symlink is
    a read primitive pointed at whatever the gate can open."""
    secret = tmp_path / "account-state"
    secret.write_text(json.dumps({"to": "self", "text": "exfiltrated"}), encoding="utf-8")
    (tmp_path / "owner").mkdir(parents=True)
    (tmp_path / "owner" / "001.json").symlink_to(secret)

    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {"malformed": 1}
    assert secret.exists(), "the link is consumed; its target is not touched"


def test_a_fifo_cannot_stall_the_gate(world: Config, tmp_path: Path) -> None:
    """Opening a fifo read-only blocks until a writer appears. One `mkfifo` in the
    outbox would otherwise stop the whole control channel, inbound included."""
    (tmp_path / "owner").mkdir(parents=True)
    os.mkfifo(tmp_path / "owner" / "001.json")

    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {"malformed": 1}


def test_a_flood_keeps_the_oldest_and_refuses_the_newest(world: Config, tmp_path: Path) -> None:
    """Past the cap the likely explanation is a loop or an injection, not traffic —
    and the entries written before it went wrong are the ones worth keeping."""
    for index in range(40):
        _entry(tmp_path, "owner", f"{index:03d}.json", {"to": "self", "text": f"{index}"})

    refused: dict[str, int] = {}
    ready = drain(tmp_path, world, refused)

    assert refused == {"too many": 8}, "40 written, 32 kept"
    assert [item.text for item in ready] == ["0", "1", "2", "3"], "oldest first, four per cycle"
    remaining = sorted(path.name for path in (tmp_path / "owner").glob("*.json"))
    assert remaining == [f"{index:03d}.json" for index in range(4, 32)]


def test_half_written_entries_are_not_read(world: Config, tmp_path: Path) -> None:
    """The agent writes a dotfile and renames it, which is atomic. pathlib's glob
    matches dotfiles even though the shell's does not, so this is explicit."""
    directory = tmp_path / "owner"
    directory.mkdir(parents=True)
    (directory / ".tmp-1234.json").write_text('{"to": "self", "te', encoding="utf-8")
    (directory / "notes.txt").write_text("not an entry", encoding="utf-8")

    refused: dict[str, int] = {}
    assert drain(tmp_path, world, refused) == []
    assert refused == {}
    assert (directory / ".tmp-1234.json").exists()
    assert (directory / "notes.txt").exists()


def test_an_outbox_that_does_not_exist_yet_is_not_an_error(world: Config, tmp_path: Path) -> None:
    assert drain(tmp_path / "absent", world, {}) == []


def test_a_refusal_names_no_identifier_and_not_the_requested_name(
    world: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Threat model R4. The label came out of a file the agent wrote: free text,
    chosen by the least trusted process here, on its way to journald."""
    _entry(tmp_path, "family", "001.json", {"to": MOM_UUID, "text": "leak me"})
    drain(tmp_path, world, {})

    logged = capsys.readouterr().err
    assert "refused send: not in list (1 total)" in logged
    for secret in (MOM_UUID, OWNER_UUID, GROUP_ID, "leak me", "+15555550100"):
        assert secret not in logged


def test_an_entry_is_unlinked_before_it_is_sent(world: Config, tmp_path: Path) -> None:
    """At-most-once on purpose. A crash between the two loses a reply; the other
    order sends a person the same message twice, and only the gap is visible to
    whoever asked."""
    path = _entry(tmp_path, "owner", "001.json", {"to": "self", "text": "hi"})
    ready = drain(tmp_path, world, {})
    assert ready and not path.exists()


# --- The wire ------------------------------------------------------------------


def _serve(path: Path, batches: list[bytes], started: threading.Event) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(len(batches))
    started.set()
    for batch in batches:
        conn, _ = server.accept()
        conn.sendall(batch)
        conn.shutdown(socket.SHUT_WR)
        conn.close()
    server.close()


def _serve_capturing(
    path: Path, batches: list[bytes], started: threading.Event, seen: list[object]
) -> None:
    """Like `_serve`, but stays open long enough to record what the gate sends back.

    `_serve` shuts down and closes as soon as it has written, which is precisely what
    would hide an unwanted reply — the thing the caller is trying to assert about.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(len(batches))
    started.set()
    for batch in batches:
        conn, _ = server.accept()
        conn.sendall(batch)
        conn.settimeout(1.0)
        buffered = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffered += chunk
        except OSError:
            pass  # the timeout is the expected end, not an error
        for raw in buffered.splitlines():
            if raw.strip():
                try:
                    seen.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        conn.close()
    server.close()


def test_it_reconnects_when_the_daemon_goes_away(world: Config, tmp_path: Path) -> None:
    """signal-cli has Restart=on-failure, so the socket disappears under a live
    gate. A gate that exits on the first EOF is a control channel that works until
    the first restart and then silently isn't there."""
    line = (FIXTURES / "message.json").read_text(encoding="utf-8").replace("\n", "") + "\n"
    sock = tmp_path / "socket"
    commands = tmp_path / "commands.jsonl"
    started = threading.Event()
    server = threading.Thread(
        target=_serve, args=(sock, [line.encode(), line.encode()], started), daemon=True
    )
    server.start()
    started.wait(timeout=5)

    run(dataclasses.replace(world, socket=sock), commands, outbox=tmp_path / "out", cycles=2)
    server.join(timeout=5)

    written_lines = [
        json.loads(entry) for entry in commands.read_text(encoding="utf-8").splitlines()
    ]
    assert len(written_lines) == 2, "the second connection's command must arrive too"
    assert written_lines[0]["principal"] == "owner"


def test_an_accepted_command_sends_nothing_back(world: Config, tmp_path: Path) -> None:
    """The gate records a command and stays silent.

    It used to reply `ack <timestamp>` so a later quoted reply could be matched to a
    pending action (ADR 0008, NVB-16). ADR 0011 replaced quoted-reply confirmations
    with reaction approvals, which bind a YES to a specific delivered message, so the
    handle has nothing left to hold — and OpenClaw answers the sender itself, making
    the ack a second message per turn that said nothing. This asserts it is gone,
    because the cheapest way for it to come back is somebody restoring a helper that
    looks unused."""
    line = (FIXTURES / "message.json").read_text(encoding="utf-8").replace("\n", "") + "\n"
    sock = tmp_path / "socket"
    commands = tmp_path / "commands.jsonl"
    started = threading.Event()
    seen: list[object] = []
    server = threading.Thread(target=_serve_capturing, args=(sock, [line.encode()], started, seen))
    server.daemon = True
    server.start()
    started.wait(timeout=5)

    run(dataclasses.replace(world, socket=sock), commands, outbox=tmp_path / "out", cycles=1)
    server.join(timeout=5)

    assert commands.read_text(encoding="utf-8").strip(), "the command itself still lands"
    sends = [r for r in seen if isinstance(r, dict) and r.get("method") == "send"]
    assert sends == [], f"an accepted command must send nothing back, got {sends}"


# Both captured off the daemon on hardware, 2026-08-11, and redacted. A failure
# carries no top-level `result`, so "did this land" is the same question as "is
# there a timestamp here" — and the reason worth logging is the per-recipient
# `type`, because `code` is -1 for every failure there is.
SEND_OK: object = {
    "jsonrpc": "2.0",
    "result": {
        "results": [
            {
                "recipientAddress": {"uuid": MOM_UUID, "number": None, "username": None},
                "type": "SUCCESS",
            }
        ],
        "timestamp": 1786473936544,
    },
    "id": "send-20260811-0001",
}
SEND_FAILED: object = {
    "jsonrpc": "2.0",
    "error": {
        "code": -1,
        "message": "Failed to send message",
        "data": {
            "response": {
                "results": [
                    {
                        "recipientAddress": {
                            "uuid": None,
                            "number": "+99999999999",
                            "username": None,
                        },
                        "type": "UNREGISTERED_FAILURE",
                    }
                ],
                "timestamp": 1786473960560,
            }
        },
    },
    "id": "send-20260811-0001",
}


def _serve_answering(
    path: Path, started: threading.Event, seen: list[object], answers: int
) -> None:
    """A daemon that replies, so one test can drive the whole outbound path."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    started.set()
    conn, _ = server.accept()
    stream = conn.makefile("rwb")
    for _ in range(answers):
        line = stream.readline()
        if not line:
            break
        request: object = json.loads(line)
        seen.append(request)
        request_id = request["id"] if isinstance(request, dict) else None
        method = request.get("method") if isinstance(request, dict) else None
        if method == "listGroups":
            body: object = {"jsonrpc": "2.0", "id": request_id, "result": []}
        else:
            response = dict(SEND_OK) if isinstance(SEND_OK, dict) else {}
            response["id"] = request_id
            body = response
        stream.write(json.dumps(body).encode() + b"\n")
        stream.flush()
    conn.shutdown(socket.SHUT_WR)
    conn.close()
    server.close()


def test_the_sent_timestamp_is_recorded_for_the_registry(world: Config, tmp_path: Path) -> None:
    """End to end: an entry the agent wrote goes out under the resolved identifier,
    and the timestamp the daemon answers with is written down. Without that, a YES
    quoting this message cannot be matched to the prompt it answers (NVB-16)."""
    sock = tmp_path / "socket"
    commands = tmp_path / "commands.jsonl"
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent.jsonl"
    _entry(outbox, "owner", "20260811-0001.json", {"to": "family", "text": "on my way"})

    started = threading.Event()
    seen: list[object] = []
    server = threading.Thread(target=_serve_answering, args=(sock, started, seen, 2), daemon=True)
    server.start()
    started.wait(timeout=5)

    run(
        dataclasses.replace(world, socket=sock),
        commands,
        outbox=outbox,
        sent=sent,
        cycles=1,
    )
    server.join(timeout=5)

    sends = [
        request
        for request in seen
        if isinstance(request, dict) and request.get("method") == "send"
    ]
    assert sends == [
        {
            "jsonrpc": "2.0",
            "id": "send-20260811-0001",
            "method": "send",
            # A group goes out as `groupId`, never as a recipient — derived from the
            # identifier itself, so config cannot get it wrong.
            "params": {"groupId": GROUP_ID, "message": "on my way"},
        }
    ]
    recorded = [json.loads(line) for line in sent.read_text(encoding="utf-8").splitlines()]
    assert recorded == [
        {
            "timestamp": 1786473936544,
            "agent": "owner",
            "profile": "owner-full",
            "entry": "20260811-0001",
        }
    ]


def test_the_gate_asks_who_is_in_the_groups_before_trusting_any_of_them(
    world: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed on connect. The daemon here answers `[]` — the group is not
    listed, so it drifts, and the gate says so instead of carrying on."""
    sock = tmp_path / "socket"
    started = threading.Event()
    seen: list[object] = []
    server = threading.Thread(target=_serve_answering, args=(sock, started, seen, 1), daemon=True)
    server.start()
    started.wait(timeout=5)

    run(
        dataclasses.replace(world, socket=sock),
        tmp_path / "commands.jsonl",
        outbox=tmp_path / "outbox",
        cycles=1,
    )
    server.join(timeout=5)

    assert [request.get("method") for request in seen if isinstance(request, dict)] == [
        "listGroups"
    ]
    logged = capsys.readouterr().err
    assert "membership drift: family" in logged
    assert GROUP_ID not in logged, "a group id is an identifier; threat model R4"


def test_a_failed_send_is_not_recorded_as_delivered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure carries a timestamp too, nested under `error.data.response`.
    Recording it would key a confirmation on a prompt that never arrived — a YES
    quoting nothing, matched to a pending action anyway."""
    sent = tmp_path / "sent.jsonl"
    awaiting = {
        "send-20260811-0001": Outbound(
            "20260811-0001", "owner", "owner-full", Recipient(MOM_UUID, group=False), "hi"
        )
    }
    refused: dict[str, int] = {}

    assert _record(SEND_FAILED, awaiting, sent, refused) is None
    assert not sent.exists()
    assert refused == {"send failed: UNREGISTERED_FAILURE": 1}
    # The delivery status may be logged; the number that failed may not.
    assert "+99999999999" not in capsys.readouterr().err


def test_a_response_to_nothing_is_ignored(tmp_path: Path) -> None:
    """Junk with a plausible id must not raise its way into the sent log."""
    sent = tmp_path / "sent.jsonl"
    junk: list[object] = [None, [], {"id": 7}, {"id": "send-unknown"}, {"result": {}}]
    for item in junk:
        assert _record(item, {}, sent, {}) is None
    assert not sent.exists()


def test_check_prints_what_each_pair_may_actually_do(world: Config) -> None:
    """The artefact for "what can this thing do", asked before a restart or asked by
    a family member. Generated from what the gate runs on, so it cannot drift."""
    printed = check(world)
    assert "family (group, 3 pinned members)" in printed
    assert "mom-family" in printed
    assert "wpa-agent@family.service" in printed
    assert "add an event to the family calendar" in printed
    assert "email.personal.draft" in printed
    # The narrower grant is visibly narrower, which is the point of printing it: the
    # family room's block mentions no personal calendar and no mailbox.
    family_block = printed[printed.index("family (group") :]
    assert "calendar.personal.rw" not in family_block
    assert "email.personal.draft" not in family_block
