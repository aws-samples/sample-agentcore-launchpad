"""Who created a user-managed resource — one shape for every kind.

Every in-scope resource records its creating console principal the way API keys do
(#259): `created_by` is the principal's `users.id` — the ownership key — and
`created_by_name` its username at creation, display only. A principal without a
`users` row (the config admin, the auth-disabled local operator) records
`(None, name)`; a path with no human principal (samples, bootstrap, experiment-owned
configs) records nothing. Legacy rows and resources created outside the console are
never backfilled — both fields read NULL.

Ledger-backed kinds carry the two columns on their own row (`stamp` / `fields`).
AWS-only kinds — knowledge bases and custom evaluators — keep theirs in the
workspace-scoped `resource_creators` table (`record` / `lookup` / `forget`).

The creator is always derived on the server; no request model carries it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ledger import ResourceCreator

if TYPE_CHECKING:
    from fastapi import Request

    from app.routers.auth import Identity

# `resource_creators.kind` values — the AWS-only kinds.
KB = "knowledge_base"
EVALUATOR = "evaluator"


@dataclass(frozen=True)
class Creator:
    user_id: str | None  # users.id — the ownership key
    name: str | None  # username at creation — display only

    @classmethod
    def of(cls, identity: Identity | None) -> Creator | None:
        if identity is None:
            return None
        return cls(user_id=identity.user_id, name=identity.username)

    @classmethod
    def of_request(cls, request: Request) -> Creator:
        """The caller's creator. Auth disabled → the local operator, `(None, name)`."""
        from app.routers.auth import require_identity

        identity = require_identity(request)
        return cls(user_id=identity.user_id, name=identity.username)


def empty() -> dict[str, str | None]:
    """Both fields null: no creator is known."""
    return {"created_by": None, "created_by_name": None}


def stamp(row: Any, creator: Creator | None) -> None:
    """Set the creator columns on a new row; `None` (no human principal) leaves NULLs."""
    if creator is None:
        return
    row.created_by = creator.user_id
    row.created_by_name = creator.name


def fields(row: Any | None) -> dict[str, str | None]:
    """The response fields for a row — or a `Creator` — (nulls when there is none)."""
    if row is None:
        return empty()
    if isinstance(row, Creator):
        return {"created_by": row.user_id, "created_by_name": row.name}
    return {
        "created_by": getattr(row, "created_by", None),
        "created_by_name": getattr(row, "created_by_name", None),
    }


# --- AWS-only kinds ------------------------------------------------------------


def record(
    db: Session,
    workspace_id: str,
    kind: str,
    resource_id: str,
    creator: Creator | None,
) -> None:
    """Remember who created an AWS-only resource. Idempotent: a row that already
    exists (a retried create of the same id) is left as is. Commits."""
    if creator is None or not resource_id:
        return
    existing = db.scalar(
        select(ResourceCreator).where(
            ResourceCreator.workspace_id == workspace_id,
            ResourceCreator.kind == kind,
            ResourceCreator.resource_id == resource_id,
        )
    )
    if existing is not None:
        return
    db.add(
        ResourceCreator(
            workspace_id=workspace_id,
            kind=kind,
            resource_id=resource_id,
            created_by=creator.user_id,
            created_by_name=creator.name,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # a concurrent record of the same id won the unique index — same outcome
        db.rollback()


def lookup(
    db: Session, workspace_id: str, kind: str, resource_ids: Iterable[str]
) -> dict[str, dict[str, str | None]]:
    """`resource_id → fields` for the ids that have a creator row (one query)."""
    ids = [rid for rid in set(resource_ids) if rid]
    if not ids:
        return {}
    rows = db.scalars(
        select(ResourceCreator).where(
            ResourceCreator.workspace_id == workspace_id,
            ResourceCreator.kind == kind,
            ResourceCreator.resource_id.in_(ids),
        )
    )
    return {row.resource_id: fields(row) for row in rows}


def lookup_one(
    db: Session, workspace_id: str, kind: str, resource_id: str
) -> dict[str, str | None]:
    return lookup(db, workspace_id, kind, [resource_id]).get(resource_id) or empty()


def forget(db: Session, workspace_id: str, kind: str, resource_id: str) -> None:
    """Drop the creator row of a resource the console just deleted. Commits."""
    rows = db.scalars(
        select(ResourceCreator).where(
            ResourceCreator.workspace_id == workspace_id,
            ResourceCreator.kind == kind,
            ResourceCreator.resource_id == resource_id,
        )
    ).all()
    if not rows:
        return
    for row in rows:
        db.delete(row)
    db.commit()
