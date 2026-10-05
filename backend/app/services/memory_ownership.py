"""Which AgentCore Memory resources a workspace *manages* (issue #55).

A workspace is one AWS account × region, but the account boundary is not the
workspace boundary: the account can hold memories the platform never created —
another team's, another tool's — and the spoke role reaches all of them
(``bedrock-agentcore:*`` on ``*``). So an id is never trusted because AWS
answers for it. A memory is **managed** exactly when it is

* the workspace's bootstrap memory (``resources.memory_id``), or
* named by a ``ManagedMemory`` row — written only when the console creates a
  memory or an administrator explicitly adopts an existing one.

Only a managed memory may be pinned by an agent spec (and so reach the agent's
execution-role grant and runtime binding), read back for an agent, or edited /
deleted through ``/api/memory/resources``. Everything else in the account is
listed as *detected, not managed* and is otherwise untouchable from the console.
"""

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.ledger import ManagedMemory
from app.services.agentcore.client import control_client
from app.services.memory import memory_id_or_none, spec_memory_id
from app.services.workspace import WorkspaceContext


def managed_ids(db: Session, workspace: WorkspaceContext) -> set[str]:
    """Every memory id this workspace manages (bootstrap memory included)."""
    ids = {
        row.memory_id
        for row in db.query(ManagedMemory).filter(ManagedMemory.workspace_id == workspace.id)
    }
    default_id = memory_id_or_none(workspace)
    if default_id:
        ids.add(default_id)
    return ids


def is_managed(db: Session, workspace: WorkspaceContext, memory_id: str | None) -> bool:
    if not memory_id:
        return False
    if memory_id == memory_id_or_none(workspace):
        return True
    return (
        db.query(ManagedMemory.id)
        .filter(
            ManagedMemory.workspace_id == workspace.id,
            ManagedMemory.memory_id == memory_id,
        )
        .first()
        is not None
    )


def require_managed(db: Session, workspace: WorkspaceContext, memory_id: str) -> None:
    """404 for an id this workspace does not manage — the same answer an id from
    another workspace gets, so the lifecycle API discloses nothing about it."""
    if not is_managed(db, workspace, memory_id):
        raise NotFoundError(
            "memory.not_managed",
            "this memory is not managed by the workspace",
            {"memory_id": memory_id},
        )


def register(
    db: Session,
    workspace: WorkspaceContext,
    memory_id: str,
    *,
    origin: str,
    created_by: str | None,
) -> None:
    """Record ``memory_id`` as managed; idempotent (a repeat or a racing writer of
    the same id leaves one row, guarded by the unique index)."""
    if not memory_id or is_managed(db, workspace, memory_id):
        return
    db.add(
        ManagedMemory(
            workspace_id=workspace.id,
            memory_id=memory_id,
            origin=origin,
            created_by=created_by,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def forget(db: Session, workspace: WorkspaceContext, memory_id: str) -> None:
    db.query(ManagedMemory).filter(
        ManagedMemory.workspace_id == workspace.id,
        ManagedMemory.memory_id == memory_id,
    ).delete()
    db.commit()


def require_pinnable(
    db: Session, workspace: WorkspaceContext, memory_id: str, control: Any = None
) -> None:
    """An agent spec may pin ``memory_id`` only when it is managed AND ``ACTIVE``.

    Ownership is checked against the ledger first, so a foreign id never reaches
    an AWS call; ``ACTIVE`` is read live (AWS is the source of truth for status).
    """
    if not is_managed(db, workspace, memory_id):
        raise AppError(
            "agent.memory_not_managed",
            f"memory '{memory_id}' is not managed by this workspace — pick one from "
            "the Memory resources list (an administrator can adopt an existing one)",
            {"memory_id": memory_id},
            status_code=422,
        )
    control = control or control_client(workspace)
    status = (control.get_memory(memoryId=memory_id).get("memory") or {}).get("status")
    if status != "ACTIVE":
        raise AppError(
            "agent.memory_not_active",
            f"memory '{memory_id}' is {status or 'unavailable'}, not ACTIVE",
            {"memory_id": memory_id, "status": status},
            status_code=409,
        )


def require_spec_memory(
    db: Session, workspace: WorkspaceContext, spec: dict[str, Any] | None
) -> None:
    """Validate the memory a spec (raw ledger JSON or ``model_dump()``) pins, if any.
    A spec that pins none lands on the bootstrap memory and needs no check."""
    memory_id = spec_memory_id(spec)
    if memory_id:
        require_pinnable(db, workspace, memory_id)


def readable_spec_memory_id(
    db: Session, workspace: WorkspaceContext, spec: dict[str, Any] | None
) -> str | None:
    """The pinned memory to read an agent's turns back from, or None for the
    default. A legacy spec that pins an unmanaged id raises instead of reading a
    memory the workspace does not own."""
    memory_id = spec_memory_id(spec)
    if memory_id and not is_managed(db, workspace, memory_id):
        raise AppError(
            "agent.memory_not_managed",
            f"the agent pins memory '{memory_id}', which this workspace does not manage",
            {"memory_id": memory_id},
            status_code=409,
        )
    return memory_id
