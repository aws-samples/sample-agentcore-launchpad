"""Memory resource management API — the mutating counterpart to ``routers/memory``.

The memory *console* router is read-only by construction (a structural test pins
that); this router owns the lifecycle of the memory stores themselves
(through the ``MemoryBackend`` Protocol — 503 ``feature.unavailable`` until N6
registers one): list the account's memories, create one, edit one's description /
event expiry, delete one. Agents pick a memory at creation time via
``spec.memory.memory_id``; the workspace's bootstrap memory is the default for
agents that pick none, so it is delete-protected here and a memory referenced by
a live agent's spec refuses deletion too. Editing is never blocked: a new
description or expiry is harmless to the agents on the memory (the console
merely names them in its confirm dialog).

Ownership (issue #55): the account can hold memories the platform never
created, so the list marks each row ``managed`` (``services/memory_ownership``)
and every per-id route answers ``404 memory.not_managed`` for the rest — they
are *detected, not managed*. A memory becomes managed when this router creates
it, or when an administrator adopts it explicitly.
"""

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, mapped_aws_error, mapped_opensearch_error
from app.models.ledger import Agent, ManagedMemory
from app.routers.memory import _unavailable
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, creators, memory_backend, memory_ownership
from app.services import opensearch_http as memory_core
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/memory/resources", tags=["memory-resources"])


