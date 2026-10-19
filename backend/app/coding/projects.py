"""Coding projects: the private-owner choke point, CRUD and the response shapes.

**Access rule** (parent design §7.2, deliberately stricter than
``resource_visibility``: a project is a live shell plus code). A project belongs
to the principal that created it (``assistant/principal.principal_of``) in every
workspace visibility mode. Every by-id route resolves through ``project_in``; a
foreign or missing id is ``404 coding.project_not_found`` — indistinguishable.
Administrators pass only where the route asks for it (``admin_ok=True``): the
workspace-wide list (``?all=1``) and DELETE, for cleanup. They can never open a
project's IDE, read its files, stream its events or run a turn in it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.assistant.principal import principal_of
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.routers.auth import Identity
from app.services import creators
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import data_client
from app.services.creators import Creator
from app.services.workspace import WorkspaceContext

from . import runtime_client, s3urls, snapshots
from .models import (
    DESCRIPTION_MAX,
    NAME_MAX,
    PROJECT_DELETING,
    PROJECT_IDLE,
    PROJECT_RUNNING,
    CodingProject,
    CodingTurn,
)

logger = logging.getLogger("launchpad.coding")

TURNS_IN_DETAIL = 20


def now() -> datetime:
    return datetime.now(UTC)


def not_found() -> NotFoundError:
    return NotFoundError("coding.project_not_found", "coding project not found")


def turn_running() -> AppError:
    return AppError("coding.turn_running", "a turn is running in this project",
                    status_code=409)


# ── the choke point ─────────────────────────────────────────────────────────


def owns(row: CodingProject | None, identity: Identity) -> bool:
    """The caller's principal is the project's owner. A NULL owner matches nobody."""
    return (
        row is not None
        and row.owner_principal is not None
        and row.owner_principal == principal_of(identity)
    )


def project_in(
    db: Session,
    workspace_id: str,
    project_id: str,
    identity: Identity,
    *,
    admin_ok: bool = False,
) -> CodingProject | None:
    """The project when it lives in this workspace and the caller owns it — or,
    with ``admin_ok``, when the caller is an administrator. Else None."""
    if not project_id:
        return None
    row = db.get(CodingProject, project_id)
    if row is None or row.workspace_id != workspace_id:
        return None
    if owns(row, identity):
        return row
    if admin_ok and identity.is_admin:
        return row
    return None


def require_project(
    db: Session, workspace_id: str, project_id: str, identity: Identity, *,
    admin_ok: bool = False,
) -> CodingProject:
    row = project_in(db, workspace_id, project_id, identity, admin_ok=admin_ok)
    if row is None:
        raise not_found()
    return row


# ── response shapes (parent design §7.3) ────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _live_agents(db: Session, workspace_id: str, ids: set[str]) -> set[str]:
    """Which deployed-Agent ids still exist (a deleted Agent is not redeployable:
    the next deploy creates a new one)."""
    if not ids:
        return set()
    rows = (
        db.query(Agent.id)
        .filter(Agent.id.in_(ids), Agent.workspace_id == workspace_id,
                Agent.status != "deleted")
        .all()
    )
    return {row.id for row in rows}


def project_out(row: CodingProject, identity: Identity, *,
                live_agents: set[str] | None = None) -> dict[str, Any]:
    agent_id = row.agent_id
    if agent_id and live_agents is not None and agent_id not in live_agents:
        agent_id = None
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        # `deleting` is a transient claim of an in-flight DELETE: still idle to a reader
        "status": PROJECT_RUNNING if row.status == PROJECT_RUNNING else PROJECT_IDLE,
        "active_turn_id": row.active_turn_id,
        "model": row.model,
        "effort": row.effort,
        "agent_id": agent_id,
        "snapshot_at": _iso(row.snapshot_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        **creators.fields(row),
        "owned": owns(row, identity),
    }


def turn_out(row: CodingTurn) -> dict[str, Any]:
    return {
        "id": row.id,
        "turn_id": row.turn_id,
        "thread_id": row.thread_id,
        "prompt": row.prompt or "",
        "model": row.model,
        "effort": row.effort,
        "status": row.status,
        "error": row.error,
        "usage": row.usage,
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
    }


def detail_out(db: Session, row: CodingProject, identity: Identity) -> dict[str, Any]:
    """The project + its newest ``TURNS_IN_DETAIL`` turns (oldest first)."""
    turns = (
        db.query(CodingTurn)
        .filter(CodingTurn.project_id == row.id, CodingTurn.workspace_id == row.workspace_id)
        .order_by(CodingTurn.started_at.desc(), CodingTurn.id.desc())
        .limit(TURNS_IN_DETAIL)
        .all()
    )
    live = _live_agents(db, row.workspace_id or "", {row.agent_id} if row.agent_id else set())
    out = project_out(row, identity, live_agents=live)
    out["turns"] = [turn_out(t) for t in reversed(turns)]
    out["active_turn"] = row.active_turn_id if row.status == PROJECT_RUNNING else None
    return out


# ── CRUD ────────────────────────────────────────────────────────────────────


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise AppError("validation.invalid_request", "a project name is required",
                       status_code=422)
    return cleaned[:NAME_MAX]


def list_projects(db: Session, workspace_id: str, identity: Identity, *,
                  all_projects: bool = False) -> list[dict[str, Any]]:
    """The caller's own projects; ``all_projects`` (administrators) every project
    of the workspace — for cleanup only (``owned`` tells the console which)."""
    query = db.query(CodingProject).filter(CodingProject.workspace_id == workspace_id)
    if not (all_projects and identity.is_admin):
        query = query.filter(CodingProject.owner_principal == principal_of(identity))
    rows = query.order_by(CodingProject.updated_at.desc(), CodingProject.id.desc()).all()
    live = _live_agents(db, workspace_id, {r.agent_id for r in rows if r.agent_id})
    return [project_out(r, identity, live_agents=live) for r in rows]


def create_project(db: Session, workspace_id: str, identity: Identity, *, name: str,
                   description: str = "") -> CodingProject:
    """A ledger row only — no runtime call: the session starts at the first action."""
    stamp = now()
    row = CodingProject(
        workspace_id=workspace_id,
        owner_principal=principal_of(identity),
        name=_clean_name(name),
        description=(description or "").strip()[:DESCRIPTION_MAX],
        session_id=runtime_client.new_session_id(),
        created_at=stamp,
        updated_at=stamp,
    )
    creators.stamp(row, Creator.of(identity))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_project(db: Session, row: CodingProject, *, name: str | None = None,
                   description: str | None = None) -> CodingProject:
    if name is not None:
        row.name = _clean_name(name)
    if description is not None:
        row.description = description.strip()[:DESCRIPTION_MAX]
    row.updated_at = now()
    db.commit()
    db.refresh(row)
    return row


def delete_project(db: Session, ctx: WorkspaceContext, row: CodingProject) -> None:
    """Stop the project's runtime session (best effort), delete its snapshots and
    exports, then its rows. Refused while a turn runs. An S3 failure fails the
    request (the user's code must not outlive the project silently); the rows stay
    so the delete can be retried."""
    project_id, workspace_id = row.id, row.workspace_id or ""
    with snapshots.project_lock(project_id):
        # the claim (idle → deleting) shuts out a turn start for the whole delete
        claimed = db.execute(
            update(CodingProject)
            .where(CodingProject.id == project_id, CodingProject.status == PROJECT_IDLE)
            .values(status=PROJECT_DELETING, updated_at=now())
            .execution_options(synchronize_session=False)
        ).rowcount
        db.commit()
        if claimed != 1:
            raise turn_running()
        arn = runtime_client.installed_runtime_arn(ctx)
        if arn:
            try:
                rt.stop_runtime_session(data_client(ctx, read_timeout=60), runtime_arn=arn,
                                        session_id=row.session_id)
            except Exception as exc:  # noqa: BLE001 — an ended session is the goal anyway
                logger.info("coding project %s: StopRuntimeSession: %s", project_id, exc)
        try:
            s3urls.delete_project_prefix(ctx, workspace_id, project_id)
        except Exception as exc:  # noqa: BLE001 — surfaced, the delete is retryable
            logger.warning("coding project %s: snapshot delete failed: %s", project_id, exc)
            db.execute(
                update(CodingProject)
                .where(CodingProject.id == project_id,
                       CodingProject.status == PROJECT_DELETING)
                .values(status=PROJECT_IDLE)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            raise AppError(
                "coding.delete_failed",
                f"the project's snapshots could not be deleted ({exc}); try again",
                status_code=502,
            ) from exc
        db.query(CodingTurn).filter(CodingTurn.project_id == project_id).delete(
            synchronize_session=False
        )
        db.query(CodingProject).filter(CodingProject.id == project_id).delete(
            synchronize_session=False
        )
        db.commit()
