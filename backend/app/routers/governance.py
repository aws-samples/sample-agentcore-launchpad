"""Governance API — Gateway lifecycle, rate limits, and target synchronization."""

from typing import Any

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.governance import RateLimitCreateRequest, RateLimitUpdateRequest
from app.services import governance as governance_service
from app.services.agentcore.client import control_client

router = APIRouter(prefix="/api", tags=["governance"])

GATEWAY_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# AWS rateLimitId: 2-64 chars, dots allowed (unlike policy/target ids)
RATE_LIMIT_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9]$")
# AWS targetId: exactly 10 alphanumerics
TARGET_ID = Path(pattern=r"^[0-9a-zA-Z]{10}$")


@router.get("/governance/gateways")
def get_gateways(
    refresh: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {
        "gateways": governance_service.list_gateway_views(
            control_client(ws.context),
            ws.context,
            refresh=refresh,
        ),
        # one console session scopes exactly one account/region
        "account_id": ws.context.account_id or None,
        "region": ws.context.region,
    }


@router.get("/governance/gateways/{gateway_id}")
def get_gateway_detail(
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.gateway_detail(
        control_client(ws.context),
        gateway_id,
        ws.context,
    )


@router.post("/governance/gateways/{gateway_id}/manage")
def manage_gateway(
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.manage_gateway(control_client(ws.context), gateway_id)


@router.delete("/governance/gateways/{gateway_id}/manage")
def unmanage_gateway(
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.unmanage_gateway(control_client(ws.context), gateway_id)


@router.get("/governance/gateways/{gateway_id}/rate-limits")
def get_gateway_rate_limits(
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.list_rate_limits(control_client(ws.context), gateway_id)


@router.post("/governance/gateways/{gateway_id}/rate-limits", status_code=201)
def create_gateway_rate_limit(
    req: RateLimitCreateRequest,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.create_rate_limit(
        db, control_client(ws.context), ws.id, gateway_id, req
    )


@router.put("/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}")
def update_gateway_rate_limit(
    req: RateLimitUpdateRequest,
    gateway_id: str = GATEWAY_ID,
    rate_limit_id: str = RATE_LIMIT_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.update_rate_limit(
        db, control_client(ws.context), ws.id, gateway_id, rate_limit_id, req
    )


@router.delete("/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}")
def delete_gateway_rate_limit(
    gateway_id: str = GATEWAY_ID,
    rate_limit_id: str = RATE_LIMIT_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.delete_rate_limit(
        db, control_client(ws.context), ws.id, gateway_id, rate_limit_id
    )


@router.post(
    "/governance/gateways/{gateway_id}/targets/{target_id}/synchronize",
    status_code=202,
)
def synchronize_gateway_target(
    gateway_id: str = GATEWAY_ID,
    target_id: str = TARGET_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    # SynchronizeGatewayTargets answers 202 and the target moves to SYNCHRONIZING;
    # the console polls the gateway detail (no local operation row) for the outcome.
    return governance_service.synchronize_target(
        db, control_client(ws.context), ws.id, gateway_id, target_id
    )
