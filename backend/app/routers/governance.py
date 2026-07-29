"""Governance API — policy card, test-evaluate, decision log, traces, generation."""

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import PolicyDecision
from app.schemas.governance import (
    EngineRequest,
    GatewayModeRequest,
    PolicyCreateRequest,
    PolicyTransitionRequest,
    PolicyUpdateRequest,
    RegistryImportRequest,
    RetireLegacyRequest,
)
from app.schemas.governance import (
    GenerationRequest as ScopedGenerationRequest,
)
from app.services import governance as governance_service
from app.services import governance_evidence, mcp_client
from app.services import traces as trace_service
from app.services.agentcore.client import control_client, iam_client
from app.services.observability import cw_client

router = APIRouter(prefix="/api", tags=["governance"])

ROLE_BY_USER = {"river": "platform-admin", "demo": "hr-analyst"}
GATEWAY_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RESOURCE_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
OPERATION_ID = Path(pattern=r"^[a-f0-9]{32}$")


@router.get("/governance/gateways")
def get_gateways(refresh: bool = False) -> dict[str, Any]:
    settings = get_settings()
    return {
        "gateways": governance_service.list_gateway_views(
            control_client(),
            settings=settings,
            refresh=refresh,
        ),
        # one console session scopes exactly one account/region
        "account_id": settings.account_id or None,
        "region": settings.region,
    }


@router.get("/governance/gateways/{gateway_id}")
def get_gateway_detail(gateway_id: str = GATEWAY_ID) -> dict[str, Any]:
    return governance_service.gateway_detail(
        control_client(),
        iam_client(),
        gateway_id,
    )


@router.post("/governance/gateways/{gateway_id}/manage")
def manage_gateway(gateway_id: str = GATEWAY_ID) -> dict[str, Any]:
    return governance_service.manage_gateway(control_client(), gateway_id)


@router.delete("/governance/gateways/{gateway_id}/manage")
def unmanage_gateway(gateway_id: str = GATEWAY_ID) -> dict[str, Any]:
    return governance_service.unmanage_gateway(control_client(), gateway_id)


@router.get("/governance/gateways/{gateway_id}/registry-preview")
def get_gateway_registry_preview(gateway_id: str = GATEWAY_ID) -> dict[str, Any]:
    return governance_service.gateway_registry_preview(control_client(), gateway_id)


@router.post("/governance/gateways/{gateway_id}/registry-import")
def import_gateway_registry(
    req: RegistryImportRequest,
    gateway_id: str = GATEWAY_ID,
) -> dict[str, Any]:
    return governance_service.import_gateway_registry(
        control_client(),
        gateway_id,
        req,
    )


@router.post("/governance/gateways/{gateway_id}/retire-legacy-records")
def retire_gateway_legacy_records(
    req: RetireLegacyRequest,
    gateway_id: str = GATEWAY_ID,
) -> dict[str, Any]:
    return governance_service.retire_gateway_legacy_records(
        control_client(),
        gateway_id,
        req,
    )


