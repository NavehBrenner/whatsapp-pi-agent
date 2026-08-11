"""Tests for the Signal trigger gate.

The fixtures in `fixtures/signal/` are real envelopes off the daemon's socket,
redacted (see the README there). That matters more than usual here: the hole this
gate closes — a typing indicator arriving as a `receive` notification with no
`dataMessage` — is not in signal-cli's documentation. It was found by watching the
wire, so the test that it stays closed reads from the wire too.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from gate.signal import (
    Command,
    Config,
    Outbound,
    Principal,
    _record,
    decide,
    drain,
    load_config,
    resolve,
    run,
)

FIXTURES = Path(__file__).parent / "fixtures" / "signal"

OWNER = Principal(name="owner", profile="owner")
MOM = Principal(name="mom", profile="family")
OWNER_UUID = "11111111-1111-4111-8111-111111111111"
MOM_UUID = "22222222-2222-4222-8222-222222222222"
ROSTER = {"owner": OWNER_UUID, "mom": MOM_UUID}
# Keyed by UUID because that is what actually arrives: current Signal does not
# share phone numbers, so `sourceNumber` is null on real traffic and a
# number-keyed allowlist matches nothing (hardware, 2026-08-11).
PRINCIPALS = {
    "11111111-1111-4111-8111-111111111111": OWNER,
    "+15555550100": OWNER,
    "22222222-2222-4222-8222-222222222222": MOM,
}


def envelope(name: str) -> object:
    parsed: object = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return parsed


def test_a_message_from_a_principal_is_a_command() -> None:
    verdict = decide(envelope("message"), PRINCIPALS)
    assert isinstance(verdict, Command)
    assert verdict.principal == "owner"
    assert verdict.profile == "owner"
    assert verdict.body == "ping"
    assert verdict.reply_to is None


def test_each_principal_carries_its_own_profile() -> None:
    """The whole extensibility claim, reduced to an assertion: adding a person is
    a config row, and what they may do travels with the command."""
    verdict = decide(envelope("family"), PRINCIPALS)
    assert isinstance(verdict, Command)
    assert (verdict.principal, verdict.profile) == ("mom", "family")


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("typing", "no body"),
        ("receipt", "no body"),
        ("stranger", "sender"),
        ("group", "group"),
    ],
)
def test_everything_else_is_dropped(fixture: str, reason: str) -> None:
    assert decide(envelope(fixture), PRINCIPALS) == reason


def test_a_reply_remembers_what_it_answered() -> None:
    """A `YES` is a command, but only useful if it still points at the action it
    authorises. The registry is M4's; not losing the link is this gate's job."""
    verdict = decide(envelope("quote-reply"), PRINCIPALS)
    assert isinstance(verdict, Command)
    assert verdict.reply_to == 1754912300000


def test_junk_never_raises_its_way_past_a_check() -> None:
    junk: list[object] = [None, [], "receive", {"method": "receive"}, {"params": 3}]
    for item in junk:
        assert isinstance(decide(item, PRINCIPALS), str)


def test_a_config_with_no_principals_refuses_to_start(tmp_path: Path) -> None:
    """An allowlist that defaults to everyone is not an allowlist, and one that
    defaults to nobody is a dead assistant that looks alive. Both are refusals."""
    config = tmp_path / "config.toml"
    config.write_text('[signal]\nsocket = "/run/wpa-signal/socket"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(config)


def test_config_keys_principals_by_number_and_uuid(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[signal]\nsocket = "/tmp/sock"\n\n'
        "[[signal.principals]]\n"
        'number = "+15555550100"\nname = "owner"\nprofile = "owner"\n'
        'uuid = "11111111-1111-4111-8111-111111111111"\n',
        encoding="utf-8",
    )
    loaded = load_config(config)
    assert loaded.socket == Path("/tmp/sock")
    assert loaded.principals["+15555550100"] is loaded.principals[OWNER_UUID]
    # The uuid, not the number: an ACI is what arrives, and a re-registered number
    # belongs to whoever holds it next.
    assert loaded.roster == {"owner": OWNER_UUID}
    assert loaded.principals[OWNER_UUID].send_to == ("self",)


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


def test_it_reconnects_when_the_daemon_goes_away(tmp_path: Path) -> None:
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

    run(Config(socket=sock, principals=PRINCIPALS, roster=ROSTER), commands, cycles=2)
    server.join(timeout=5)

    written = [json.loads(entry) for entry in commands.read_text(encoding="utf-8").splitlines()]
    assert len(written) == 2, "the second connection's command must arrive too"
    assert written[0]["principal"] == "owner"


# --- Outbound: the agent asks by name, the gate decides (ADR 0009) -------------


def _config(**send_to: tuple[str, ...]) -> Config:
    """A two-principal config, with each principal's send list given by keyword."""
    principals = {
        OWNER_UUID: Principal("owner", "owner", send_to.get("owner", ("self",))),
        MOM_UUID: Principal("mom", "family", send_to.get("mom", ("self",))),
    }
    return Config(socket=Path("/nonexistent"), principals=principals, roster=dict(ROSTER))


