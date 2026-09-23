"""Pure structured grant of one tool into openclaw.json policy layers.

NVB-103. The agent supplies only ``agent_id`` and ``tool_name``. Host code owns the
mutation: which lists grow, that an agent-level ``alsoAllow`` is seeded from the
global set before the first append (replaces, does not merge), and that a room
ceiling is touched only when the agent is already bound to a group that has one.

No I/O. Callers load JSON, call ``apply_grant`` / ``plan_grant``, write results.
Secrets never need to enter this module — callers pass a dict and take a dict back.

Types use ``object`` rather than ``Any``: this repo sets ``disallow_any_explicit``.
JSON trees are validated at the edges with isinstance checks, same pattern as gate.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Mapping, MutableMapping

# Tool names: core (read), MCP (wpa__sync), groups (group:plugins).
# Reject path separators and whitespace.
_TOOL_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*(?::[A-Za-z][A-Za-z0-9_]*)?$"
)
_AGENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

# Tools that must never be granted through this path. Granting the grant tool
# itself turns one approval into a second grant-capable agent; cheaper to refuse
# than to trust a 400-char summary (PR #55 review).
_DENIED_TOOL_NAMES = frozenset(
    {
        "wpa__gateway_grant_tool",
        "gateway_grant_tool",
    }
)

_SECRET_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "authorization",
    "credential",
    "private_key",
    "privatekey",
)

# Mutable JSON object tree. Values stay object; callers narrow with isinstance.
JsonObject = dict[str, object]


class GrantError(ValueError):
    """Intent or config is not grantable. Message is safe to show the model."""


@dataclass(frozen=True)
class GrantIntent:
    """Typed intent — the only agent-supplied surface for this op."""

    agent_id: str
    tool_name: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> GrantIntent:
        if not isinstance(raw, Mapping):
            raise GrantError("intent must be an object")
        unexpected = sorted(set(raw) - {"op", "agent_id", "tool_name"})
        if unexpected:
            raise GrantError(f"intent has unexpected keys: {', '.join(unexpected)}")
        op = raw.get("op", "grant_tool")
        if op != "grant_tool":
            raise GrantError(f"unsupported op: {op!r}")
        agent_id = raw.get("agent_id")
        tool_name = raw.get("tool_name")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise GrantError("agent_id must be a non-empty string")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise GrantError("tool_name must be a non-empty string")
        agent_id = agent_id.strip()
        tool_name = tool_name.strip()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise GrantError(f"invalid agent_id: {agent_id!r}")
        if not _TOOL_NAME_RE.fullmatch(tool_name):
            raise GrantError(f"invalid tool_name: {tool_name!r}")
        if tool_name in _DENIED_TOOL_NAMES:
            raise GrantError(
                f"refusing to grant {tool_name!r}: the grant tool itself is "
                "not grantable through this path"
            )
        return cls(agent_id=agent_id, tool_name=tool_name)


@dataclass
class LayerChange:
    """One list that will gain (or already holds) the tool."""

    layer: str
    path: str
    action: str  # "add" | "already" | "seed+add"
    detail: str = ""


@dataclass
class GrantReport:
    """What ``apply_grant`` did or would do."""

    agent_id: str
    tool_name: str
    changes: list[LayerChange] = field(default_factory=list)
    already_complete: bool = False

    @property
    def added(self) -> list[LayerChange]:
        return [c for c in self.changes if c.action != "already"]

    def summary_lines(self) -> list[str]:
        lines = [f"grant {self.tool_name} -> agent {self.agent_id}"]
        if self.already_complete:
            lines.append("already granted on every required layer — no write")
            return lines
        for change in self.changes:
            lines.append(f"{change.action}: {change.layer} ({change.path})")
            if change.detail:
                lines.append(f"  {change.detail}")
        return lines


def _as_dict(node: object, path: str) -> JsonObject:
    if not isinstance(node, dict):
        raise GrantError(f"{path} must be an object")
    out: JsonObject = {}
    for key, value in node.items():
        if not isinstance(key, str):
            raise GrantError(f"{path} keys must be strings")
        out[key] = value
    return out


def _as_list(node: object, path: str) -> list[object]:
    if not isinstance(node, list):
        raise GrantError(f"{path} must be an array")
    return list(node)


def _agent_entries(config: Mapping[str, object]) -> list[JsonObject]:
    agents = _as_dict(config.get("agents", {}), "agents")
    raw_list = agents.get("list", [])
    entries = _as_list(raw_list, "agents.list")
    out: list[JsonObject] = []
    for i, entry in enumerate(entries):
        out.append(_as_dict(entry, f"agents.list[{i}]"))
    return out


def _find_agent(
    config: Mapping[str, object], agent_id: str
) -> tuple[int, JsonObject]:
    for i, entry in enumerate(_agent_entries(config)):
        if entry.get("id") == agent_id:
            return i, entry
    raise GrantError(f"unknown agent_id: {agent_id!r} (not in agents.list)")


def _global_also_allow(config: Mapping[str, object]) -> list[str]:
    tools = _as_dict(config.get("tools", {}), "tools")
    raw = tools.get("alsoAllow", [])
    if raw is None:
        return []
    items = _as_list(raw, "tools.alsoAllow")
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str):
            raise GrantError(f"tools.alsoAllow[{i}] must be a string")
        out.append(item)
    return out


def _string_list(node: object, path: str) -> list[str]:
    items = _as_list(node, path)
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str):
            raise GrantError(f"{path}[{i}] must be a string")
        out.append(item)
    return out


def _group_ids_for_agent(config: Mapping[str, object], agent_id: str) -> list[str]:
    """Group peer ids from bindings that route to this agent."""
    raw_bindings = config.get("bindings", [])
    if raw_bindings is None:
        return []
    bindings = _as_list(raw_bindings, "bindings")
    group_ids: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(bindings):
        entry = _as_dict(raw, f"bindings[{i}]")
        if entry.get("agentId") != agent_id:
            continue
        match = entry.get("match")
        if match is None:
            continue
        match_d = _as_dict(match, f"bindings[{i}].match")
        peer = match_d.get("peer")
        if peer is None:
            continue
        peer_d = _as_dict(peer, f"bindings[{i}].match.peer")
        if peer_d.get("kind") != "group":
            continue
        peer_id = peer_d.get("id")
        if not isinstance(peer_id, str) or not peer_id or peer_id in seen:
            continue
        seen.add(peer_id)
        group_ids.append(peer_id)
    return group_ids


def _room_allow_targets(
    config: Mapping[str, object], agent_id: str
) -> list[tuple[str, JsonObject, str]]:
    """Return (group_id, tools_object, path) for rooms that already have a ceiling."""
    channels = config.get("channels")
    if channels is None:
        return []
    channels_d = _as_dict(channels, "channels")
    signal = channels_d.get("signal")
    if signal is None:
        return []
    signal_d = _as_dict(signal, "channels.signal")
    groups = signal_d.get("groups")
    if groups is None:
        return []
    groups_d = _as_dict(groups, "channels.signal.groups")
    out: list[tuple[str, JsonObject, str]] = []
    for group_id in _group_ids_for_agent(config, agent_id):
        # Bound group with no groups{} entry — no ceiling to extend.
        if group_id not in groups_d:
            continue
        room = _as_dict(groups_d[group_id], f"channels.signal.groups[{group_id!r}]")
        tools = room.get("tools")
        if tools is None:
            continue
        tools_d = _as_dict(tools, f"channels.signal.groups[{group_id!r}].tools")
        # Room exists but has no allow ceiling — do not invent one.
        if "allow" not in tools_d:
            continue
        out.append(
            (
                group_id,
                tools_d,
                f"channels.signal.groups[{group_id!r}].tools.allow",
            )
        )
    return out


def _as_mutable_config(config: Mapping[str, object]) -> JsonObject:
    """Deep-copy a mapping into a plain mutable JSON object tree."""
    return copy.deepcopy(dict(config))


def plan_grant(config: Mapping[str, object], intent: GrantIntent) -> GrantReport:
    """Describe required layers without mutating."""
    working = _as_mutable_config(config)
    return apply_grant(working, intent, mutate=False)


def apply_grant(
    config: MutableMapping[str, object],
    intent: GrantIntent,
    *,
    mutate: bool = True,
) -> GrantReport:
    """Grant ``intent.tool_name`` on every required layer.

    When ``mutate`` is False, compute the report against a deep copy and leave
    ``config`` untouched. When True, mutate ``config`` in place and return the report.

    Tool *existence* in OpenClaw's catalogue is not fully checkable from JSON
    alone (first-time grants like skill_workshop are the point). Shape + denylist
    are enforced at GrantIntent construction; unknown agent is enforced here.
    """
    target: MutableMapping[str, object]
    if mutate:
        target = config
    else:
        target = _as_mutable_config(config)

    index, _agent = _find_agent(target, intent.agent_id)
    agents = _as_dict(target.get("agents", {}), "agents")
    agent_list = _as_list(agents.get("list", []), "agents.list")
    # Replace the list slot with a mutable JsonObject we own.
    agent_entry = _as_dict(agent_list[index], f"agents.list[{index}]")
    agent_list[index] = agent_entry
    agents["list"] = agent_list
    target["agents"] = agents

    report = GrantReport(agent_id=intent.agent_id, tool_name=intent.tool_name)
    tool = intent.tool_name

    # --- layer 1: agent tools.alsoAllow (seed from global if first alsoAllow) ---
    tools_block = agent_entry.get("tools")
    if tools_block is None:
        tools_d: JsonObject = {}
        agent_entry["tools"] = tools_d
    else:
        tools_d = _as_dict(tools_block, f"agents.list[{index}].tools")
        agent_entry["tools"] = tools_d

    global_names = _global_also_allow(target)
    also_path = f"agents.list[{index}].tools.alsoAllow"
    if "alsoAllow" not in tools_d or tools_d.get("alsoAllow") is None:
        # First agent-level alsoAllow REPLACES the global set — seed it.
        seed = list(global_names)
        if tool not in seed:
            seed.append(tool)
        if mutate:
            tools_d["alsoAllow"] = seed
        report.changes.append(
            LayerChange(
                layer="agent.alsoAllow",
                path=also_path,
                action="seed+add",
                detail=f"seeded {len(global_names)} global name(s), then {tool}",
            )
        )
    else:
        also = _string_list(tools_d["alsoAllow"], also_path)
        if tool in also:
            report.changes.append(
                LayerChange(layer="agent.alsoAllow", path=also_path, action="already")
            )
        else:
            if mutate:
                also.append(tool)
                tools_d["alsoAllow"] = also
            report.changes.append(
                LayerChange(layer="agent.alsoAllow", path=also_path, action="add")
            )

    # --- layer 2: room ceilings for group bindings that already have allow ---
    channels_raw = target.get("channels")
    if channels_raw is not None:
        channels_d = _as_dict(channels_raw, "channels")
        target["channels"] = channels_d
        signal_raw = channels_d.get("signal")
        if signal_raw is not None:
            signal_d = _as_dict(signal_raw, "channels.signal")
            channels_d["signal"] = signal_d
            groups_raw = signal_d.get("groups")
            if groups_raw is not None:
                groups_d = _as_dict(groups_raw, "channels.signal.groups")
                signal_d["groups"] = groups_d
                for group_id in _group_ids_for_agent(target, intent.agent_id):
                    if group_id not in groups_d:
                        continue
                    room_path = f"channels.signal.groups[{group_id!r}]"
                    room = _as_dict(groups_d[group_id], room_path)
                    groups_d[group_id] = room
                    tools_raw = room.get("tools")
                    if tools_raw is None:
                        continue
                    room_tools = _as_dict(tools_raw, f"{room_path}.tools")
                    room["tools"] = room_tools
                    if "allow" not in room_tools:
                        continue
                    allow_path = f"{room_path}.tools.allow"
                    allow = _string_list(room_tools["allow"], allow_path)
                    short_gid = (
                        group_id if len(group_id) <= 12 else group_id[:12] + "..."
                    )
                    if tool in allow:
                        report.changes.append(
                            LayerChange(
                                layer=f"room.allow[{short_gid}]",
                                path=allow_path,
                                action="already",
                            )
                        )
                    else:
                        if mutate:
                            allow.append(tool)
                            room_tools["allow"] = allow
                        report.changes.append(
                            LayerChange(
                                layer=f"room.allow[{short_gid}]",
                                path=allow_path,
                                action="add",
                            )
                        )

    # --- layer 3: tools.sandbox.tools.allow (sandboxed runs) ---
    tools_root_raw = target.get("tools")
    if tools_root_raw is None:
        if mutate:
            target["tools"] = {"sandbox": {"tools": {"allow": [tool]}}}
        report.changes.append(
            LayerChange(
                layer="sandbox.allow",
                path="tools.sandbox.tools.allow",
                action="seed+add",
                detail="created tools.sandbox.tools.allow",
            )
        )
    else:
        tools_root = _as_dict(tools_root_raw, "tools")
        target["tools"] = tools_root
        sandbox_raw = tools_root.get("sandbox")
        if sandbox_raw is None:
            if mutate:
                tools_root["sandbox"] = {"tools": {"allow": [tool]}}
            report.changes.append(
                LayerChange(
                    layer="sandbox.allow",
                    path="tools.sandbox.tools.allow",
                    action="seed+add",
                    detail="created tools.sandbox.tools.allow",
                )
            )
        else:
            sandbox = _as_dict(sandbox_raw, "tools.sandbox")
            tools_root["sandbox"] = sandbox
            sb_tools_raw = sandbox.get("tools")
            if sb_tools_raw is None:
                if mutate:
                    sandbox["tools"] = {"allow": [tool]}
                report.changes.append(
                    LayerChange(
                        layer="sandbox.allow",
                        path="tools.sandbox.tools.allow",
                        action="seed+add",
                        detail="created tools.sandbox.tools.allow",
                    )
                )
            else:
                sb_tools = _as_dict(sb_tools_raw, "tools.sandbox.tools")
                sandbox["tools"] = sb_tools
                allow_path = "tools.sandbox.tools.allow"
                if "allow" not in sb_tools or sb_tools.get("allow") is None:
                    if mutate:
                        sb_tools["allow"] = [tool]
                    report.changes.append(
                        LayerChange(
                            layer="sandbox.allow",
                            path=allow_path,
                            action="seed+add",
                            detail="created allow list",
                        )
                    )
                else:
                    allow = _string_list(sb_tools["allow"], allow_path)
                    if tool in allow:
                        report.changes.append(
                            LayerChange(
                                layer="sandbox.allow",
                                path=allow_path,
                                action="already",
                            )
                        )
                    else:
                        if mutate:
                            allow.append(tool)
                            sb_tools["allow"] = allow
                        report.changes.append(
                            LayerChange(
                                layer="sandbox.allow",
                                path=allow_path,
                                action="add",
                            )
                        )

    report.already_complete = not report.added
    return report


def focused_policy_snapshot(
    config: Mapping[str, object], intent: GrantIntent
) -> JsonObject:
    """Minimal JSON object for a redacted, secret-free diff.

    Only tool-policy paths the grant touches. Never includes ``env``, tokens, or
    MCP server bodies.
    """
    _index, agent = _find_agent(config, intent.agent_id)
    snap: JsonObject = {
        "agent_id": intent.agent_id,
        "tool_name": intent.tool_name,
        "agent_alsoAllow": None,
        "rooms": {},
        "sandbox_allow": None,
    }
    tools = agent.get("tools")
    if isinstance(tools, dict):
        also = tools.get("alsoAllow")
        if isinstance(also, list):
            snap["agent_alsoAllow"] = [x for x in also if isinstance(x, str)]

    rooms: dict[str, list[str]] = {}
    for group_id, tools_d, _path in _room_allow_targets(config, intent.agent_id):
        allow = tools_d.get("allow")
        if isinstance(allow, list):
            rooms[group_id] = [x for x in allow if isinstance(x, str)]
    snap["rooms"] = rooms

    tools_root = config.get("tools")
    if isinstance(tools_root, dict):
        sandbox = tools_root.get("sandbox")
        if isinstance(sandbox, dict):
            sb_tools = sandbox.get("tools")
            if isinstance(sb_tools, dict):
                allow = sb_tools.get("allow")
                if isinstance(allow, list):
                    snap["sandbox_allow"] = [x for x in allow if isinstance(x, str)]
    return snap


def redact_for_log(value: object, *, key_hint: str = "") -> object:
    """Recursively redact string values under secret-looking keys."""
    key_l = key_hint.lower()
    if any(h in key_l for h in _SECRET_KEY_HINTS):
        if isinstance(value, str):
            return "***"
        if isinstance(value, dict):
            return {str(k): "***" for k in value}
        if isinstance(value, list):
            return ["***" for _ in value]
        return "***"
    if isinstance(value, dict):
        out: JsonObject = {}
        for k, v in value.items():
            sk = str(k)
            out[sk] = redact_for_log(v, key_hint=sk)
        return out
    if isinstance(value, list):
        return [redact_for_log(v, key_hint=key_hint) for v in value]
    return value


def intent_to_public_dict(intent: GrantIntent) -> dict[str, str]:
    return {
        "op": "grant_tool",
        "agent_id": intent.agent_id,
        "tool_name": intent.tool_name,
    }
