"""Cedar entities and request contexts built from the ledger (plan §2.1).

Entities are built **per request** from rows the request already holds: the console
``Identity`` (→ ``Launchpad::User`` with its ``Role`` parent), the ``/v1`` ``ApiKey``
row, the ``Agent`` row (``system_key`` → ``system = true``, parent ``Workspace``) and the
Gateway tools named ``<target>___<tool>`` (``name`` = that full id, so ``like
"hr-database___*"`` rules mirror the ``LAUNCHPAD_GATEWAY_TOOLS`` patterns; ``tool`` /
``target`` are the two halves). Attribute sets match the schema exactly — an
attribute the schema does not declare turns the decision into ``NoDecision`` (fail-closed),
so nothing here guesses.

``Principal`` is the hub's view of *who* is asking (console user, API key, an agent for
deploy-time compilation, or ``System::"hub"`` for internal callers — plan §4.3); the
invoke chain builds it from ``routers/chat.py`` / ``routers/public_api.py`` for the
principal token, the simulator for a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.models.ledger import Agent, ApiKey
from app.policy import CONTEXT_ATTRIBUTES, entity_type
from app.services.policy_backend import EntityRef

PrincipalKind = Literal["user", "api_key", "agent", "system"]

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
SYSTEM_HUB_ID = "hub"
TOOL_SEPARATOR = "___"

_KIND_TYPES: dict[str, str] = {
    "user": entity_type("User"),
    "api_key": entity_type("ApiKey"),
    "agent": entity_type("Agent"),
    "system": entity_type("System"),
}


@dataclass(frozen=True)
class Principal:
    """Who is asking. ``id`` is the Cedar entity id operators write in rules: the
    username for a console user, the key id for an API key, the agent id for an agent."""

    kind: PrincipalKind
    id: str
    label: str = ""
    role: str | None = None  # users only: ``admin`` | ``member``
    attrs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_identity(cls, identity: Any) -> Principal:
        """From ``routers/auth.Identity`` (console session)."""
        attrs: dict[str, Any] = {"username": identity.username, "role": identity.role}
        if getattr(identity, "user_id", None):
            attrs["user_id"] = identity.user_id
        return cls(kind="user", id=identity.username, label=identity.username,
                   role=identity.role, attrs=attrs)

    @classmethod
    def from_api_key(cls, key: ApiKey) -> Principal:
        attrs: dict[str, Any] = {"name": key.name}
        # `created_by?: String` in the Cedar schema is the minting USERNAME (the column
        # held one before #259 re-pointed it to `users.id`; the name moved beside it)
        if getattr(key, "created_by_name", None):
            attrs["created_by"] = key.created_by_name
        return cls(kind="api_key", id=key.id, label=key.name, attrs=attrs)

    @classmethod
    def from_agent(cls, agent: Agent) -> Principal:
        return cls(kind="agent", id=agent.id, label=agent.name)

    @classmethod
    def system(cls, id: str = SYSTEM_HUB_ID) -> Principal:
        return cls(kind="system", id=id, label=f"system:{id}")

    @classmethod
    def from_request(cls, kind: str, id: str, *, role: str | None = None) -> Principal:
        """The simulator's picker: ``{kind, id}`` without a ledger row behind it."""
        if kind not in _KIND_TYPES:
            raise ValueError(f"unknown principal kind {kind!r}")
        attrs: dict[str, Any] = {}
        if kind == "user":
            attrs = {"username": id, "role": role or ROLE_MEMBER}
        elif kind == "api_key":
            attrs = {"name": id}
        return cls(kind=kind, id=id, label=id, role=role, attrs=attrs)  # type: ignore[arg-type]

    @property
    def entity_type(self) -> str:
        return _KIND_TYPES[self.kind]

    def ref(self) -> EntityRef:
        return EntityRef(self.entity_type, self.id)

    def entities(self, agent: Agent | None = None, workspace_id: str | None = None) -> list[dict]:
        """The principal's own entity (+ parents). An ``agent`` principal is described by
        ``agent_entities`` instead so the attributes come from the row."""
        if self.kind == "agent":
            return agent_entities(agent, workspace_id) if agent is not None else [
                _entity(self.entity_type, self.id, {}, [])
            ]
        parents: list[dict[str, str]] = []
        attrs = dict(self.attrs)
        if self.kind == "user":
            role = self.role or attrs.get("role") or ROLE_MEMBER
            attrs.setdefault("username", self.id)
            attrs["role"] = role
            parents.append(_uid(entity_type("Role"), role))
        elif self.kind == "api_key":
            attrs.setdefault("name", self.label or self.id)
        else:
            attrs = {}
        return [_entity(self.entity_type, self.id, attrs, parents)]