def _entry(outbox: Path, principal: str, name: str, payload: object) -> Path:
    directory = outbox / principal
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_entry_is_resolved_through_the_roster(tmp_path: Path) -> None:
    """The whole indirection in one assertion: the agent wrote a *name*, and what
    goes on the wire is an identifier it has never seen."""
    config = _config(owner=("self", "mom"))
    _entry(tmp_path, "owner", "001.json", {"to": "mom", "text": "on my way"})

    refused: dict[str, int] = {}
    ready = drain(tmp_path, config, refused)

    assert ready == [
        Outbound(
            entry="001",
            principal="owner",
            profile="owner",
            recipient=MOM_UUID,
            text="on my way",
        )
    ]
    assert refused == {}


def test_self_is_the_default_and_needs_no_configuration(tmp_path: Path) -> None:
    """An agent granted nothing can still answer its own principal — and only it."""
    config = _config()
    assert config.principals[OWNER_UUID].send_to == ("self",)
    _entry(tmp_path, "owner", "001.json", {"to": "self", "text": "done"})

    ready = drain(tmp_path, config, {})
    assert [(item.principal, item.recipient) for item in ready] == [("owner", OWNER_UUID)]


@pytest.mark.parametrize("name", ["mom", "owner", "nobody", "", "self "])
def test_a_name_outside_the_send_list_is_refused(tmp_path: Path, name: str) -> None:
    """Unlisted and unknown are the *same* refusal: the agent must not be able to
    learn who exists by watching which way it is told no."""
    config = _config()
    path = _entry(tmp_path, "owner", "001.json", {"to": name, "text": "hello"})

    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {"not in list": 1}
    assert not path.exists(), "a refused entry is consumed, not retried forever"


def test_resolution_is_pure_and_never_invents_a_recipient() -> None:
    listed = Principal("owner", "owner", ("self", "mom"))
    assert resolve("self", listed, ROSTER) == OWNER_UUID
    assert resolve("mom", listed, ROSTER) == MOM_UUID
    assert resolve("mom", OWNER, ROSTER) is None
    # A send list may name only principals, so this cannot happen through
    # `load_config` — but resolution must not depend on that check having run.
    assert resolve("ghost", Principal("owner", "owner", ("ghost",)), ROSTER) is None


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
    tmp_path: Path, payload: object, reason: str
) -> None:
    config = _config()
    _entry(tmp_path, "owner", "001.json", payload)
    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {reason: 1}


def test_unparseable_bytes_are_refused(tmp_path: Path) -> None:
    config = _config()
    (tmp_path / "owner").mkdir(parents=True)
    (tmp_path / "owner" / "001.json").write_bytes(b"\xff\xfe not json")
    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {"malformed": 1}


def test_an_oversize_entry_is_refused_without_being_read(tmp_path: Path) -> None:
    config = _config()
    (tmp_path / "owner").mkdir(parents=True)
    (tmp_path / "owner" / "001.json").write_text(
        json.dumps({"to": "self", "text": "x" * (64 * 1024)}), encoding="utf-8"
    )
    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {"too long": 1}


def test_a_symlink_is_never_followed(tmp_path: Path) -> None:
    """`/var/lib/wpa-signal` is the Signal account, and the agent writing this
    directory is the least trusted thing on the box. O_NOFOLLOW, or a symlink is
    a read primitive pointed at whatever the gate can open."""
    config = _config()
    secret = tmp_path / "account-state"
    secret.write_text(json.dumps({"to": "self", "text": "exfiltrated"}), encoding="utf-8")
    (tmp_path / "owner").mkdir(parents=True)
    (tmp_path / "owner" / "001.json").symlink_to(secret)

    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {"malformed": 1}
    assert secret.exists(), "the link is consumed; its target is not touched"


def test_a_fifo_cannot_stall_the_gate(tmp_path: Path) -> None:
    """Opening a fifo read-only blocks until a writer appears. One `mkfifo` in the
    outbox would otherwise stop the whole control channel, inbound included."""
    config = _config()
    (tmp_path / "owner").mkdir(parents=True)
    os.mkfifo(tmp_path / "owner" / "001.json")

    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {"malformed": 1}


def test_a_flood_keeps_the_oldest_and_refuses_the_newest(tmp_path: Path) -> None:
    """Past the cap the likely explanation is a loop or an injection, not traffic —
    and the entries written before it went wrong are the ones worth keeping."""
    config = _config()
    for index in range(40):
        _entry(tmp_path, "owner", f"{index:03d}.json", {"to": "self", "text": f"{index}"})

    refused: dict[str, int] = {}
    ready = drain(tmp_path, config, refused)

    assert refused == {"too many": 8}, "40 written, 32 kept"
    assert [item.text for item in ready] == ["0", "1", "2", "3"], "oldest first, four per cycle"
    remaining = sorted(path.name for path in (tmp_path / "owner").glob("*.json"))
    assert remaining == [f"{index:03d}.json" for index in range(4, 32)]


