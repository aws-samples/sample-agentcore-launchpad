"""Coding agent console routes (``/api/coding``).

**Runtime.** ``GET /runtime`` is a member read (the wizard card and the project
list need to know whether the coding agent is usable). Install / repair and
uninstall create or delete AWS resources on an administrator's explicit request:
``ADMIN`` in ``core/route_policy`` and ``FREE`` in ``core/billing_policy`` (deploy
and build costs are not usage). Both answer ``202 {job_id, status}``; the console
re-reads ``GET /runtime`` for progress.

**Projects** (parent design §7.3). Every project route is ``MEMBER`` and resolves
the project through the private-owner choke point (``projects.project_in``): a
foreign or missing id is ``404 coding.project_not_found``. Administrators reach
other members' projects only through ``GET /projects?all=1`` and ``DELETE``.
Starting a turn is the one billable route (``Billable(SOURCE_CODING)`` — the gate
checks the payer's spend, the handler the model's price); deploying is
``perm:agents.deploy``. Handlers stay thin: the logic lives in ``projects``,
``turns``, ``files``, ``stream`` and ``deploy``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import ByocPythonVersion
from app.services import budgets
from app.services.visibility import Viewer, require_viewer

from . import deploy, files, install, projects, runtime_client, status, stream, turns
from .models import DESCRIPTION_MAX, NAME_MAX

router = APIRouter(prefix="/api/coding", tags=["coding"])


# ── runtime ─────────────────────────────────────────────────────────────────


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


# ── projects ────────────────────────────────────────────────────────────────


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NotBlank = AfterValidator(_not_blank)


class ProjectCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX), NotBlank]
    description: str = Field(default="", max_length=DESCRIPTION_MAX)


class ProjectPatch(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX), NotBlank] | None = None
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)


class TurnStart(BaseModel):
    # the 32 KB cap is the handler's (413 coding.prompt_too_long, not a 422)
    prompt: Annotated[str, Field(min_length=1), NotBlank]
    model: str | None = Field(default=None, max_length=160)
    effort: str | None = Field(default=None, max_length=16)


class FileWrite(BaseModel):
    path: str = Field(min_length=1, max_length=files.MAX_PATH_CHARS)
    content: str
    # omitted = overwrite unconditionally; a hash = the file must still have it;
    # null = the file must not exist yet
    expected_sha256: str | None = Field(default=None, max_length=128)


class DeployRequest(BaseModel):
    agent_name: str | None = Field(default=None, pattern=deploy.AGENT_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=20000)
    artifact_kind: Literal["code_zip", "container_source"] | None = None
    entrypoint: str | None = Field(default=None, min_length=1, max_length=255)
    python_version: ByocPythonVersion | None = None


def _owned(request: Request, ws: WorkspaceScope, db: Session, project_id: str):
    identity = require_identity(request)
    return identity, projects.require_project(db, ws.id, project_id, identity)


@router.get("/projects")
def list_coding_projects(
    request: Request,
    all_projects: bool = Query(False, alias="all"),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The caller's projects; ``?all=1`` (administrators) every project here."""
    identity = require_identity(request)
    return {"projects": projects.list_projects(db, ws.id, identity,
                                               all_projects=all_projects)}


@router.post("/projects", status_code=201)
def create_coding_project(
    body: ProjectCreate,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = projects.create_project(db, ws.id, identity, name=body.name,
                                  description=body.description)
    return projects.project_out(row, identity)


@router.get("/projects/{project_id}")
def get_coding_project(
    project_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity, row = _owned(request, ws, db, project_id)
    return projects.detail_out(db, row, identity)


@router.patch("/projects/{project_id}")
def update_coding_project(
    project_id: str,
    body: ProjectPatch,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity, row = _owned(request, ws, db, project_id)
    row = projects.update_project(db, row, name=body.name, description=body.description)
    return projects.project_out(row, identity)


@router.delete("/projects/{project_id}")
def delete_coding_project(
    project_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The owner, or an administrator cleaning up. Refused while a turn runs."""
    identity = require_identity(request)
    row = projects.require_project(db, ws.id, project_id, identity, admin_ok=True)
    projects.delete_project(db, ws.context, row)
    return {"deleted": True}


# ── turns ───────────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/turns", status_code=202)
def start_coding_turn(
    project_id: str,
    body: TurnStart,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start a turn; it runs in the project's microVM, the call returns at once."""
    _identity, row = _owned(request, ws, db, project_id)
    turn = turns.start_turn(db, ws.context, row, prompt=body.prompt, model=body.model,
                            effort=body.effort, payer=budgets.payer_of_request(request))
    return projects.turn_out(turn)


@router.post("/projects/{project_id}/interrupt")
def interrupt_coding_turn(
    project_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, project_id)
    return turns.interrupt(ws.context, row)


@router.get("/projects/{project_id}/events")
def coding_events(
    project_id: str,
    request: Request,
    after: int | None = Query(None, ge=0),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """SSE: the project's event log after ``after`` (or ``Last-Event-ID``), then live."""
    _identity, row = _owned(request, ws, db, project_id)
    runtime_client.require_runtime_arn(ws.context)
    cursor = after or 0
    last_id = request.headers.get("last-event-id", "")
    if last_id.isdigit():
        cursor = max(cursor, int(last_id))
    project_id = row.id
    # the stream reads the ledger through its own short sessions; this request's
    # session would otherwise stay open for the whole stream (up to 25 min)
    db.close()
    return StreamingResponse(
        stream.body(ws.context, project_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── files ───────────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/tree")
def coding_tree(
    project_id: str,
    request: Request,
    path: str = Query("."),
    depth: int = Query(1, ge=1, le=files.MAX_TREE_DEPTH),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, project_id)
    return files.tree(ws.context, row, path, depth)


@router.get("/projects/{project_id}/file")
def coding_read_file(
    project_id: str,
    request: Request,
    path: str = Query(...),
    rev: Literal["head", "turn"] | None = Query(None),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, project_id)
    return files.read_file(ws.context, row, path, rev)


@router.put("/projects/{project_id}/file")
def coding_write_file(
    project_id: str,
    body: FileWrite,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, project_id)
    expected = ({"expected_sha256": body.expected_sha256}
                if "expected_sha256" in body.model_fields_set else {})
    return files.write_file(db, ws.context, row, path=body.path, content=body.content,
                            expected=expected)


@router.get("/projects/{project_id}/changes")
def coding_changes(
    project_id: str,
    request: Request,
    scope: Literal["worktree", "turn"] = Query("worktree"),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, project_id)
    return files.changes(ws.context, row, scope)


@router.get("/projects/{project_id}/diff")
def coding_diff(
    project_id: str,
    request: Request,
    scope: Literal["worktree", "turn"] = Query("worktree"),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, project_id)
    return files.diff(ws.context, row, scope)


# ── deploy ──────────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/deploy", status_code=202)
def deploy_coding_project(
    project_id: str,
    body: DeployRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Hand the project to the byoc pipeline: create its Agent, or re-publish it."""
    identity, row = _owned(request, ws, db, project_id)
    return deploy.deploy_project(
        db, ws, row, identity, viewer=viewer, agent_name=body.agent_name,
        description=body.description, artifact_kind=body.artifact_kind,
        entrypoint=body.entrypoint, python_version=body.python_version,
    )
