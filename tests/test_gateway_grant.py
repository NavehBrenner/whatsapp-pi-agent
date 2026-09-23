"""Pure grant mutator + focused snapshot (NVB-103).

No I/O, no root. The security properties under test:

1. Unknown agent / bad tool name refuse before any mutation.
2. First agent-level alsoAllow seeds the global set (replaces, does not merge).
3. Room ceilings are touched only when a group binding already has tools.allow.
4. Sandbox allow is always extended.
5. focused_policy_snapshot never carries env / token bodies.
6. Idempotent second grant reports already_complete.
"""

from __future__ import annotations

import copy
import json

import pytest

from wpa_mcp.gateway_grant import (
    GrantError,
    GrantIntent,
    JsonObject,
    apply_grant,
    focused_policy_snapshot,
    intent_to_public_dict,
    plan_grant,
    redact_for_log,
)



def _obj(value: object, path: str = "value") -> JsonObject:
    assert isinstance(value, dict), path
    out: JsonObject = {}
    for k, v in value.items():
        assert isinstance(k, str), path
        out[k] = v
    return out


def _lst(value: object, path: str = "value") -> list[object]:
    assert isinstance(value, list), path
    return list(value)


def _str_list(value: object, path: str = "value") -> list[str]:
    items = _lst(value, path)
    out: list[str] = []
    for i, item in enumerate(items):
        assert isinstance(item, str), f"{path}[{i}]"
        out.append(item)
    return out


def _base_config() -> JsonObject:
    return {
        "tools": {
            "alsoAllow": ["read", "write", "web_search"],
            "sandbox": {"tools": {"allow": ["read", "write", "exec"]}},
        },
        "agents": {
            "list": [
                {
                    "id": "builder",
                    "tools": {
                        "alsoAllow": [
                            "read",
                            "write",
                            "web_search",
                            "wpa__sync",
                            "wpa__deploy",
                        ]
                    },
                },
                {"id": "owner"},
            ]
        },
        "bindings": [
            {
                "agentId": "builder",
                "match": {
                    "channel": "signal",
                    "peer": {"kind": "group", "id": "group-abc"},
                },
            }
        ],
        "channels": {
            "signal": {
                "configWrites": False,
                "groups": {
                    "group-abc": {
                        "tools": {
                            "allow": [
                                "read",
                                "write",
                                "wpa__sync",
                                "wpa__deploy",
                            ]
                        }
                    }
                },
            }
        },
        "mcp": {
            "servers": {
                "wpa": {
                    "env": {"WPA_PUSH_TOKEN": "ghp_should_never_leak"},
                }
            }
        },
    }


def test_intent_rejects_unexpected_keys() -> None:
    with pytest.raises(GrantError, match="unexpected keys"):
        GrantIntent.from_mapping(
            {
                "op": "grant_tool",
                "agent_id": "builder",
                "tool_name": "x",
                "extra": 1,
            }
        )


def test_intent_rejects_bad_tool_and_agent() -> None:
    with pytest.raises(GrantError, match="invalid tool_name"):
        GrantIntent.from_mapping({"agent_id": "builder", "tool_name": "../etc/passwd"})
    with pytest.raises(GrantError, match="invalid agent_id"):
        GrantIntent.from_mapping({"agent_id": "no spaces", "tool_name": "read"})


def test_intent_refuses_self_grant() -> None:
    """Granting the grant tool itself is cheaper to refuse than to trust a summary."""
    with pytest.raises(GrantError, match="not grantable"):
        GrantIntent.from_mapping(
            {"agent_id": "builder", "tool_name": "wpa__gateway_grant_tool"}
        )
    with pytest.raises(GrantError, match="not grantable"):
        GrantIntent.from_mapping(
            {"agent_id": "owner", "tool_name": "gateway_grant_tool"}
        )


def test_unknown_agent_refuses() -> None:
    cfg = _base_config()
    intent = GrantIntent(agent_id="nope", tool_name="skill_workshop")
    with pytest.raises(GrantError, match="unknown agent_id"):
        apply_grant(cfg, intent)


def test_three_layer_grant_for_builder_with_room_ceiling() -> None:
    cfg = _base_config()
    before = copy.deepcopy(cfg)
    intent = GrantIntent(agent_id="builder", tool_name="skill_workshop")
    report = apply_grant(cfg, intent)

    assert not report.already_complete
    layers = {c.layer: c.action for c in report.changes}
    assert layers["agent.alsoAllow"] == "add"
    assert any(k.startswith("room.allow[") for k in layers)
    assert layers["sandbox.allow"] == "add"

    agents = _obj(cfg["agents"], "agents")
    agent_list = _lst(agents["list"], "agents.list")
    agent = next(
        _obj(a, "agent")
        for a in agent_list
        if _obj(a, "agent").get("id") == "builder"
    )
    also = _str_list(_obj(agent["tools"], "tools")["alsoAllow"], "alsoAllow")
    assert "skill_workshop" in also
    channels = _obj(cfg["channels"], "channels")
    signal = _obj(channels["signal"], "signal")
    groups = _obj(signal["groups"], "groups")
    room = _obj(groups["group-abc"], "group")
    room_allow = _str_list(_obj(room["tools"], "tools")["allow"], "room.allow")
    assert "skill_workshop" in room_allow
    tools = _obj(cfg["tools"], "tools")
    sandbox = _obj(tools["sandbox"], "sandbox")
    sb_tools = _obj(sandbox["tools"], "sandbox.tools")
    assert "skill_workshop" in _str_list(sb_tools["allow"], "sandbox.allow")

    # Secrets untouched.
    mcp = _obj(cfg["mcp"], "mcp")
    servers = _obj(mcp["servers"], "servers")
    wpa = _obj(servers["wpa"], "wpa")
    env = _obj(wpa["env"], "env")
    assert env["WPA_PUSH_TOKEN"] == "ghp_should_never_leak"
    # Unrelated agent untouched.
    before_agents = _obj(before["agents"], "before.agents")
    assert agent_list[1] == _lst(before_agents["list"], "before.list")[1]
    assert signal["configWrites"] is False