def test_half_written_entries_are_not_read(tmp_path: Path) -> None:
    """The agent writes a dotfile and renames it, which is atomic. pathlib's glob
    matches dotfiles even though the shell's does not, so this is explicit."""
    config = _config()
    directory = tmp_path / "owner"
    directory.mkdir(parents=True)
    (directory / ".tmp-1234.json").write_text('{"to": "self", "te', encoding="utf-8")
    (directory / "notes.txt").write_text("not an entry", encoding="utf-8")

    refused: dict[str, int] = {}
    assert drain(tmp_path, config, refused) == []
    assert refused == {}
    assert (directory / ".tmp-1234.json").exists()
    assert (directory / "notes.txt").exists()


def test_an_outbox_that_does_not_exist_yet_is_not_an_error(tmp_path: Path) -> None:
    assert drain(tmp_path / "absent", _config(), {}) == []


def test_a_refusal_names_no_identifier_and_not_the_requested_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Threat model R4. The name came out of a file the agent wrote: free text,
    chosen by the least trusted process here, on its way to journald."""
    config = _config()
    _entry(tmp_path, "owner", "001.json", {"to": MOM_UUID, "text": "leak me"})
    drain(tmp_path, config, {})

    logged = capsys.readouterr().err
    assert "refused send: not in list (1 total)" in logged
    for secret in (MOM_UUID, OWNER_UUID, "leak me", "+15555550100"):
        assert secret not in logged


def test_a_send_list_naming_a_non_principal_refuses_to_start(tmp_path: Path) -> None:
    """A send list may hold only principals (ADR 0009). A typo that quietly means
    "grants nothing" is worse than one that stops the gate."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[signal]\nsocket = "/tmp/sock"\n\n'
        "[[signal.principals]]\n"
        f'uuid = "{OWNER_UUID}"\nname = "owner"\nprofile = "owner"\n'
        'send_to = ["self", "the-plumber"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="the-plumber"):
        load_config(config)


def test_two_principals_may_not_share_a_name(tmp_path: Path) -> None:
    """Once a name addresses a person, a duplicate has no answer to `to: "mom"`."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[signal]\nsocket = "/tmp/sock"\n\n'
        "[[signal.principals]]\n"
        f'uuid = "{OWNER_UUID}"\nname = "mom"\nprofile = "owner"\n\n'
        "[[signal.principals]]\n"
        f'uuid = "{MOM_UUID}"\nname = "mom"\nprofile = "family"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="two principals named mom"):
        load_config(config)


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
        response = dict(SEND_OK) if isinstance(SEND_OK, dict) else {}
        response["id"] = request_id
        stream.write(json.dumps(response).encode() + b"\n")
        stream.flush()
    conn.shutdown(socket.SHUT_WR)
    conn.close()
    server.close()


def test_the_sent_timestamp_is_recorded_for_the_registry(tmp_path: Path) -> None:
    """End to end: an entry the agent wrote goes out under the resolved identifier,
    and the timestamp the daemon answers with is written down. Without that, a YES
    quoting this message cannot be matched to the prompt it answers (NVB-16)."""
    sock = tmp_path / "socket"
    commands = tmp_path / "commands.jsonl"
    outbox = tmp_path / "outbox"
    sent = tmp_path / "sent.jsonl"
    _entry(outbox, "owner", "20260811-0001.json", {"to": "mom", "text": "on my way"})

    started = threading.Event()
    seen: list[object] = []
    server = threading.Thread(
        target=_serve_answering, args=(sock, started, seen, 1), daemon=True
    )
    server.start()
    started.wait(timeout=5)

    config = _config(owner=("self", "mom"))
    run(
        Config(socket=sock, principals=config.principals, roster=config.roster),
        commands,
        outbox=outbox,
        sent=sent,
        cycles=1,
    )
    server.join(timeout=5)

    assert seen == [
        {
            "jsonrpc": "2.0",
            "id": "send-20260811-0001",
            "method": "send",
            "params": {"recipient": [MOM_UUID], "message": "on my way"},
        }
    ]
    recorded = [json.loads(line) for line in sent.read_text(encoding="utf-8").splitlines()]
    assert recorded == [
        {
            "timestamp": 1786473936544,
            "principal": "owner",
            "profile": "owner",
            "entry": "20260811-0001",
        }
    ]


def test_a_failed_send_is_not_recorded_as_delivered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure carries a timestamp too, nested under `error.data.response`.
    Recording it would key a confirmation on a prompt that never arrived — a YES
    quoting nothing, matched to a pending action anyway."""
    sent = tmp_path / "sent.jsonl"
    awaiting = {
        "send-20260811-0001": Outbound("20260811-0001", "owner", "owner", MOM_UUID, "hi")
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


def test_an_entry_is_unlinked_before_it_is_sent(tmp_path: Path) -> None:
    """At-most-once on purpose. A crash between the two loses a reply; the other
    order sends a person the same message twice, and only the gap is visible to
    whoever asked."""
    config = _config()
    path = _entry(tmp_path, "owner", "001.json", {"to": "self", "text": "hi"})
    ready = drain(tmp_path, config, {})
    assert ready and not path.exists()
