"""System-managed presets API — status (ledger read), admin install/repair, uninstall.

The install route is the *only* way a preset reaches AWS, and it is an explicit,
billable operator action: nothing here runs on startup, and the status read never
touches a cloud client. Authorization comes from ``ROUTE_POLICY`` (GET member, POST /
DELETE admin) plus the same check re-asserted in the handlers, so a table edit alone
cannot open the mutation paths.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import NotFoundError
from app.deployer.pipeline import start_deploy_async
from app.routers.agents import _agent_out, _delete_agent_resources
from app.routers.auth import require_admin, require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import KnowledgeBaseRef, ModelSource
from app.system_agents import presets as catalogue
from app.system_agents import service
from app.system_agents.presets import InstallOptions

router = APIRouter(prefix="/api/system-agents", tags=["system-agents"])


class InstallRequest(BaseModel):
    """The administrator's choices. An empty JSON object (``{}``) installs with the
    platform defaults or repairs with the currently stored choices; the body itself
    is required."""

    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    model_source: ModelSource | None = None
    # Existing, already-authorized knowledge bases only — never created here.
    knowledge_bases: list[KnowledgeBaseRef] | None = Field(default=None, max_length=10)
    # Re-publish even when nothing changed (e.g. after an out-of-band AWS change).
    force: bool = False

    def options(self, stored: dict[str, Any] | None) -> InstallOptions | None:
        if self.model_id is None and self.model_source is None and self.knowledge_bases is None:
            return None  # repair with what is stored / install with defaults
        base = catalogue.options_from_spec(stored or {})
        return InstallOptions(
            model_id=self.model_id or base.model_id,
            model_source=self.model_source or base.model_source,
            knowledge_bases=(
                tuple(self.knowledge_bases)
                if self.knowledge_bases is not None
                else base.knowledge_bases
            ),
        )


def _preset(preset_key: str):
    preset = catalogue.get_preset(preset_key)
    if preset is None:
        raise NotFoundError("system_agent.unknown", f"no system preset named '{preset_key}'")
    return preset


@router.get("")
def list_system_agents(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    identity = require_identity(request)
    return {
        "workspace_id": ws.id,
        "presets": service.list_status(db, ws.row, is_admin=identity.is_admin),
    }


@router.post("/{preset_key}/install")
def install_system_agent(
    preset_key: str,
    req: InstallRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> Any:
    from fastapi.responses import JSONResponse

    require_admin(request)  # belt and braces with ROUTE_POLICY
    preset = _preset(preset_key)
    stored = service.find_installed(db, ws.id, preset)
    outcome = service.install_preset(
        db, ws.row, preset, req.options(stored.spec if stored else None), force=req.force
    )
    db.commit()
    if outcome.job is not None and outcome.changed:  # only the claim winner launches
        start_deploy_async(outcome.job.id)
    body = {
        "agent": _agent_out(outcome.agent, outcome.deployment),
        "job_id": outcome.job.id if outcome.job else None,
        "deployment_id": outcome.deployment.id if outcome.deployment else None,
        "created": outcome.created,
        "changed": outcome.changed,
        "preset": service.preset_status(db, ws.row, preset, is_admin=True),
    }
    return JSONResponse(status_code=outcome.status_code, content=body)


@router.delete("/{preset_key}")
def uninstall_system_agent(
    preset_key: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    require_admin(request)
    preset = _preset(preset_key)
    return service.uninstall_preset(
        db, ws.row, preset, lambda agent: _delete_agent_resources(agent, ws.context)
    )
