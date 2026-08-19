"""Skill Lab console routes (`/api/skill-lab`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.skill_lab import artifacts as artifacts_svc
from app.skill_lab import jobs as jobs_svc
from app.skill_lab import runner
from app.skill_lab import tasksets as svc

router = APIRouter(prefix="/api/skill-lab", tags=["skill-lab"])

WORKER_RESOURCE_KEYS = (
    "skill_lab_worker_runtime_arn",
    "skill_lab_worker_role_arn",
    "skill_lab_worker_image_digest",
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
        "default_codex_target_model": settings.skill_lab_codex_target_model_id,
        "target_backends": list(runner.TARGET_BACKENDS),
        "judge_modes": list(runner.JUDGE_MODES),
        # Host-side agentic judge readiness: the sandbox launcher binary must
        # exist here (parsers run under it). When false, auto/agentic runs
        # still work for text-only tasks; artifact tasks fail closed.
        "agentic_judge_ready": _sandbox_launcher_present(settings),
        # An openai-family judge model routes the agentic judge to the host
        # codex CLI (runner.judge_exec_route) — warn the wizard when absent.
        "judge_codex_ready": _which("codex"),
    }


def _which(binary: str) -> bool:
    import shutil

    return shutil.which(binary) is not None


def _sandbox_launcher_present(settings: Any) -> bool:
    import shlex

    argv = shlex.split(settings.skill_lab_judge_sandbox or "")
    return bool(argv) and _which(argv[0])


@router.get("/tasksets")
def list_tasksets(
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return svc.list_tasksets(db, ws.id)


@router.post("/tasksets", status_code=201)
def create_taskset(
    body: TasksetCreate,
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
    )


@router.get("/tasksets/{taskset_id}")
def get_taskset(
    taskset_id: str,
    full: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return svc.read_taskset(db, ws.id, taskset_id, full=full)


@router.put("/tasksets/{taskset_id}")
def update_taskset(
    taskset_id: str,
    body: TasksetUpdate,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return svc.update_taskset(
        db,
        ws.id,
        taskset_id,
        name=body.name,
        description=body.description,
        tasks_by_split=body.tasks_by_split,
    )


@router.delete("/tasksets/{taskset_id}")
def delete_taskset(
    taskset_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    svc.delete_taskset(db, ws.id, taskset_id)
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
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/jobs")
def list_jobs(
    type: str | None = None,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return jobs_svc.list_jobs(db, ws.id, type_filter=type)


@router.post("/jobs", status_code=201)
def create_job(
    body: JobCreate,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if body.type in ("eval", "train") and not body.taskset_id:
        raise AppError(
            "skill_lab.bad_params", "taskset_id is required", status_code=422
        )
    if body.type == "eval":
        return jobs_svc.submit_eval_job(
            db,
            ws.id,
            ws.context,
            skill_source=body.skill_source,
            taskset_id=body.taskset_id,
            split=body.split,
            params=body.params,
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
        )
    raise AppError(
        "skill_lab.bad_params", "type must be 'eval' or 'train'", status_code=422
    )


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return jobs_svc.job_out(jobs_svc.get_job(db, ws.id, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return jobs_svc.cancel_job(db, ws.id, job_id)


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    jobs_svc.delete_job(db, ws.id, job_id)
    return {"ok": True}


@router.get("/jobs/{job_id}/log")
def job_log(
    job_id: str,
    offset: int = 0,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    jobs_svc.get_job(db, ws.id, job_id)  # workspace ownership gate
    return artifacts_svc.read_log(job_id, offset)


@router.get("/jobs/{job_id}/results")
def job_results(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id)
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
    return {"type": row.type, **results} if row.type == "taskgen" else results


class ImportTasksetRequest(BaseModel):
    name: str


@router.post("/jobs/{job_id}/import-taskset", status_code=201)
def import_taskgen_taskset(
    job_id: str,
    body: ImportTasksetRequest,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save a succeeded taskgen job's reviewed tasks as a new task set."""
    return jobs_svc.import_taskgen_taskset(db, ws.id, job_id, name=body.name)


@router.post("/jobs/{job_id}/apply-expansion")
def apply_taskgen_expansion(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Append a succeeded expansion job's tasks to its target task set/split."""
    return jobs_svc.apply_taskgen_expansion(db, ws.id, job_id)


class PublishRequest(BaseModel):
    reapprove: bool = False


@router.post("/jobs/{job_id}/resume")
def resume_job(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return jobs_svc.resume_job(db, ws.id, ws.context, job_id)


@router.post("/jobs/{job_id}/publish")
def publish_job(
    job_id: str,
    body: PublishRequest,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return jobs_svc.publish_job(db, ws.id, ws.context, job_id, reapprove=body.reapprove)


@router.get("/jobs/{job_id}/train-summary")
def job_train_summary(
    job_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id)
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
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = jobs_svc.get_job(db, ws.id, job_id)
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
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    jobs_svc.get_job(db, ws.id, job_id)
    return artifacts_svc.list_artifacts(job_id, path)


@router.get("/jobs/{job_id}/artifacts/raw")
def job_artifact_raw(
    job_id: str,
    path: str,
    ws: WorkspaceScope = Depends(require_workspace),
    db: Session = Depends(get_db),
) -> FileResponse:
    jobs_svc.get_job(db, ws.id, job_id)
    target = artifacts_svc.artifact_file(job_id, path)
    return FileResponse(target, filename=target.name)