def _uid(type_: str, id_: str) -> dict[str, str]:
    return {"type": type_, "id": id_}


def _entity(type_: str, id_: str, attrs: dict[str, Any], parents: list[dict[str, str]]) -> dict:
    return {"uid": _uid(type_, id_), "attrs": attrs, "parents": parents}


def role_entities() -> list[dict]:
    return [
        _entity(entity_type("Role"), ROLE_ADMIN, {}, []),
        _entity(entity_type("Role"), ROLE_MEMBER, {}, []),
    ]


def workspace_entity(workspace_id: str) -> dict:
    return _entity(entity_type("Workspace"), workspace_id, {}, [])


def agent_ref(agent: Agent | str) -> EntityRef:
    return EntityRef(entity_type("Agent"), agent if isinstance(agent, str) else agent.id)


def agent_entities(agent: Agent, workspace_id: str | None = None) -> list[dict]:
    attrs: dict[str, Any] = {
        "name": agent.name,
        "method": agent.method or "",
        "system": bool(getattr(agent, "system_key", None)),
    }
    # `owner?: String` stays the Cedar schema attribute (policy-backend.md contract); its
    # value is the creator's USERNAME — `created_by_name` since Family B step 1 retired
    # `Agent.owner` — so a policy written against it keeps matching
    if getattr(agent, "created_by_name", None):
        attrs["owner"] = agent.created_by_name
    ws = workspace_id or agent.workspace_id
    parents = [_uid(entity_type("Workspace"), ws)] if ws else []
    return [_entity(entity_type("Agent"), agent.id, attrs, parents)]


def tool_id(target: str, tool: str) -> str:
    return f"{target}{TOOL_SEPARATOR}{tool}"


def split_tool_id(tool_id_: str) -> tuple[str, str]:
    """``hr-database___create_payout`` → ``("hr-database", "create_payout")``; a bare name
    has an empty target."""
    target, sep, tool = tool_id_.partition(TOOL_SEPARATOR)
    return (target, tool) if sep else ("", tool_id_)


def tool_ref(tool_id_: str) -> EntityRef:
    return EntityRef(entity_type("Tool"), tool_id_)


def tool_entities(
    tool_ids: list[str] | tuple[str, ...],
    *,
    agent_id: str | None = None,
    gateway_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict]:
    out = []
    if gateway_id:
        parents = [_uid(entity_type("Workspace"), workspace_id)] if workspace_id else []
        out.append(_entity(entity_type("Gateway"), gateway_id, {"gateway_id": gateway_id}, parents))
    for tid in dict.fromkeys(tool_ids):
        target, tool = split_tool_id(tid)
        parents = []
        if agent_id:
            parents.append(_uid(entity_type("Agent"), agent_id))
        if gateway_id:
            parents.append(_uid(entity_type("Gateway"), gateway_id))
        attrs = {"name": tid, "tool": tool, "target": target}
        out.append(_entity(entity_type("Tool"), tid, attrs, parents))
    return out


def build_entities(
    principal: Principal,
    *,
    workspace_id: str,
    agent: Agent | None = None,
    tool_ids: list[str] | tuple[str, ...] = (),
    gateway_id: str | None = None,
) -> list[dict]:
    """The complete entity set for one decision — principal, roles, workspace, the
    agent (as resource or as the tools' parent) and the tools."""
    entities: list[dict] = []
    entities.extend(role_entities())
    entities.append(workspace_entity(workspace_id))
    entities.extend(principal.entities(agent, workspace_id))
    if agent is not None and principal.kind != "agent":
        entities.extend(agent_entities(agent, workspace_id))
    if tool_ids:
        entities.extend(
            tool_entities(
                tool_ids, agent_id=agent.id if agent is not None else None,
                gateway_id=gateway_id, workspace_id=workspace_id,
            )
        )
    return _dedupe(entities)


def _dedupe(entities: list[dict]) -> list[dict]:
    seen: dict[tuple[str, str], dict] = {}
    for e in entities:
        seen.setdefault((e["uid"]["type"], e["uid"]["id"]), e)
    return list(seen.values())


def request_context(action: str, **values: Any) -> dict[str, Any]:
    """The context record for ``action``, filtered to the attributes the schema declares
    (an undeclared one would make the decision ``NoDecision``) with required strings
    defaulted to ``""`` so a missing value never fails the request build. The caller's
    ``input`` (tool arguments) is dropped here — see ``policy-backend.md`` §Schema."""
    declared = CONTEXT_ATTRIBUTES.get(action)
    if declared is None:
        raise ValueError(f"unknown action {action!r}")
    context: dict[str, Any] = {}
    for name in declared:
        value = values.get(name)
        if value is None:
            if name == "session_id":
                continue  # optional in the schema
            value = ""
        context[name] = str(value)
    return context
