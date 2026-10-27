"""Coding agent console routes (``/api/coding``).

**Runtime.** ``GET /runtime`` is a member read (the wizard card and the repo
list need to know whether the coding agent is usable). Install / repair and
uninstall create or delete AWS resources on an administrator's explicit request
(``ADMIN`` in ``core/route_policy``). Both answer ``202 {job_id, status}``; the
console re-reads ``GET /runtime`` for progress.

**Repos** (parent design §7.3). Every repo route is ``MEMBER`` and resolves
the repo through the private-owner choke point (``repos.repo_in``): a
foreign or missing id is ``404 coding.repo_not_found``. Administrators reach
other members' repos only through ``GET /repos?all=1`` and ``DELETE``.
Deploying is ``perm:agents.deploy``. Handlers stay thin: the logic lives in
``repos``, ``turns``, ``files``, ``downloads``, ``stream`` and ``deploy``.
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
from app.services.visibility import Viewer, require_viewer

from . import deploy, downloads, files, install, repos, runtime_client, status, stream, turns
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
    """Delete the runtime and its role. Refused while any repo runs a turn;
    repo snapshots are kept, so a re-install brings every repo back."""
    running = install.running_repos(db, ws.id)
    if running:
        raise AppError(
            "coding.runtime_in_use",
            f"{running} coding repo(s) are running a turn — wait for them or interrupt "
            "them before uninstalling",
            {"running": running},
            status_code=409,
        )
    return _queue(request, ws, db, install.UNINSTALL_JOB_TYPE)


# ── repos ────────────────────────────────────────────────────────────────


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NotBlank = AfterValidator(_not_blank)


class RepoCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX), NotBlank]
    description: str = Field(default="", max_length=DESCRIPTION_MAX)


class RepoPatch(BaseModel):
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


class DownloadRequest(BaseModel):
    # set = that one file; omitted (or null) = the whole repo as a zip
    path: str | None = Field(default=None, max_length=files.MAX_PATH_CHARS)


class DeployRequest(BaseModel):
    agent_name: str | None = Field(default=None, pattern=deploy.AGENT_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=20000)
    artifact_kind: Literal["code_zip", "container_source"] | None = None
    entrypoint: str | None = Field(default=None, min_length=1, max_length=255)
    python_version: ByocPythonVersion | None = None
    # the business project a NEW agent is filed under (services/agent_project); must
    # match X-Project when both are sent, ignored when the repo re-publishes its agent
    project_id: str | None = Field(default=None, max_length=32)


def _owned(request: Request, ws: WorkspaceScope, db: Session, repo_id: str):
    identity = require_identity(request)
    return identity, repos.require_repo(db, ws.id, repo_id, identity)


@router.get("/repos")
def list_coding_repos(
    request: Request,
    all_repos: bool = Query(False, alias="all"),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The caller's repos; ``?all=1`` (administrators) every repo here."""
    identity = require_identity(request)
    return {"repos": repos.list_repos(db, ws.id, identity,
                                               all_repos=all_repos)}