class NamespaceKeyInput(BaseModel):
    """One custom namespace variable key (CreateMemory ``namespaceKeys`` entry).

    Constraints mirror the AWS shapes so bad input is a 422 with a field
    pointer instead of a mid-request ValidationException. Keys are lowercase
    alphanumeric by the API's own pattern, which also rules out the built-in
    variable names (``actorId``, ``sessionId``, ``memoryStrategyId``).
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9]{0,31}$")
    # up to 10 permitted runtime values (AND-ed with regex_pattern when both set)
    allowed_values: list[str] | None = Field(default=None, min_length=1, max_length=10)
    regex_pattern: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("allowed_values")
    @classmethod
    def _values_shape(cls, values: list[str] | None) -> list[str] | None:
        for value in values or []:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
                raise ValueError(
                    f"allowed value {value!r} must be lowercase alphanumeric "
                    "with hyphens/underscores (max 64 chars)"
                )
        return values


def _bounded(key: str) -> dict[str, int]:
    low, high = memory_core.MERGE_SETTINGS_BOUNDS[key]
    return {"ge": low, "le": high}


class MergeSettingsInput(BaseModel):
    """Merge strategy v2 knobs of a store (r14-a, N14-obs1). Every field optional: on create
    the omitted ones take the defaults; on update the body is a partial patch and an explicit
    ``null`` resets that knob to its default. Bounds mirror
    ``opensearch_http.MERGE_SETTINGS_BOUNDS`` so bad input is a 422 with a field pointer."""

    model_config = {"extra": "forbid"}

    recall_half_life_days: int | None = Field(default=None, **_bounded("recall_half_life_days"))
    max_facts_per_actor: int | None = Field(default=None, **_bounded("max_facts_per_actor"))
    max_preferences_per_actor: int | None = Field(
        default=None, **_bounded("max_preferences_per_actor")
    )
    long_term_ttl_days: int | None = Field(default=None, **_bounded("long_term_ttl_days"))
    actor_summary_interval: int | None = Field(default=None, **_bounded("actor_summary_interval"))

    def patch(self) -> dict[str, Any]:
        """Only the keys the client sent (``null`` included — it means "reset")."""
        return self.model_dump(exclude_unset=True)


class CreateMemoryResourceRequest(BaseModel):
    """CreateMemory surface the console exposes — name + expiry + strategy picks."""

    # the CreateMemory API's own name constraint, checked here so a bad name is
    # a 422 with a field pointer instead of a mid-request AWS ValidationException
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
    description: str = Field(default="", max_length=4096)
    event_expiry_days: int = Field(default=30, ge=3, le=365)
    # default mirrors the bootstrap memory so per-agent memories behave the same
    strategies: list[str] = Field(
        default_factory=lambda: ["semantic", "user_preference"], max_length=4
    )
    # flexible namespace variables — up to 5 keys per memory resource
    namespace_keys: list[NamespaceKeyInput] = Field(default_factory=list, max_length=5)
    # r14-a: merge strategy v2 knobs (omitted = defaults)
    merge: MergeSettingsInput | None = None

    @field_validator("namespace_keys")
    @classmethod
    def _unique_keys(cls, keys: list[NamespaceKeyInput]) -> list[NamespaceKeyInput]:
        names = [k.key for k in keys]
        if len(names) != len(set(names)):
            raise ValueError("namespace keys must be unique")
        return keys


class UpdateMemoryResourceRequest(BaseModel):
    """UpdateMemory surface the console exposes — description and/or event expiry.

    Both optional, at least one required. Strategies, namespace keys, execution
    role, indexed keys and stream delivery are deliberately absent: the service
    never forwards them (``namespaceKeys`` in particular replaces the existing set
    wholesale, so it must not be echoed — see ``MemoryBackend.update_memory_resource``).
    """

    # UpdateMemory's own description shape is 1–4096 chars: an existing
    # description cannot be cleared through the API, only replaced
    description: str | None = Field(default=None, min_length=1, max_length=4096)
    event_expiry_days: int | None = Field(default=None, ge=7, le=365)
    # r14-a: a partial patch of the merge strategy v2 knobs (`null` resets one)
    merge: MergeSettingsInput | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateMemoryResourceRequest":
        if (
            self.description is None and self.event_expiry_days is None
            and not (self.merge and self.merge.patch())
        ):
            raise ValueError("provide description, event_expiry_days and/or merge")
        return self


def _guard(fn, *args, **kwargs):
    """Map AWS/botocore failures onto the ``memory.unavailable`` envelope
    (mirrors ``routers/memory``); typed domain errors pass through, and a
    ``ClientError`` the console can translate (``ResourceNotFoundException`` for
    an unknown memory id, ``AccessDeniedException``, …), or a self-hosted-
    substrate ``OpenSearchError`` the console can translate (N6 fix round 2), is
    left to the matching global handler in ``app.core.errors`` so the toast
    reads "not found" or "backend unavailable", not boto/OpenSearch text."""
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except Exception as exc:  # botocore ClientError, OpenSearchError, ...
        if mapped_aws_error(exc) or mapped_opensearch_error(exc):
            raise
        raise _unavailable("memory operation failed", fn, exc) from exc  # N17-obs92: static


def _agents_by_memory(
    db: Session, ws: WorkspaceScope, viewer: Viewer = Viewer.ALL
) -> dict[str, list[dict[str, str]]]:
    """Live agents grouped by the memory their spec pins (default users excluded).

    The reference lives inside the spec JSON, so this scans the workspace's
    agent rows in Python — bounded by the agent count, and only non-deleted rows.
    Agents hidden from ``viewer`` (workspace isolation) are not named; callers that
    need the true usage (the delete guard) count with ``Viewer.ALL``.
    """
    hidden = agent_access.hidden_agent_ids(db, viewer, ws.id)
    usage: dict[str, list[dict[str, str]]] = {}
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == ws.id, Agent.status != "deleted")
        .all()
    )
    for agent in rows:
        mem_id = ((agent.spec or {}).get("memory") or {}).get("memory_id")
        if mem_id and agent.id not in hidden:
            usage.setdefault(mem_id, []).append({"id": agent.id, "name": agent.name})
    return usage


def _creator_of(db: Session, ws: WorkspaceScope, memory_id: str) -> dict[str, Any]:
    """`created_by` / `created_by_name` of a managed memory (nulls for the
    bootstrap / default store, which has no row)."""
    row = (
        db.query(ManagedMemory)
        .filter(ManagedMemory.workspace_id == ws.id, ManagedMemory.memory_id == memory_id)
        .first()
    )
    return creators.fields(row)


@router.get("")
def list_resources(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    page = _guard(memory_backend.get_backend().list_memory_resources, ws.context)
    # a managed memory another member created is not listed under isolation
    hidden = memory_ownership.hidden_ids(db, ws.context, viewer)
    if hidden:
        page["items"] = [i for i in page["items"] if (i.get("id") or "") not in hidden]
    usage = _agents_by_memory(db, ws, viewer)
    managed = memory_ownership.managed_ids(db, ws.context)
    rows = memory_ownership.managed_rows(db, ws.context)
    for item in page["items"]:
        item["agents"] = usage.get(item["id"] or "", [])
        # everything else in the account is detected, not managed: listed, but no
        # per-id route reaches it and no agent may pin it
        item["managed"] = bool(item["id"]) and item["id"] in managed
        # the default store and detected memories have no row → no creator
        item.update(creators.fields(rows.get(item["id"] or "")))
    return page


@router.post("", status_code=201)
def create_resource(
    req: CreateMemoryResourceRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    created = _guard(
        memory_backend.get_backend().create_memory_resource,
        ws.context,
        req.name,
        req.description,
        req.event_expiry_days,
        req.strategies,
        [k.model_dump() for k in req.namespace_keys],
        merge=req.merge.patch() if req.merge else None,
    )
    memory_ownership.register(
        db,
        ws.context,
        created.get("id") or "",
        origin="created",
        creator=Creator.of_request(request),
    )
    return {**created, **_creator_of(db, ws, created.get("id") or ""), "managed": True}


@router.get("/{memory_id}")
def get_resource(
    memory_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    memory_ownership.require_managed(db, ws.context, memory_id, viewer=viewer)
    detail = _guard(memory_backend.get_backend().get_memory_resource, ws.context, memory_id)
    return {**detail, **_creator_of(db, ws, memory_id), "managed": True}


@router.post("/{memory_id}/adopt")
def adopt_resource(
    memory_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Bring an existing memory of this account under the workspace's management.

    Administrator-only (route policy): adopting is the governance decision that
    lets members pin, edit and delete a resource the platform did not create —
    e.g. one created through the console before ownership was recorded. The id
    must resolve with ``GetMemory`` first (an unknown id is ``404 aws.not_found``).
    """
    detail = _guard(memory_backend.get_backend().get_memory_resource, ws.context, memory_id)
    memory_ownership.register(
        db,
        ws.context,
        detail.get("id") or memory_id,
        origin="adopted",
        creator=Creator.of_request(request),
    )
    return {**detail, **_creator_of(db, ws, detail.get("id") or memory_id), "managed": True}


