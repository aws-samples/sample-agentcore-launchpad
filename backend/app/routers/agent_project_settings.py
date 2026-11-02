"""The agent-project switch (``services/agent_project_settings``): may a new agent be
filed under a workspace ``default`` project? Hub-global, administrators only — both
the read and the write (a steward does not loosen where data lands)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import Identity, require_admin
from app.services import agent_project_settings

router = APIRouter(tags=["settings"])


class AgentProjectSettingsBody(BaseModel):
    # true / false = the console override; null = clear it (env / yaml / default rule)
    allow_default_project: bool | None


@router.get("/api/settings/agent-projects")
def get_agent_project_settings(
    db: Session = Depends(get_db),
    _: Identity = Depends(require_admin),
) -> dict[str, Any]:
    return agent_project_settings.view(db)


@router.put("/api/settings/agent-projects")
def put_agent_project_settings(
    body: AgentProjectSettingsBody,
    db: Session = Depends(get_db),
    identity: Identity = Depends(require_admin),
) -> dict[str, Any]:
    return agent_project_settings.set_allowed(db, body.allow_default_project, identity)
