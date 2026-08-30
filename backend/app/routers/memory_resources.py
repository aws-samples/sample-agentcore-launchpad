"""Memory resource management API — the mutating counterpart to ``routers/memory``.

The memory *console* router is read-only by construction (a structural test pins
that); this router owns the lifecycle of the AgentCore Memory resources
themselves: list the account's memories, create one, delete one. Agents pick a
memory at creation time via ``spec.memory.memory_id``; the workspace's bootstrap
memory is the default for agents that pick none, so it is delete-protected here
and a memory referenced by a live agent's spec refuses deletion too.
"""

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import Agent
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import memory_admin

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

    @field_validator("namespace_keys")
    @classmethod
    def _unique_keys(cls, keys: list[NamespaceKeyInput]) -> list[NamespaceKeyInput]:
        names = [k.key for k in keys]
        if len(names) != len(set(names)):
            raise ValueError("namespace keys must be unique")
        return keys


def _guard(fn, *args, **kwargs):
    """Map AWS/botocore failures onto the ``memory.unavailable`` envelope
    (mirrors ``routers/memory``); typed domain errors pass through."""
    try:
        return fn(*args, **kwargs)
    except AppError:
        raise
    except Exception as exc:  # botocore ClientError, endpoint errors, ...
        raise AppError(
            "memory.unavailable", f"memory operation failed: {exc}", status_code=502
        ) from exc


def _agents_by_memory(db: Session, ws: WorkspaceScope) -> dict[str, list[dict[str, str]]]:
    """Live agents grouped by the memory their spec pins (default users excluded).

    The reference lives inside the spec JSON, so this scans the workspace's
    agent rows in Python — bounded by the agent count, and only non-deleted rows.
    """
    usage: dict[str, list[dict[str, str]]] = {}
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == ws.id, Agent.status != "deleted")
        .all()
    )
    for agent in rows:
        mem_id = ((agent.spec or {}).get("memory") or {}).get("memory_id")
        if mem_id:
            usage.setdefault(mem_id, []).append({"id": agent.id, "name": agent.name})
    return usage


@router.get("")
def list_resources(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    page = _guard(memory_admin.list_memory_resources, ws.context)
    usage = _agents_by_memory(db, ws)
    for item in page["items"]:
        item["agents"] = usage.get(item["id"] or "", [])
    return page


@router.post("", status_code=201)
def create_resource(
    req: CreateMemoryResourceRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _guard(
        memory_admin.create_memory_resource,
        ws.context,
        req.name,
        req.description,
        req.event_expiry_days,
        req.strategies,
        [k.model_dump() for k in req.namespace_keys],
    )


@router.get("/{memory_id}")
def get_resource(
    memory_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return _guard(memory_admin.get_memory_resource, ws.context, memory_id)


@router.delete("/{memory_id}")
def delete_resource(
    memory_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agents = _agents_by_memory(db, ws).get(memory_id, [])
    if agents:
        raise AppError(
            "memory.in_use",
            f"{len(agents)} agent(s) still reference this memory — "
            "delete or re-point them first",
            {"agents": agents},
            status_code=409,
        )
    return _guard(memory_admin.delete_memory_resource, ws.context, memory_id)
