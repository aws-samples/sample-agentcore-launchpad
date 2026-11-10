"""Entities of the per-user principal work in the single ``AgentCore`` namespace (stream A4;
contract amendment 1 in docs/cn/interfaces/policy-principal-contracts.md, design §13.3).

STDLIB ONLY and no ``app`` import, so stream C's compiler (and the interceptor, by vendoring)
builds exactly the same entity ids and graphs as the hub:

* ``ref`` — a Cedar entity literal ``AgentCore::<Type>::"<id>"`` (I4 ``entity`` pattern);
* ``principal_descriptor`` — the I4 ``principal`` object of a token ``p`` claim: ``User``
  (parent ``Role``; attrs ``username``, ``role``, ``user_id?``), ``ApiKey`` (attr ``name``,
  NO parent — D1 revised) or ``System`` (no attrs);
* ``agent_descriptor`` / ``agent_entities`` — the calling agent as Global names an IAM
  caller: ``IamEntity::"<role arn>"`` in ``Agent::"<id>"`` in ``Workspace::"<ws>"``;
* ``tool_action`` / ``target_group`` / ``gateway_resource`` — per-tool actions
  ``<target>___<tool>`` (D8), the ``target:<t>`` action groups and the Gateway resource.

Entities must conform to the generated schema exactly — a missing required attribute is a
fail-closed deny — so nothing here adds an attribute the contract does not list.
"""

from __future__ import annotations

from typing import Any

NAMESPACE = "AgentCore"
TOOL_SEPARATOR = "___"
TARGET_GROUP_PREFIX = "target:"
ROLES = ("admin", "member")


def _type(name: str) -> str:
    if not name or "::" in name or not name[0].isalpha() or not name.replace("_", "").isalnum():
        raise ValueError(f"not a bare entity type name: {name!r}")
    return f"{NAMESPACE}::{name}"


def ref(type_name: str, entity_id: str) -> str:
    """``AgentCore::<Type>::"<id>"`` with Cedar string escaping of ``\\`` and ``"``."""
    escaped = str(entity_id).replace("\\", "\\\\").replace('"', '\\"')
    return f'{_type(type_name)}::"{escaped}"'


def uid(type_name: str, entity_id: str) -> dict[str, str]:
    """The Cedar JSON entity uid ``{type, id}``."""
    return {"type": _type(type_name), "id": str(entity_id)}


def _entity(type_name: str, entity_id: str, attrs: dict[str, Any],
            parents: list[dict[str, str]]) -> dict[str, Any]:
    return {"uid": uid(type_name, entity_id), "attrs": attrs, "parents": parents}


def _principal_parts(claim: dict[str, Any]) -> tuple[str, str, dict[str, Any], list[str]]:
    kind, pid = claim.get("kind"), claim.get("id")
    if not isinstance(pid, str) or not pid:
        raise ValueError("a principal needs an id")
    if kind == "user":
        role = claim.get("role")
        if role not in ROLES:
            raise ValueError(f"a user principal needs admin|member, got {role!r}")
        attrs: dict[str, Any] = {"username": pid, "role": role}
        if claim.get("user_id"):
            attrs["user_id"] = claim["user_id"]
        return "User", pid, attrs, [role]
    if kind == "api_key":
        return "ApiKey", pid, {"name": claim.get("label") or pid}, []
    if kind == "system":
        return "System", pid, {}, []
    raise ValueError(f"unknown principal kind {kind!r}")


def principal_descriptor(claim: dict[str, Any]) -> dict[str, Any]:
    """The I4 ``principal`` object (``{entity, parents, attrs}``) of a token ``p`` claim."""
    type_name, pid, attrs, roles = _principal_parts(claim)
    return {"entity": ref(type_name, pid), "parents": [ref("Role", r) for r in roles],
            "attrs": attrs}


def principal_entities(claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Cedar JSON entities of the principal (+ its ``Role`` for a user)."""
    type_name, pid, attrs, roles = _principal_parts(claim)
    out = [_entity(type_name, pid, attrs, [uid("Role", r) for r in roles])]
    out += [_entity("Role", r, {}, []) for r in roles]
    return out


def agent_descriptor(role_arn: str, agent_id: str, *, method: str, system: bool,
                     workspace_id: str | None = None) -> dict[str, Any]:
    """The I4 ``agent`` object: the caller resolved from the SigV4 ``awsPrincipalArn``."""
    descriptor = {"entity": ref("IamEntity", role_arn), "agent_id": agent_id, "method": method,
                  "system": bool(system)}
    if workspace_id is not None:
        descriptor["workspace_id"] = workspace_id
    return descriptor


def agent_entities(role_arn: str, agent_id: str, workspace_id: str) -> list[dict[str, Any]]:
    """``IamEntity::"<role>"`` in ``Agent::"<id>"`` in ``Workspace::"<ws>"``."""
    return [
        _entity("IamEntity", role_arn, {}, [uid("Agent", agent_id)]),
        _entity("Agent", agent_id, {}, [uid("Workspace", workspace_id)]),
        _entity("Workspace", workspace_id, {}, []),
    ]


def tool_action(target: str, tool: str) -> str:
    """The per-tool action (D8) — the REAL tool id, ``-`` kept (amendment 1)."""
    return ref("Action", f"{target}{TOOL_SEPARATOR}{tool}")


def target_group(target: str) -> str:
    return ref("Action", f"{TARGET_GROUP_PREFIX}{target}")


def gateway_resource(gateway_arn: str) -> str:
    return ref("Gateway", gateway_arn)
