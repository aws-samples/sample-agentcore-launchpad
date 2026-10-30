"""Skill Lab console routes (`/api/skill-lab`)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import ownership
from app.services.creators import Creator
from app.services.visibility import Viewer, require_viewer
from app.skill_lab import artifacts as artifacts_svc
from app.skill_lab import jobs as jobs_svc
from app.skill_lab import runner
from app.skill_lab import task_assets as asset_svc
from app.skill_lab import tasksets as svc
from app.skill_lab import verdict as skill_lab_verdict

router = APIRouter(prefix="/api/skill-lab", tags=["skill-lab"])

WORKER_RESOURCE_KEYS = (
    "skill_lab_worker_runtime_arn",
    "skill_lab_worker_role_arn",
    "skill_lab_worker_image_digest",
)

FORBIDDEN = "skill_lab.forbidden"


def _require_job_creator(request: Request, row: Any, action: str) -> None:
    """Creator-or-admin on a job mutation (N13-obs15, E15 NC-15): cancel / resume /
    publish / delete change the job's state or the Registry record it came from. Runs
    after `get_job` (a hidden job stays a 404), before any status check."""
    ownership.require_creator_or_admin(
        row, require_identity(request), FORBIDDEN,
        message="only the job's creator or an administrator may " + action + " it",
        detail={"job_id": row.id, "action": action},
    )


def _require_taskset_creator(request: Request, row: Any, action: str) -> None:
    """Creator-or-admin on a task set mutation (PUT / DELETE / an expansion applied to
    it). Sample sets have no creator and keep their own `skill_lab.sample_readonly`
    refusal in the service — this gate never reaches them for members, administrators
    fall through to it."""
    ownership.require_creator_or_admin(
        row, require_identity(request), FORBIDDEN,
        message="only the task set's creator or an administrator may " + action + " it",
        detail={"taskset_id": row.id, "action": action},
    )


class TasksetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=96)  # max matches the ledger column
    description: str = Field(default="", max_length=500)
    mode: str  # single | split (validated by the service)
    tasks_by_split: dict[str, Any]


class TasksetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=96)
    description: str | None = Field(default=None, max_length=500)
    tasks_by_split: dict[str, Any]


@router.get("/status")
def skill_lab_status(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    """Provisioning state for the page banner: the worker keys are deliberately
    optional resources (a workspace bootstrapped before this feature has none)."""
    resources = ws.context.resources
    settings = get_settings()
    missing = [key for key in WORKER_RESOURCE_KEYS if not resources.get(key)]
    return {
        "provisioned": not missing,
        "missing": missing,
        "venv_ready": Path(settings.skill_lab_python).exists(),
        # What a job runs with when its params omit the models — the wizard
        # prefills these so it can't drift from an operator's config override.
        "default_target_model": settings.skill_lab_target_model_id,
        "default_judge_model": settings.skill_lab_judge_model_id,
        "target_backends": list(runner.TARGET_BACKENDS),
        "judge_modes": list(runner.JUDGE_MODES),
        # The SHARED agentic-judge prerequisite only: the sandbox launcher binary
        # must exist here (parsers run under it). It is deliberately not the whole
        # answer — the judge also shells out to the exec CLI, so a host with bwrap
        # but no CLI still fails every artifact task; that is the probe below.
        "agentic_judge_ready": _sandbox_launcher_present(settings),
        # The one exec CLI (D16): every judge model routes to it.
        "judge_opencode_ready": _which(runner.JUDGE_CLI_BINARY[runner.TARGET_BACKENDS[0]]),
        # Readiness for the CONFIGURED DEFAULT judge model through the route rule,
        # for a caller that does not want to reimplement it (bare curl).
        "judge_cli_ready": _which(
            runner.judge_cli_binary(settings.skill_lab_judge_model_id)
        ),
    }


def _which(binary: str) -> bool:
    import shutil

    return shutil.which(binary) is not None


def _sandbox_launcher_present(settings: Any) -> bool:
    import shlex

    argv = shlex.split(settings.skill_lab_judge_sandbox or "")
    return bool(argv) and _which(argv[0])


@router.post("/task-assets", status_code=201)
async def upload_task_assets(
    files: list[UploadFile] = File(...),  # noqa: B008 - FastAPI multipart declaration
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"assets": await asset_svc.stage_uploads(ws.id, files)}


@router.get("/tasksets")
def list_tasksets(
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return svc.list_tasksets(db, ws.id, viewer=viewer)


@router.post("/tasksets", status_code=201)
def create_taskset(
    body: TasksetCreate,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return svc.create_taskset(
        db,
        ws.id,
        name=body.name,
        description=body.description,
        mode=body.mode,
        tasks_by_split=body.tasks_by_split,
        creator=Creator.of_request(request),
    )


@router.get("/tasksets/{taskset_id}")
def get_taskset(
    taskset_id: str,
    full: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return svc.read_taskset(db, ws.id, taskset_id, full=full, viewer=viewer)


@router.put("/tasksets/{taskset_id}")
def update_taskset(
    taskset_id: str,
    body: TasksetUpdate,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_row(db, ws.id, taskset_id, viewer=viewer)  # a hidden task set is a missing one
    _require_taskset_creator(request, row, "edit")
    return svc.update_taskset(
        db,
        ws.id,
        taskset_id,
        name=body.name,
        description=body.description,
        tasks_by_split=body.tasks_by_split,
        viewer=viewer,
    )


@router.delete("/tasksets/{taskset_id}")
def delete_taskset(
    taskset_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = svc.get_row(db, ws.id, taskset_id, viewer=viewer)  # a hidden task set is a missing one
    _require_taskset_creator(request, row, "delete")
    svc.delete_taskset(db, ws.id, taskset_id, viewer=viewer)
    return {"ok": True}


# ── jobs ───────────────────────────────────────────────────────────────────


class JobCreate(BaseModel):
    type: str = "eval"  # eval | train | taskgen
    skill_source: dict[str, Any]
    # required for eval/train; optional for taskgen (expansion target only)
    taskset_id: str | None = None
    split: str | None = None
    # taskgen expansion only: which split the generated tasks will extend
    target_split: str | None = None
    # taskgen only: staged asset tokens the generation agent authors against
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/jobs")
def list_jobs(
    type: str | None = None,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return jobs_svc.list_jobs(db, ws.id, type_filter=type, viewer=viewer)


@router.post("/jobs", status_code=201)
def create_job(
    body: JobCreate,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    creator = Creator.of_request(request)
    if body.type in ("eval", "train") and not body.taskset_id:
        raise AppError("skill_lab.bad_params", "taskset_id is required", status_code=422)
    if body.type == "eval":
        return jobs_svc.submit_eval_job(
            db,
            ws.id,
            ws.context,
            skill_source=body.skill_source,
            taskset_id=body.taskset_id,
            split=body.split,
            params=body.params,
            creator=creator,
            viewer=viewer,
        )
    if body.type == "train":
        if body.split:
            raise AppError(
                "skill_lab.bad_params",
                "training uses the whole task set — omit `split`",
                status_code=422,
            )
        return jobs_svc.submit_train_job(
            db,
            ws.id,
            ws.context,
            skill_source=body.skill_source,
            taskset_id=body.taskset_id,
            params=body.params,
            creator=creator,
            viewer=viewer,
        )
    if body.type == "taskgen":
        if body.split:
            raise AppError(
                "skill_lab.bad_params",
                "taskgen expansion uses `target_split`, not `split`",
                status_code=422,
            )
        return jobs_svc.submit_taskgen_job(
            db,
            ws.id,
            ws.context,
            skill_source=body.skill_source,
            taskset_id=body.taskset_id,
            target_split=body.target_split,
            params=body.params,
            attachments=body.attachments,
            creator=creator,
            viewer=viewer,
        )
    raise AppError("skill_lab.bad_params", "type must be 'eval' or 'train'", status_code=422)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return jobs_svc.job_out(jobs_svc.get_job(db, ws.id, job_id, viewer=viewer))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # a hidden job is a missing one
    _require_job_creator(request, row, "cancel")
    return jobs_svc.cancel_job(db, ws.id, job_id, viewer=viewer)


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # a hidden job is a missing one
    _require_job_creator(request, row, "delete")
    jobs_svc.delete_job(db, ws.id, job_id, viewer=viewer)
    return {"ok": True}


@router.get("/jobs/{job_id}/log")
def job_log(
    job_id: str,
    offset: int = 0,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # workspace ownership gate
    return artifacts_svc.read_log(job_id, offset)


@router.get("/jobs/{job_id}/results")
def job_results(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)
    results = (
        artifacts_svc.taskgen_results(job_id)
        if row.type == "taskgen"
        else artifacts_svc.eval_results(job_id)
    )
    if results is None:
        raise AppError(
            "skill_lab.results_pending",
            f"results not available yet (job status: {row.status})",
            status_code=404,
        )
    if row.type == "taskgen":
        return {"type": row.type, **results}
    # N10v3-bug3: errored rollouts (scored zeros included) are stated in the summary
    return skill_lab_verdict.with_errored_count(results)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


# One reviewed row: WHICH generated row (by its index in generated_tasks.json) plus
# the author fields the reviewer may change. `extra="forbid"` is the security
# boundary — a client can never smuggle `files`, `attachments` or any other key;
# the server rebuilds those from the job's own artifacts. `index` is strict so a
# bool / "1" / 1.0 cannot select a row by coercion.
_TaskgenId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=jobs_svc.TASKGEN_EDIT_ID_MAX_CHARS
    ),
]
_TaskgenText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=jobs_svc.TASKGEN_EDIT_TEXT_MAX_CHARS),
    AfterValidator(_not_blank),
]
_TaskgenType = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=jobs_svc.TASKGEN_EDIT_TYPE_MAX_CHARS)
]


class TaskgenRowEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(strict=True, ge=0, le=svc.MAX_TASKS_PER_SPLIT - 1)
    id: _TaskgenId | None = None
    question: _TaskgenText | None = None
    rubric: _TaskgenText | None = None
    # "" clears the field (loader default applies); None keeps the generated value.
    task_type: _TaskgenType | None = None


_TaskgenSelection = Annotated[
    list[TaskgenRowEdit] | None, Field(max_length=svc.MAX_TASKS_PER_SPLIT)
]


class ImportTasksetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    # Omitted/null → every generated row verbatim (legacy full import).
    tasks: _TaskgenSelection = None


class ApplyExpansionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: _TaskgenSelection = None


def _edits(selection: list[TaskgenRowEdit] | None) -> list[dict[str, Any]] | None:
    return None if selection is None else [row.model_dump() for row in selection]


@router.post("/jobs/{job_id}/import-taskset", status_code=201)
def import_taskgen_taskset(
    job_id: str,
    body: ImportTasksetRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save a succeeded taskgen job's reviewed tasks as a new task set (created by
    whoever imports it)."""
    jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # a hidden job is a missing one
    return jobs_svc.import_taskgen_taskset(
        db, ws.id, job_id, name=body.name, edits=_edits(body.tasks),
        creator=Creator.of_request(request), viewer=viewer,
    )