def test_first_also_allow_seeds_global_set() -> None:
    cfg = _base_config()
    # owner has no tools block — first alsoAllow must seed globals.
    intent = GrantIntent(agent_id="owner", tool_name="session_status")
    report = apply_grant(cfg, intent)
    agents = _obj(cfg["agents"], "agents")
    owner = next(
        _obj(a, "agent")
        for a in _lst(agents["list"], "agents.list")
        if _obj(a, "agent").get("id") == "owner"
    )
    also = _str_list(_obj(owner["tools"], "tools")["alsoAllow"], "alsoAllow")
    assert also == ["read", "write", "web_search", "session_status"]
    seed = next(c for c in report.changes if c.layer == "agent.alsoAllow")
    assert seed.action == "seed+add"


def test_no_room_ceiling_when_group_has_no_allow() -> None:
    cfg = _base_config()
    # Binding exists but groups entry has no tools.allow — do not invent one.
    # Mutate the live tree in place (helpers that rebuild dicts would orphan the write).
    channels = cfg["channels"]
    assert isinstance(channels, dict)
    signal = channels["signal"]
    assert isinstance(signal, dict)
    groups = signal["groups"]
    assert isinstance(groups, dict)
    groups["group-abc"] = {"requireMention": False}
    intent = GrantIntent(agent_id="builder", tool_name="skill_workshop")
    report = apply_grant(cfg, intent)
    assert all(not c.layer.startswith("room.allow") for c in report.changes)
    tools = _obj(cfg["tools"], "tools")
    sandbox = _obj(tools["sandbox"], "sandbox")
    sb_tools = _obj(sandbox["tools"], "sandbox.tools")
    assert "skill_workshop" in _str_list(sb_tools["allow"], "sandbox.allow")


def test_idempotent_second_grant() -> None:
    cfg = _base_config()
    intent = GrantIntent(agent_id="builder", tool_name="skill_workshop")
    apply_grant(cfg, intent)
    after_first = json.dumps(cfg, sort_keys=True)
    report = apply_grant(cfg, intent)
    assert report.already_complete
    assert json.dumps(cfg, sort_keys=True) == after_first


def test_plan_grant_does_not_mutate() -> None:
    cfg = _base_config()
    snapshot = json.dumps(cfg, sort_keys=True)
    intent = GrantIntent(agent_id="builder", tool_name="skill_workshop")
    report = plan_grant(cfg, intent)
    assert report.added
    assert json.dumps(cfg, sort_keys=True) == snapshot


def test_focused_snapshot_excludes_secrets() -> None:
    cfg = _base_config()
    intent = GrantIntent(agent_id="builder", tool_name="skill_workshop")
    apply_grant(cfg, intent)
    snap = focused_policy_snapshot(cfg, intent)
    blob = json.dumps(snap)
    assert "ghp_" not in blob
    assert "WPA_PUSH_TOKEN" not in blob
    also = snap["agent_alsoAllow"]
    sandbox_allow = snap["sandbox_allow"]
    rooms = snap["rooms"]
    assert isinstance(also, list) and "skill_workshop" in also
    assert isinstance(sandbox_allow, list) and "skill_workshop" in sandbox_allow
    assert isinstance(rooms, dict) and "group-abc" in rooms


def test_redact_for_log_scrubs_secret_keys() -> None:
    raw: JsonObject = {
        "env": {"token": "secret", "name": "ok"},
        "list": [{"password": "x"}],
    }
    out = redact_for_log(raw)
    out_d = _obj(out, "out")
    env = _obj(out_d["env"], "env")
    assert env["token"] == "***"
    assert env["name"] == "ok"
    first = _obj(_lst(out_d["list"], "list")[0], "list0")
    assert first["password"] == "***"


def test_intent_public_dict_is_minimal() -> None:
    intent = GrantIntent(agent_id="builder", tool_name="skill_workshop")
    assert intent_to_public_dict(intent) == {
        "op": "grant_tool",
        "agent_id": "builder",
        "tool_name": "skill_workshop",
    }


def test_mcp_style_tool_names_accepted() -> None:
    GrantIntent.from_mapping({"agent_id": "builder", "tool_name": "wpa__sync"})
    GrantIntent.from_mapping({"agent_id": "builder", "tool_name": "group:plugins"})
    GrantIntent.from_mapping({"agent_id": "builder", "tool_name": "skill_workshop"})
