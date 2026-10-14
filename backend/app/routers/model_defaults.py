"""Platform model settings for the console (app.services.model_settings).

- ``GET /api/model-defaults`` (member): every role's effective model, what a reset
  returns to, and whether an administrator overrode it — the console pickers take
  their pre-selection from here, so a change needs no frontend rebuild.
- ``PUT`` / ``DELETE /api/admin/models/{role}`` (admin): set or clear a console
  override; applied at once, persisted in the ledger, no restart.
- ``POST /api/admin/models/probe`` (admin): one small real Converse call.

Model ids only and hub-global (one ledger table, no workspace column), so none of
these routes is workspace-scoped; the probe runs in the hub's default workspace.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import model_settings
from app.services.workspace import default_workspace_context

router = APIRouter(tags=["models"])


@router.get("/api/model-defaults")
def model_defaults(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"roles": model_settings.effective(db)}


class ModelOverrideIn(BaseModel):
    model_id: str = Field(max_length=128)


@router.put("/api/admin/models/{role}")
def set_model_override(
    role: str, body: ModelOverrideIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    actor = require_identity(request)
    return model_settings.set_override(db, role, body.model_id, actor.username)


@router.delete("/api/admin/models/{role}")
def clear_model_override(role: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return model_settings.clear_override(db, role)


class ModelProbeIn(BaseModel):
    model_id: str = Field(max_length=128)
    check: Literal["converse", "tool_use"] = "converse"


@router.post("/api/admin/models/probe")
def probe_model(body: ModelProbeIn) -> dict[str, Any]:
    return model_settings.probe(body.model_id, body.check, default_workspace_context())