@router.get("/governance/gateways/{gateway_id}/policies")
def get_gateway_policies(
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return governance_service.policies_view(control_client(), gateway_id, db=db)


@router.post("/governance/gateways/{gateway_id}/engine", status_code=202)
def attach_policy_engine(
    req: EngineRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    operation = governance_service.queue_engine_attach(
        db,
        control_client(),
        gateway_id,
        req,
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.post("/governance/gateways/{gateway_id}/policies", status_code=202)
def create_gateway_policy(
    req: PolicyCreateRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_create(
        db,
        control_client(),
        gateway_id,
        req,
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.put(
    "/governance/gateways/{gateway_id}/policies/{policy_id}",
    status_code=202,
)
def update_gateway_policy(
    req: PolicyUpdateRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    policy_id: str = RESOURCE_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_update(
        db,
        control_client(),
        gateway_id,
        policy_id,
        req,
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.post(
    "/governance/gateways/{gateway_id}/policies/{policy_id}/promote",
    status_code=202,
)
def promote_gateway_policy(
    req: PolicyTransitionRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    policy_id: str = RESOURCE_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_transition(
        db,
        control_client(),
        gateway_id,
        policy_id,
        req,
        rollback=False,
        evidence_count=governance_evidence.evidence_count(
            cw_client(), gateway_id, req.evidence_range
        ),
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.post(
    "/governance/gateways/{gateway_id}/policies/{policy_id}/rollback",
    status_code=202,
)
def rollback_gateway_policy(
    req: PolicyTransitionRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    policy_id: str = RESOURCE_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_transition(
        db,
        control_client(),
        gateway_id,
        policy_id,
        req,
        rollback=True,
        evidence_count=governance_evidence.evidence_count(
            cw_client(), gateway_id, req.evidence_range
        ),
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.post("/governance/gateways/{gateway_id}/mode", status_code=202)
def update_gateway_mode(
    req: GatewayModeRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    operation = governance_service.queue_gateway_mode(
        db,
        control_client(),
        iam_client(),
        gateway_id,
        req,
        evidence_count=governance_evidence.evidence_count(
            cw_client(), gateway_id, req.evidence_range
        ),
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.post("/governance/gateways/{gateway_id}/generations", status_code=202)
def start_gateway_generation(
    req: ScopedGenerationRequest,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = governance_service.start_generation(
        db,
        control_client(),
        gateway_id,
        req,
    )
    return {
        "operation": result["operation"],
        "generation_id": result["id"],
        "status": result["status"],
    }


@router.get(
    "/governance/gateways/{gateway_id}/generations/{generation_id}",
)
def get_gateway_generation(
    gateway_id: str = GATEWAY_ID,
    generation_id: str = RESOURCE_ID,
) -> dict[str, Any]:
    return governance_service.generation_view(
        control_client(),
        gateway_id,
        generation_id,
    )


@router.get("/governance/gateways/{gateway_id}/audit")
def get_gateway_audit(
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return {"changes": governance_service.list_audit(db, gateway_id)}


@router.get("/governance/gateways/{gateway_id}/decisions")
def get_gateway_decisions(
    gateway_id: str = GATEWAY_ID,
    range: Literal["1h", "6h", "24h", "7d"] = "24h",
    policy_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    return governance_evidence.gateway_decisions(
        control_client(),
        cw_client(),
        gateway_id,
        range,
        policy_id,
        force,
    )


@router.get("/governance/operations/{operation_id}")
def get_governance_operation(
    operation_id: str = OPERATION_ID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return {"operation": governance_service.get_operation(db, operation_id)}


@router.get("/governance/policies")
def get_policies() -> dict[str, Any]:
    settings = get_settings()
    engine_id = settings.resources.get("policy_engine_id")
    if not engine_id:
        raise AppError("policy.not_bootstrapped", "policy engine missing — run bootstrap",
                       status_code=503)
    control = control_client()
    engine = control.get_policy_engine(policyEngineId=engine_id)
    gateway = control.get_gateway(
        gatewayIdentifier=settings.resources.get("gateway_id")
    )
    policies = []
    for summary in control.list_policies(policyEngineId=engine_id, maxResults=20).get(
        "policies", []
    ):
        detail = control.get_policy(
            policyEngineId=engine_id, policyId=summary["policyId"]
        )
        policies.append(
            {
                "id": detail["policyId"],
                "name": detail["name"],
                "status": detail["status"],
                "statement": detail.get("definition", {}).get("cedar", {}).get("statement", ""),
            }
        )
    attach = gateway.get("policyEngineConfiguration") or {}
    return {
        "engine": {
            "id": engine["policyEngineId"],
            "name": engine["name"],
            "status": engine["status"],
            "attached_mode": attach.get("mode"),
            "attached": attach.get("arn") == engine["policyEngineArn"],
        },
        "policies": policies,
    }


class PolicyTestRequest(BaseModel):
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    username: str = Field(default="demo", pattern="^(river|demo)$")


@router.post("/governance/policy-test")
def policy_test(req: PolicyTestRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Evaluate a real tools/call as the chosen principal and record the decision."""
    principal = f"{req.username}@{ROLE_BY_USER.get(req.username, 'user')}"
    outcome, reason = "ALLOW", None
    try:
        result = mcp_client.tools_call(req.tool, req.arguments, username=req.username)
        excerpt = str(result)[:300]
    except AppError as exc:
        outcome = "DENY"
        reason = str(exc.detail or exc.message)[:300]
        excerpt = reason
    decision = PolicyDecision(
        principal=principal, tool=req.tool, outcome=outcome, reason=reason
    )
    db.add(decision)
    db.commit()
    return {
        "principal": principal,
        "tool": req.tool,
        "outcome": outcome,
        "detail": excerpt,
        "decision_id": decision.id,
    }


@router.get("/governance/decisions")
def decision_log(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = (
        db.query(PolicyDecision).order_by(PolicyDecision.created_at.desc()).limit(30).all()
    )
    return {
        "decisions": [
            {
                "at": r.created_at.isoformat() if r.created_at else None,
                "principal": r.principal,
                "tool": r.tool,
                "outcome": r.outcome,
                "reason": (r.reason or "")[:160],
                "source": "demo",
            }
            for r in rows
        ],
        "source": "demo",
    }


@router.get("/traces/{session_id}")
def get_trace(session_id: str, lookback_hours: int = 3) -> dict[str, Any]:
    return trace_service.session_trace(session_id, lookback_hours=lookback_hours)


class GenerationRequest(BaseModel):
    text: str = Field(min_length=10, max_length=2000)
    name: str = Field(default="launchpad_generated", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")


@router.post("/governance/policy-generation")
def start_generation(req: GenerationRequest) -> dict[str, Any]:
    """AI policy generation from natural language (preview API — surfaced honestly)."""
    settings = get_settings()
    engine_id = settings.resources.get("policy_engine_id")
    gateway_arn = settings.resources.get("gateway_arn")
    if not engine_id or not gateway_arn:
        raise AppError("policy.not_bootstrapped", "run bootstrap first", status_code=503)
    control = control_client()
    try:
        generation = control.start_policy_generation(
            policyEngineId=engine_id,
            name=req.name,
            content={"rawText": req.text},
            resource={"arn": gateway_arn},
        )
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {
        "available": True,
        "generation_id": generation.get("policyGenerationId"),
        "status": generation.get("status"),
    }


@router.get("/governance/policy-generation/{generation_id}")
def get_generation(generation_id: str) -> dict[str, Any]:
    settings = get_settings()
    engine_id = settings.resources.get("policy_engine_id")
    control = control_client()
    generation = control.get_policy_generation(
        policyEngineId=engine_id, policyGenerationId=generation_id
    )
    assets: list[dict[str, Any]] = []
    if generation.get("status") == "GENERATED":
        assets = control.list_policy_generation_assets(
            policyEngineId=engine_id, policyGenerationId=generation_id, maxResults=10
        ).get("policyGenerationAssets", [])
    return {
        "status": generation.get("status"),
        "assets": [
            {
                "id": a.get("policyGenerationAssetId") or a.get("assetId"),
                "statement": (
                    a.get("definition", {}).get("cedar", {}).get("statement", "")
                    or str(a.get("finding", ""))
                )[:800],
            }
            for a in assets
        ],
    }