@router.post("/repos", status_code=201)
def create_coding_repo(
    body: RepoCreate,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity = require_identity(request)
    row = repos.create_repo(db, ws.id, identity, name=body.name,
                                  description=body.description)
    return repos.repo_out(row, identity)


@router.get("/repos/{repo_id}")
def get_coding_repo(
    repo_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity, row = _owned(request, ws, db, repo_id)
    return repos.detail_out(db, row, identity)


@router.patch("/repos/{repo_id}")
def update_coding_repo(
    repo_id: str,
    body: RepoPatch,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    identity, row = _owned(request, ws, db, repo_id)
    row = repos.update_repo(db, row, name=body.name, description=body.description)
    return repos.repo_out(row, identity)


@router.delete("/repos/{repo_id}")
def delete_coding_repo(
    repo_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The owner, or an administrator cleaning up. Refused while a turn runs."""
    identity = require_identity(request)
    row = repos.require_repo(db, ws.id, repo_id, identity, admin_ok=True)
    repos.delete_repo(db, ws.context, row)
    return {"deleted": True}


# ── turns ───────────────────────────────────────────────────────────────────


@router.post("/repos/{repo_id}/turns", status_code=202)
def start_coding_turn(
    repo_id: str,
    body: TurnStart,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start a turn; it runs in the repo's microVM, the call returns at once."""
    _identity, row = _owned(request, ws, db, repo_id)
    turn = turns.start_turn(db, ws.context, row, prompt=body.prompt, model=body.model,
                            effort=body.effort)
    return repos.turn_out(turn)


@router.post("/repos/{repo_id}/interrupt")
def interrupt_coding_turn(
    repo_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, repo_id)
    return turns.interrupt(ws.context, row)


@router.get("/repos/{repo_id}/events")
def coding_events(
    repo_id: str,
    request: Request,
    after: int | None = Query(None, ge=0),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """SSE: the repo's event log after ``after`` (or ``Last-Event-ID``), then live."""
    _identity, row = _owned(request, ws, db, repo_id)
    runtime_client.require_runtime_arn(ws.context)
    cursor = after or 0
    last_id = request.headers.get("last-event-id", "")
    if last_id.isdigit():
        cursor = max(cursor, int(last_id))
    repo_id = row.id
    # the stream reads the ledger through its own short sessions; this request's
    # session would otherwise stay open for the whole stream (up to 25 min)
    db.close()
    return StreamingResponse(
        stream.body(ws.context, repo_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── files ───────────────────────────────────────────────────────────────────


@router.get("/repos/{repo_id}/tree")
def coding_tree(
    repo_id: str,
    request: Request,
    path: str = Query("."),
    depth: int = Query(1, ge=1, le=files.MAX_TREE_DEPTH),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, repo_id)
    return files.tree(ws.context, row, path, depth)


@router.get("/repos/{repo_id}/file")
def coding_read_file(
    repo_id: str,
    request: Request,
    path: str = Query(...),
    rev: Literal["head", "turn"] | None = Query(None),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, repo_id)
    return files.read_file(ws.context, row, path, rev)


@router.put("/repos/{repo_id}/file")
def coding_write_file(
    repo_id: str,
    body: FileWrite,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, repo_id)
    expected = ({"expected_sha256": body.expected_sha256}
                if "expected_sha256" in body.model_fields_set else {})
    return files.write_file(db, ws.context, row, path=body.path, content=body.content,
                            expected=expected)


@router.get("/repos/{repo_id}/changes")
def coding_changes(
    repo_id: str,
    request: Request,
    scope: Literal["worktree", "turn"] = Query("worktree"),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, repo_id)
    return files.changes(ws.context, row, scope)


@router.get("/repos/{repo_id}/diff")
def coding_diff(
    repo_id: str,
    request: Request,
    scope: Literal["worktree", "turn"] = Query("worktree"),
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _identity, row = _owned(request, ws, db, repo_id)
    return files.diff(ws.context, row, scope)


@router.post("/repos/{repo_id}/download")
def download_coding_files(
    repo_id: str,
    body: DownloadRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A short-lived download URL for one file (``path``) or the repo zip.
    Owner-only like every file route; allowed while a turn runs (read-only)."""
    _identity, row = _owned(request, ws, db, repo_id)
    return downloads.download(ws.context, ws.id, row, body.path)


# ── deploy ──────────────────────────────────────────────────────────────────


@router.post("/repos/{repo_id}/deploy", status_code=202)
def deploy_coding_repo(
    repo_id: str,
    body: DeployRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Hand the repo to the byoc pipeline: create its Agent, or re-publish it."""
    identity, row = _owned(request, ws, db, repo_id)
    return deploy.deploy_repo(
        db, ws, row, identity, request=request, viewer=viewer, agent_name=body.agent_name,
        description=body.description, artifact_kind=body.artifact_kind,
        entrypoint=body.entrypoint, python_version=body.python_version,
        project_id=body.project_id,
    )
