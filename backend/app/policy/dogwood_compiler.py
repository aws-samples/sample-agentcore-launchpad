"""The hub's door to the Dogwood engine binary's offline commands (stream C3; contract
docs/cn/interfaces/policy-principal-contracts.md, design plans/DESIGN-POLICY-USER-PRINCIPAL.md
§13-§15).

* ``compile(tools_list, policies)`` — validate + classify a workspace's statements against
  the schema generated from the Gateway's ``tools/list``: install items (one statement each,
  Stage ``a`` / ``bt``, ``stateless`` / ``temporal``; no synthetic closure permit since the
  Global model — every principal needs its own permit), Global's
  quotas, the temporal-referenced actions the interceptor must record.
* ``replay(...)`` — the console simulator: a draft / pinned set decided over a hand-written
  history on the engine's own code path.
* ``rewrite_d20(statement)`` — the one-time rewrite of a stored D20 (cedarpy-era) statement
  into the Global vocabulary (pure Python; the result is then validated by ``compile``).

Inert unless called; nothing on a request path imports it while
``Settings.policy_gateway_interceptor`` is off. The binary is ``LAUNCHPAD_POLICY_ENGINE_BIN``,
else the installed ``/opt/launchpad-policy-engine/bin/launchpad-policy-engine``, else the
repo build ``policy-engine/target/release/launchpad-policy-engine``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_BIN = "LAUNCHPAD_POLICY_ENGINE_BIN"
INSTALLED_BIN = Path("/opt/launchpad-policy-engine/bin/launchpad-policy-engine")
REPO_BIN = (
    Path(__file__).resolve().parents[3] / "policy-engine" / "target" / "release"
    / "launchpad-policy-engine"
)
TIMEOUT_S = 30


class EngineUnavailable(RuntimeError):
    """The engine binary is missing or failed to answer (not a policy error)."""


def engine_bin() -> Path:
    for candidate in (os.environ.get(ENV_BIN), INSTALLED_BIN, REPO_BIN):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise EngineUnavailable("launchpad-policy-engine binary not found")


def _run(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed binary, JSON on stdin
            [str(engine_bin()), command], input=json.dumps(payload).encode(),
            capture_output=True, timeout=TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineUnavailable(f"{command}: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise EngineUnavailable(
            f"{command} exited {proc.returncode}: {proc.stderr.decode(errors='replace')[:300]}"
        )
    return json.loads(proc.stdout)


@dataclass
class Compiled:
    ok: bool
    schema: str
    items: list[dict[str, Any]]
    errors: list[dict[str, str]]
    warnings: list[dict[str, str]]
    actions: list[str] = field(default_factory=list)
    temporal_count: int = 0
    temporal_referenced_actions: list[str] = field(default_factory=list)
    closure_actions: list[str] = field(default_factory=list)

    def errors_for(self, policy_id: str) -> list[str]:
        """Messages for one policy (``pol_x`` also matches its split ``pol_x#k`` parts)."""
        return [e["message"] for e in self.errors
                if e["policy_id"] == policy_id or e["policy_id"].startswith(f"{policy_id}#")]


def compile(tools_list: dict[str, Any], policies: list[dict[str, Any]]) -> Compiled:  # noqa: A001
    """``policies`` = ``[{id, version, mode, statement}]`` (the ACTIVE / draft / pinned rows
    a caller picked). Raises ``EngineUnavailable`` only for a missing / broken binary."""
    out = _run("compile", {"tools_list": tools_list, "policies": policies})
    return Compiled(**{k: out.get(k) for k in Compiled.__dataclass_fields__ if k in out})


def replay(
    tools_list: dict[str, Any], policies: list[dict[str, Any]], *, agent: dict[str, Any],
    request: dict[str, Any], history: list[dict[str, Any]] | None = None,
    channel: str = "chat", ws: str = "sim",
) -> dict[str, Any]:
    """One simulated decision after ``history`` (each ``{tool, kind, principal, input,
    output, at_s}``) — ``{ok, decision{decision, stage_a, stage_bt, determining, …},
    trace[]}``, or ``{ok: false, errors[]}`` when the set does not compile. ``ws`` is the
    workspace the set is simulated under: pass the real id so rules naming the workspace
    (``principal in AgentCore::Workspace::"<ws>"``) decide as at the gateway (N17-obs495)."""
    return _run("replay", {
        "tools_list": tools_list, "policies": policies, "agent": agent,
        "history": history or [], "request": request, "channel": channel, "ws": ws,
    })


# ── D20 → Global vocabulary (Cedar EST, never text substitution) ──────────────

_ENTITY_RENAMES = {f"Launchpad::{name}": f"AgentCore::{name}" for name in (
    "User", "Role", "ApiKey", "System", "Agent", "Workspace", "Gateway",
)}
INVOKE_DROPPED = ("statement dropped: the invoke action no longer exists — policies decide "
                  "Gateway tool calls only (contract amendment 9); retire this policy")


@dataclass
class Rewrite:
    statement: str
    warnings: list[str]


def _entity_expr(type_: str, id_: str) -> dict[str, Any]:
    return {"Value": {"__entity": {"type": type_, "id": id_}}}


def _binary_expr(op: str, left: dict, right: dict) -> dict:
    return {op: {"left": left, "right": right}}


def _attribute(var: str, attr: str) -> dict:
    return {".": {"left": {"Var": var}, "attr": attr}}


def _entity(node: Any) -> dict:
    if isinstance(node, dict) and isinstance(node.get("Value"), dict):
        return node["Value"].get("__entity", {})
    return {}


def _names_invoke(node: Any) -> bool:
    if isinstance(node, dict):
        if node == {"type": "Launchpad::Action", "id": "invoke"}:
            return True
        # Annotations and literal strings are data, not policy code.
        return any(_names_invoke(v) for k, v in node.items() if k != "annotations")
    return isinstance(node, list) and any(_names_invoke(v) for v in node)


def _tool_test(op: str, value: str) -> dict:
    return _binary_expr(op, {"Var": "action"}, _entity_expr("AgentCore::Action", value))


def _rewrite_expr(node: Any, warnings: list[str]) -> Any:
    """Rewrite leaves in place; Boolean operators, polarity and order remain in the EST."""
    if isinstance(node, list):
        return [_rewrite_expr(v, warnings) for v in node]
    if not isinstance(node, dict):
        return node
    # Record keys are user data, not EST operator tags (a field may be named "Value").
    if "Record" in node:
        return {"Record": {k: _rewrite_expr(v, warnings) for k, v in node["Record"].items()}}
    # Values (including records/string contents) are opaque except for entity literals.
    if "Value" in node:
        entity = _entity(node)
        if entity:
            return _entity_expr(_ENTITY_RENAMES.get(entity["type"], entity["type"]), entity["id"])
        return node
    for op in ("==", "in"):
        if op not in node:
            continue
        left, right = node[op]["left"], node[op]["right"]
        if op == "==" and right in ({"Var": "resource"}, {"Var": "action"},
                                      {"Var": "principal"}, _attribute("resource", "name"),
                                      _attribute("resource", "target")):
            left, right = right, left
        entity = _entity(right)
        if left == {"Var": "resource"} and entity.get("type") == "Launchpad::Tool":
            return _tool_test(op, entity["id"])
        if op == "in" and left == {"Var": "resource"} and "Set" in right:
            tools = [_entity(v) for v in right["Set"]]
            if all(t.get("type") == "Launchpad::Tool" for t in tools):
                return _binary_expr("in", {"Var": "action"}, {"Set": [
                    _entity_expr("AgentCore::Action", t["id"]) for t in tools
                ]})
        if left == {"Var": "action"} and entity == {
            "type": "Launchpad::Action", "id": "call_tool",
        }:
            return _tool_test("in", "CallTool")
        if left == {"Var": "principal"} and entity.get("type") == "Launchpad::Agent":
            return _binary_expr("in", left, _rewrite_expr(right, warnings))
        value = right.get("Value")
        if op == "==" and isinstance(value, str):
            if left == _attribute("resource", "name"):
                return _tool_test("==", value)
            if left == _attribute("resource", "target"):
                return _tool_test("in", f"target:{value}")
    if "like" in node and node["like"]["left"] == _attribute("resource", "name"):
        pattern = node["like"]["pattern"]
        literals = pattern[:-1] if pattern and pattern[-1] == "Wildcard" else pattern
        if all(isinstance(p, dict) and "Literal" in p for p in literals):
            value = "".join(p["Literal"] for p in literals)
            if literals == pattern:
                return _tool_test("==", value)
            # Gateway target groups use the prefix before the FIRST triple underscore.
            if value.endswith("___") and "___" not in value[:-3]:
                return _tool_test("in", f"target:{value[:-3]}")
        warnings.append("a tool pattern has no exact action-group equivalent; edit it")
    if "." in node:
        attr = node["."]
        if attr["left"] == {"Var": "context"}:
            if attr["attr"] in ("agent_id", "channel"):
                return {".": {"left": _attribute("context", "launchpad"), "attr": attr["attr"]}}
            if attr["attr"] in ("session_id", "env"):
                warnings.append(
                    "context.session_id / context.env have no Global equivalent; edit them")
    result = {k: _rewrite_expr(v, warnings) for k, v in node.items()}
    if "is" in node:
        type_ = node["is"]["entity_type"]
        result["is"]["entity_type"] = (
            "AgentCore::IamEntity" if type_ == "Launchpad::Agent"
            else _ENTITY_RENAMES.get(type_, type_)
        )
    return result


def _rewrite_scope(scope: dict, *, principal: bool = False) -> dict:
    result = dict(scope)
    if "entity" in scope:
        entity = scope["entity"]
        result["entity"] = {**entity, "type": _ENTITY_RENAMES.get(entity["type"], entity["type"])}
        if principal and entity["type"] == "Launchpad::Agent" and scope["op"] == "==":
            result["op"] = "in"
    if "entity_type" in scope:
        type_ = scope["entity_type"]
        result["entity_type"] = ("AgentCore::IamEntity" if type_ == "Launchpad::Agent"
                                 else _ENTITY_RENAMES.get(type_, type_))
    return result


def rewrite_d20(statement: str) -> Rewrite:
    """Translate each static policy through Cedar's EST. A Tool is an action on the Gateway;
    predicates become action tests exactly where they occurred, including under ``unless``.
    The stored source is unchanged. Unsupported tests stay fail-closed at schema compilation.
    """
    from app.services.policy_cedar import CedarEngine

    cedar = CedarEngine()
    policies = cedar.parse_policies(statement)
    kept, warnings = {}, []
    for key, policy in policies.items():
        if _names_invoke(policy):
            warnings.append(INVOKE_DROPPED)
            continue
        policy["principal"] = _rewrite_scope(policy["principal"], principal=True)
        action = policy["action"]
        if action.get("entity") == {"type": "Launchpad::Action", "id": "call_tool"}:
            policy["action"] = {"op": "in", "entity": {
                "type": "AgentCore::Action", "id": "CallTool",
            }}
        elif action.get("entities") == [{"type": "Launchpad::Action", "id": "call_tool"}]:
            policy["action"] = {"op": "in", "entity": {
                "type": "AgentCore::Action", "id": "CallTool",
            }}
        scope = policy["resource"]
        entity = scope.get("entity", {})
        if entity.get("type") == "Launchpad::Tool":
            # A resource scope is an implicit AND, unlike a predicate within a condition.
            policy["resource"] = {"op": "All"}
            policy["conditions"].append({
                "kind": "when", "body": _tool_test(scope["op"], entity["id"]),
            })
        else:
            policy["resource"] = _rewrite_scope(scope)
        policy["conditions"] = [
            {**c, "body": _rewrite_expr(c["body"], warnings)} for c in policy["conditions"]
        ]
        kept[key] = policy
    return Rewrite(cedar.render_policies(kept), warnings)