@router.post("/jobs/{job_id}/apply-expansion")
def apply_taskgen_expansion(
    job_id: str,
    request: Request,
    body: ApplyExpansionRequest | None = None,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Append a succeeded expansion job's tasks to its target task set/split.
    The body is optional: no body (or no `tasks`) appends every generated row.
    The write lands on the TARGET task set, so its creator (or an administrator) is
    the one who may apply it — the job's creator alone is not enough."""
    job = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # a hidden job is a missing one
    if job.taskset_id:
        try:
            # unfiltered on purpose: this is ownership, not visibility — a target the job's
            # viewer cannot see is still somebody's; a target that is gone is nobody's and
            # the service answers its own 404 for it
            target = svc.get_row(db, ws.id, job.taskset_id)
        except AppError:
            target = None
        if target is not None:
            _require_taskset_creator(request, target, "expand")
    return jobs_svc.apply_taskgen_expansion(
        db, ws.id, job_id, edits=_edits(body.tasks) if body is not None else None,
        viewer=viewer,
    )


class PublishRequest(BaseModel):
    reapprove: bool = False


@router.post("/jobs/{job_id}/resume")
def resume_job(
    job_id: str,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # a hidden job is a missing one
    _require_job_creator(request, row, "resume")
    return jobs_svc.resume_job(db, ws.id, ws.context, job_id, viewer=viewer)


@router.post("/jobs/{job_id}/publish")
def publish_job(
    job_id: str,
    body: PublishRequest,
    request: Request,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)  # a hidden job is a missing one
    _require_job_creator(request, row, "publish")
    # N17-obs248: only an administrator's publish re-approves; a member's re-submits.
    # N17-obs263: the identity also gates the SOURCE record (creator-or-admin).
    identity = require_identity(request)
    return jobs_svc.publish_job(
        db, ws.id, ws.context, job_id, reapprove=body.reapprove, viewer=viewer,
        is_admin=identity.is_admin, identity=identity,
    )


@router.get("/jobs/{job_id}/train-summary")
def job_train_summary(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)
    summary = artifacts_svc.train_summary(job_id)
    if summary is None:
        raise AppError(
            "skill_lab.results_pending",
            f"no training steps recorded yet (job status: {row.status})",
            status_code=404,
        )
    return summary


@router.get("/jobs/{job_id}/diff")
def job_skill_diff(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)
    diff = artifacts_svc.skill_diff(job_id)
    if diff is None:
        raise AppError(
            "skill_lab.results_pending",
            f"skill versions not written yet (job status: {row.status})",
            status_code=404,
        )
    return diff


@router.get("/jobs/{job_id}/artifacts")
def job_artifacts(
    job_id: str,
    path: str = "",
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)
    return artifacts_svc.list_artifacts(job_id, path)


@router.get("/jobs/{job_id}/artifacts/raw")
def job_artifact_raw(
    job_id: str,
    path: str,
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> FileResponse:
    jobs_svc.get_job(db, ws.id, job_id, viewer=viewer)
    target = artifacts_svc.artifact_file(job_id, path)
    return FileResponse(target, filename=target.name)
