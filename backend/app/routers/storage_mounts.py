"""The storage catalog (design storage-mounts §4, §5, §9): S3 Files / EFS access points
an administrator registers per workspace, that a project's agents may mount.

Reads are member routes inside the request's project — a member sees the project's
allowed set only, an administrator the whole workspace (the "configure → storage"
page, with the shared view). Registering, checking, editing, archiving and the
per-project allowed set are administrator routes, project-exempt: an entry is workspace
infrastructure, and `PUT /api/storage-mounts/projects/{project_id}` names its project in
the path. The preflight (`POST …/preflight`, §5) is the wizard's: it runs a spec's
catalog mounts against its network inside the request's project, and the agent route
(`GET …/agents/{agent_id}`) is the detail page's storage card.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.storage import STORAGE_ID_PATTERN
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import NETWORK_PROFILE_ID_RE, StorageMountRef
from app.services import access, agent_access
from app.services import storage_mounts as service
from app.services import storage_provision as provision
from app.services.audit import record_audit_event
from app.services.visibility import Viewer, require_viewer

router = APIRouter(prefix="/api/storage-mounts", tags=["storage-mounts"])

Mode = Literal["ro", "rw"]
DataClass = Literal["reference", "shared_state", "scratch"]


class RegisterBody(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field(pattern=STORAGE_ID_PATTERN)
    name: str = Field(default="", max_length=64)
    access_point_arn: str = Field(min_length=20, max_length=2048)
    max_mode: Mode = "ro"
    data_class: DataClass = "reference"
    network_profile_id: str | None = Field(default=None, pattern=NETWORK_PROFILE_ID_RE)
    # run the check right after registering (the default)
    check: bool = True


class PatchBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=64)
    max_mode: Mode | None = None
    data_class: DataClass | None = None
    # "" clears the default profile
    network_profile_id: str | None = Field(default=None, max_length=32)


class ProjectStorageBody(BaseModel):
    model_config = {"extra": "forbid"}

    storage_ids: list[str] = Field(default_factory=list, max_length=50)


class ProvisionBody(BaseModel):
    """`POST …/provision` (storage-mounts §7): what the console builds — a new S3 file
    system on a new bucket, reachable from one network profile — and the catalog fields
    the result is registered with."""

    model_config = {"extra": "forbid"}

    id: str = Field(pattern=STORAGE_ID_PATTERN)
    name: str = Field(default="", max_length=64)
    network_profile_id: str = Field(pattern=NETWORK_PROFILE_ID_RE)
    max_mode: Mode = "ro"
    data_class: DataClass = "reference"
    # the access point's POSIX identity and root directory. The directory is created on
    # first mount, owned by that identity (mode 755) — never "/": a new file system's
    # root belongs to root, so an access point rooted there could not write as 1000
    # (measured live 2026-11-04: EACCES on every write)
    posix_uid: int = Field(default=1000, ge=0, le=4294967295)
    posix_gid: int = Field(default=1000, ge=0, le=4294967295)
    root_directory: str = Field(default="/data", max_length=100,
                                pattern=r"^(/[A-Za-z0-9._-]+){1,4}$")
    # projects allowed to mount it once registered
    project_ids: list[str] = Field(default_factory=list, max_length=50)


class TeardownBody(BaseModel):
    model_config = {"extra": "forbid"}

    # repeats the storage id: the console's second confirmation (§7)
    confirm: str = Field(min_length=1, max_length=32)


class PreflightBody(BaseModel):
    """What the wizard knows when a mount is picked: the mounts, the network, the
    method (and a BYOC artifact kind, for the architecture note)."""

    model_config = {"extra": "forbid"}

    mounts: list[StorageMountRef] = Field(min_length=1, max_length=4)
    network_profile_id: str | None = Field(default=None, pattern=NETWORK_PROFILE_ID_RE)
    method: str = Field(default="", max_length=24)
    byoc_artifact_kind: str | None = Field(default=None, max_length=32)


def _actor(request: Request) -> str:
    return require_identity(request).username if auth_enabled() else "river"


def _see_all(request: Request) -> bool:
    # administrators and stewards (§11.4): the catalogue is structure, not project data
    return access.can_manage_structure(require_identity(request))


def _project_id(request: Request) -> str | None:
    scope = getattr(request.state, "project", None)
    return scope.id if scope is not None else None


def _audit(ws: WorkspaceScope, request: Request, action: str, target: str) -> None:
    # workspace infrastructure: a workspace-level event, not one project's
    record_audit_event(workspace_id=ws.id, actor=_actor(request), action=action,
                       target=target, project_id=None)


@router.get("")
def list_storage(
    request: Request, include_archived: bool = False, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    entries = service.list_entries(db, ws.id, _project_id(request), see_all=_see_all(request),
                                   include_archived=include_archived)
    return {"storage": entries}


@router.post("", status_code=201)
def register_storage(
    body: RegisterBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    fields = body.model_dump(exclude={"check"})
    row = service.register(db, ws.id, fields, _actor(request))
    _audit(ws, request, "storage.register", f"storage:{row.id}")
    out = service.get_entry(db, ws.id, row.id, None, see_all=True)
    if body.check:
        try:
            out = service.run_check(db, ws.context, row.id)
        except AppError as exc:
            # registered all the same; the page offers the check again
            out = {**out, "check_error": {"code": exc.code, "message": exc.message}}
    return out


@router.post("/provision", status_code=202)
def provision_storage(
    body: ProvisionBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Queue the console's S3 Files creation (§7); poll `GET …/provision/jobs/{job_id}`.
    A second call for the same id returns its running job, or resumes a failed one."""
    job, started = provision.queue_create(db, ws.context, body.model_dump(), _actor(request))
    if started:
        provision.start_async(job.id)
        _audit(ws, request, "storage.provision", f"storage:{body.id}")
    return provision.view(job)


