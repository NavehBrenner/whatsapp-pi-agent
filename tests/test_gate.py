"""Tests for the Signal trigger gate.

The fixtures in `fixtures/signal/` are real envelopes off the daemon's socket,
redacted (see the README there). That matters more than usual here: the hole this
gate closes — a typing indicator arriving as a `receive` notification with no
`dataMessage` — is not in signal-cli's documentation. It was found by watching the
wire, so the test that it stays closed reads from the wire too.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from gate.signal import Command, Principal, decide, load_config, run

FIXTURES = Path(__file__).parent / "fixtures" / "signal"

OWNER = Principal(number="+15555550100", name="owner", profile="owner")
MOM = Principal(number="+15555550101", name="mom", profile="family")
PRINCIPALS = {
    OWNER.number: OWNER,
    "11111111-1111-4111-8111-111111111111": OWNER,
    MOM.number: MOM,
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
    socket_path, principals = load_config(config)
    assert socket_path == Path("/tmp/sock")
    assert principals["+15555550100"] is principals["11111111-1111-4111-8111-111111111111"]


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

    run(sock, PRINCIPALS, commands, cycles=2)
    server.join(timeout=5)

    written = [json.loads(entry) for entry in commands.read_text(encoding="utf-8").splitlines()]
    assert len(written) == 2, "the second connection's command must arrive too"
    assert written[0]["principal"] == "owner"
