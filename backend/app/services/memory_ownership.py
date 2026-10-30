"""Which memory stores a workspace *manages* (issue #55).

A workspace is one AWS account × region, but the account boundary is not the
workspace boundary: the account can hold memories the platform never created —
another team's, another tool's. So an id is never trusted because a backend
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
from app.models.ledger import ManagedMemory, MemoryStore
from app.services import creators, visibility
from app.services.creators import Creator
from app.services.memory_backend import memory_id_or_none, spec_memory_id
from app.services.visibility import Viewer
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


def require_managed(
    db: Session, workspace: WorkspaceContext, memory_id: str, *, viewer: Viewer = Viewer.ALL
) -> None:
    """404 for an id this workspace does not manage — the same answer an id from
    another workspace gets, so the lifecycle API discloses nothing about it. A managed
    memory hidden from ``viewer`` by workspace isolation answers the same 404."""
    if not is_managed(db, workspace, memory_id) or memory_id in hidden_ids(db, workspace, viewer):
        raise NotFoundError(
            "memory.not_managed",
            "this memory is not managed by the workspace",
            {"memory_id": memory_id},
        )


def hidden_ids(db: Session, workspace: WorkspaceContext, viewer: Viewer) -> set[str]:
    """The managed memory ids workspace isolation hides from ``viewer`` (the bootstrap
    memory has no row and so no creator — never hidden). No query when unfiltered."""
    return visibility.hidden_ledger_ids(
        viewer, db, ManagedMemory.memory_id, ManagedMemory.created_by,
        ManagedMemory.workspace_id == workspace.id,
    )


def managed_rows(db: Session, workspace: WorkspaceContext) -> dict[str, ManagedMemory]:
    """The workspace's ``ManagedMemory`` rows by memory id (bootstrap memory excluded:
    it has no row, so it carries no creator)."""
    return {
        row.memory_id: row
        for row in db.query(ManagedMemory).filter(ManagedMemory.workspace_id == workspace.id)
    }


def register(
    db: Session,
    workspace: WorkspaceContext,
    memory_id: str,
    *,
    origin: str,
    creator: Creator | None = None,
) -> None:
    """Record ``memory_id`` as managed; idempotent (a repeat or a racing writer of
    the same id leaves one row, guarded by the unique index).

    A created memory records its full creator. An adopted one pre-existed, so it
    records no owner id — only the adopting administrator's name, for display
    (upstream #261). The self-hosted store row of a CREATED memory (N6
    ``memory_stores``) mirrors the same creator so the two ledgers agree.
    """
    if not memory_id or is_managed(db, workspace, memory_id):
        return
    row = ManagedMemory(workspace_id=workspace.id, memory_id=memory_id, origin=origin)
    if creator is not None and origin == "adopted":
        creator = Creator(user_id=None, name=creator.name)
    creators.stamp(row, creator)
    db.add(row)
    if creator is not None and origin == "created":
        store = (
            db.query(MemoryStore)
            .filter(MemoryStore.workspace_id == workspace.id, MemoryStore.memory_id == memory_id)
            .first()
        )
        if store is not None:
            creators.stamp(store, creator)
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


def require_pinnable(db: Session, workspace: WorkspaceContext, memory_id: str) -> None:
    """An agent spec may pin ``memory_id`` only when the workspace manages it.

    Ledger-only: ownership is the governance decision; whether the store is live is
    the memory backend's business at write time (N6), not a deploy-time probe.
    """
    if not is_managed(db, workspace, memory_id):
        raise AppError(
            "agent.memory_not_managed",
            f"memory '{memory_id}' is not managed by this workspace — pick one from "
            "the Memory resources list (an administrator can adopt an existing one)",
            {"memory_id": memory_id},
            status_code=422,
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
