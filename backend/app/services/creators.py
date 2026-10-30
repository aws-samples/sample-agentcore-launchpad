"""Who created a user-managed resource — one shape for every kind (upstream #261).

Every in-scope resource records its creating console principal the way API keys do
(#259): `created_by` is the principal's `users.id` — the ownership key — and
`created_by_name` its username at creation, display only. A principal without a
`users` row (the config admin, the auth-disabled local operator) records
`(None, name)`; a path with no human principal (presets, bootstrap, discovered runtimes,
experiment-owned configs) records nothing. Legacy rows and resources created outside the
console are never backfilled — both fields read NULL.

Ledger-backed kinds carry the two columns on their own row (`stamp` / `fields`).
AWS-only kinds keep theirs in the workspace-scoped `resource_creators` table
(`record` / `lookup` / `forget`). In the CN build every kind upstream kept there
(knowledge bases, custom evaluators) has a ledger row of its own (`knowledge_bases`,
`eval_evaluators`), so no kind is registered on the side table today; it is kept with
upstream's exact API so a future AWS-only kind — and step 2's visibility clause — plug in
unchanged.

The creator is always derived on the server; no request model carries it.
`reject_client_creator` is the one guard a router calls on a payload that might try.

Contract: docs/cn/interfaces/users-and-ownership.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import ResourceCreator

if TYPE_CHECKING:
    from fastapi import Request

    from app.routers.auth import Identity

# `resource_creators.kind` values — the AWS-only kinds. Both are ledger-backed in the CN
# build (see the module docstring); the constants stay for the side-table API.
KB = "knowledge_base"
EVALUATOR = "evaluator"

# The response / column names — the only two the client ever sees, and the two a
# client must never send.
FIELD_ID = "created_by"
FIELD_NAME = "created_by_name"
CLIENT_FORBIDDEN_FIELDS = (FIELD_ID, FIELD_NAME)


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
        """The caller's creator. Config admin / auth disabled → `(None, name)`."""
        from app.routers.auth import require_identity

        identity = require_identity(request)
        return cls(user_id=identity.user_id, name=identity.username)

    def owns(self, row: Any) -> bool:
        """Whether `row` (any stamped row) was created by this principal: a `users.id`
        match. A principal without a row (`user_id` None) owns nothing by id — the
        config admin is an administrator and never needs this."""
        return self.user_id is not None and getattr(row, FIELD_ID, None) == self.user_id


def empty() -> dict[str, str | None]:
    """Both fields null: no creator is known."""
    return {FIELD_ID: None, FIELD_NAME: None}


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
        return {FIELD_ID: row.user_id, FIELD_NAME: row.name}
    return {
        FIELD_ID: getattr(row, FIELD_ID, None),
        FIELD_NAME: getattr(row, FIELD_NAME, None),
    }


def reject_client_creator(payload: Mapping[str, Any] | None) -> None:
    """422 `validation.creator_not_settable` when a client payload carries a creator
    field. Every request model in scope either forbids extra fields or is a dict the
    router hands here first — the creator is server-derived, never accepted."""
    if not payload:
        return
    sent = [name for name in CLIENT_FORBIDDEN_FIELDS if name in payload]
    if sent:
        raise AppError(
            "validation.creator_not_settable",
            "the creator is recorded by the server and cannot be set by the client",
            {"fields": sent},
            status_code=422,
        )


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