@router.put("/{memory_id}")
def update_resource(
    memory_id: str,
    req: UpdateMemoryResourceRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Edit description / event expiry; the reply is the refreshed detail.

    No in-use or platform-default guard here, unlike delete: neither field can
    break the agents writing to the memory. The console lists those agents in its
    confirm dialog (from the list route's ``agents`` annotation) so the operator
    knows whom a shorter expiry window reaches.
    """
    memory_ownership.require_managed(db, ws.context, memory_id, viewer=viewer)
    detail = _guard(
        memory_backend.get_backend().update_memory_resource,
        ws.context,
        memory_id,
        description=req.description,
        event_expiry_days=req.event_expiry_days,
        merge=req.merge.patch() if req.merge else None,
    )
    return {**detail, **_creator_of(db, ws, memory_id), "managed": True}


@router.delete("/{memory_id}")
def delete_resource(
    memory_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    memory_ownership.require_managed(db, ws.context, memory_id, viewer=viewer)
    # the guard counts every agent; the 409 names only those the caller may see
    agents = _agents_by_memory(db, ws).get(memory_id, [])
    if agents:
        hidden = agent_access.hidden_agent_ids(db, viewer, ws.id)
        raise AppError(
            "memory.in_use",
            f"{len(agents)} agent(s) still reference this memory — "
            "delete or re-point them first",
            {"agents": [a for a in agents if a["id"] not in hidden]},
            status_code=409,
        )
    deleted = _guard(memory_backend.get_backend().delete_memory_resource, ws.context, memory_id)
    memory_ownership.forget(db, ws.context, memory_id)
    return deleted
