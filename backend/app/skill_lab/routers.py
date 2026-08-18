"""Skill Lab console routes (`/api/skill-lab`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.routers.workspaces import WorkspaceScope, require_workspace
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
    missing = [key for key in WORKER_RESOURCE_KEYS if not resources.get(key)]
    return {
        "provisioned": not missing,
        "missing": missing,
        "venv_ready": Path(get_settings().skill_lab_python).exists(),
    }


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