@router.get("/provision/jobs")
def list_provision_jobs(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"jobs": provision.list_jobs(db, ws.id)}


@router.get("/provision/jobs/{job_id}")
def get_provision_job(
    job_id: str, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return provision.get_job(db, ws.id, job_id)


@router.post("/{storage_id}/teardown", status_code=202)
def teardown_storage(
    storage_id: str, body: TeardownBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Delete what the console made for an entry and archive it (§7); the bucket and its
    objects are kept. Registered (not console-made) storage is refused."""
    job, started = provision.queue_delete(db, ws.context, storage_id, body.confirm,
                                          _actor(request))
    if started:
        provision.start_async(job.id)
        _audit(ws, request, "storage.teardown", f"storage:{storage_id}")
    return provision.view(job)


@router.post("/preflight")
def preflight(
    body: PreflightBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    spec = {
        "method": body.method,
        "filesystem": {"mounts": [m.model_dump() for m in body.mounts]},
        "network": {"profile_id": body.network_profile_id} if body.network_profile_id else None,
        "byoc": {"artifact_kind": body.byoc_artifact_kind} if body.byoc_artifact_kind else None,
    }
    project_id = _project_id(request)
    return service.preflight_spec(db, ws.context, spec,
                                  None if _see_all(request) and project_id is None else project_id)


@router.get("/projects/{project_id}")
def get_project_storage(
    project_id: str, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return service.project_entries(db, ws.id, project_id)


@router.put("/projects/{project_id}")
def put_project_storage(
    project_id: str, body: ProjectStorageBody, request: Request,
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    result = service.set_project_entries(db, ws.id, project_id, body.storage_ids)
    _audit(ws, request, "storage.project_set", f"project:{project_id}")
    return result


@router.get("/agents/{agent_id}")
def get_agent_storage(
    agent_id: str, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The agent's mounts and their entries' state. Another project's agent answers 404
    (the project-scoped agent lookup)."""
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return service.agent_storage(db, agent)


@router.get("/{storage_id}")
def get_storage(
    storage_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return service.get_entry(db, ws.id, storage_id, _project_id(request),
                             see_all=_see_all(request))


@router.get("/{storage_id}/usage")
def get_storage_usage(
    storage_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return service.entry_usage(db, ws.id, storage_id, _project_id(request),
                               see_all=_see_all(request))


@router.post("/{storage_id}/check")
def check_storage(
    storage_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    checked = service.run_check(db, ws.context, storage_id)
    _audit(ws, request, "storage.check", f"storage:{storage_id}")
    return checked


@router.patch("/{storage_id}")
def update_storage(
    storage_id: str, body: PatchBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    updated = service.update(db, ws.id, storage_id, body.model_dump(exclude_unset=True))
    _audit(ws, request, "storage.update", f"storage:{storage_id}")
    return updated


@router.delete("/{storage_id}", status_code=204)
def archive_storage(
    storage_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> None:
    service.archive(db, ws.id, storage_id)
    _audit(ws, request, "storage.archive", f"storage:{storage_id}")
