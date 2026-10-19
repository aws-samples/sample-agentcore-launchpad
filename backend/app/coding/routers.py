"""Coding agent console routes (``/api/coding``) — the runtime half.

``GET /runtime`` is a member read (the wizard card and the project list need to
know whether the coding agent is usable). Install / repair and uninstall create or
delete AWS resources on an administrator's explicit request: ``ADMIN`` in
``core/route_policy`` and ``FREE`` in ``core/billing_policy`` (deploy and build
costs are not usage). Both answer ``202 {job_id, status}``; the console re-reads
``GET /runtime`` for progress. The project / turn / file routes join this router
with the backend child.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace

from . import install, status

router = APIRouter(prefix="/api/coding", tags=["coding"])


@router.get("/runtime")
def coding_runtime(
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    return status.runtime_status(db, ws.id, ws.context, is_admin=identity.is_admin)


def _queue(request: Request, ws: WorkspaceScope, db: Session, job_type: str) -> dict[str, Any]:
    identity = require_identity(request)
    job, started = install.request_job(
        db, ws.id, job_type, requested_by=identity.user_id, requested_by_name=identity.username
    )
    if started:
        install.start_async(job.id)
    status.invalidate(ws.id)
    return {"job_id": job.id, "status": job.status}


@router.post("/runtime/install", status_code=202)
def install_coding_runtime(
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Install, or repair an installed runtime (idempotent: role reconcile,
    content-addressed image, drift-checked runtime update)."""
    return _queue(request, ws, db, install.INSTALL_JOB_TYPE)


@router.delete("/runtime", status_code=202)
def uninstall_coding_runtime(
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete the runtime and its role. Refused while any project runs a turn;
    project snapshots are kept, so a re-install brings every project back."""
    running = install.running_projects(db, ws.id)
    if running:
        raise AppError(
            "coding.runtime_in_use",
            f"{running} coding project(s) are running a turn — wait for them or interrupt "
            "them before uninstalling",
            {"running": running},
            status_code=409,
        )
    return _queue(request, ws, db, install.UNINSTALL_JOB_TYPE)
