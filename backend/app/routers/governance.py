"""Governance API — policy card, test-evaluate, decision log, traces, generation."""

import re
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.ledger import PolicyDecision
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.governance import (
    EngineRequest,
    GatewayModeRequest,
    PolicyCreateRequest,
    PolicyDeleteRequest,
    PolicyTransitionRequest,
    PolicyUpdateRequest,
    RateLimitCreateRequest,
    RateLimitUpdateRequest,
    RegistryImportRequest,
    RetireLegacyRequest,
)
from app.schemas.governance import (
    GenerationRequest as ScopedGenerationRequest,
)
from app.services import governance as governance_service
from app.services import governance_evidence, mcp_client
from app.services import traces as trace_service
from app.services.agentcore import policy as policy_api
from app.services.agentcore.client import control_client, iam_client
from app.services.observability import cw_client, logs_client

router = APIRouter(prefix="/api", tags=["governance"])

ROLE_BY_USER = {"admin": "platform-admin", "demo": "hr-analyst"}
GATEWAY_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RESOURCE_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
OPERATION_ID = Path(pattern=r"^[a-f0-9]{32}$")
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
        iam_client(ws.context),
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


@router.get("/governance/gateways/{gateway_id}/registry-preview")
def get_gateway_registry_preview(
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.gateway_registry_preview(
        control_client(ws.context), gateway_id, ws.context
    )


@router.post("/governance/gateways/{gateway_id}/registry-import")
def import_gateway_registry(
    req: RegistryImportRequest,
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.import_gateway_registry(
        control_client(ws.context),
        gateway_id,
        req,
        ws.context,
    )


@router.post("/governance/gateways/{gateway_id}/retire-legacy-records")
def retire_gateway_legacy_records(
    req: RetireLegacyRequest,
    gateway_id: str = GATEWAY_ID,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.retire_gateway_legacy_records(
        control_client(ws.context),
        gateway_id,
        req,
        ws.context,
    )


@router.get("/governance/gateways/{gateway_id}/policies")
def get_gateway_policies(
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.policies_view(
        control_client(ws.context), gateway_id, db=db
    )


@router.post("/governance/gateways/{gateway_id}/engine", status_code=202)
def attach_policy_engine(
    req: EngineRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_engine_attach(
        db,
        control_client(ws.context),
        ws.context,
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
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_create(
        db,
        control_client(ws.context),
        ws.context,
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
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_update(
        db,
        control_client(ws.context),
        ws.context,
        gateway_id,
        policy_id,
        req,
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.delete(
    "/governance/gateways/{gateway_id}/policies/{policy_id}",
    status_code=202,
)
def delete_gateway_policy(
    req: PolicyDeleteRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    policy_id: str = RESOURCE_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_delete(
        db,
        control_client(ws.context),
        ws.context,
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
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_transition(
        db,
        control_client(ws.context),
        ws.context,
        gateway_id,
        policy_id,
        req,
        rollback=False,
        evidence_count=governance_evidence.evidence_count(
            cw_client(ws.context), gateway_id, req.evidence_range
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
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_policy_transition(
        db,
        control_client(ws.context),
        ws.context,
        gateway_id,
        policy_id,
        req,
        rollback=True,
        evidence_count=governance_evidence.evidence_count(
            cw_client(ws.context), gateway_id, req.evidence_range
        ),
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


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
    # Synchronous control-plane call: the journal row is written around it inline,
    # not through the 202/operation machinery the policy mutations use.
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


@router.post("/governance/gateways/{gateway_id}/mode", status_code=202)
def update_gateway_mode(
    req: GatewayModeRequest,
    background_tasks: BackgroundTasks,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    operation = governance_service.queue_gateway_mode(
        db,
        control_client(ws.context),
        ws.context,
        iam_client(ws.context),
        gateway_id,
        req,
        evidence_count=governance_evidence.evidence_count(
            cw_client(ws.context), gateway_id, req.evidence_range
        ),
    )
    background_tasks.add_task(governance_service.run_policy_change, operation["id"])
    return {"operation": operation}


@router.post("/governance/gateways/{gateway_id}/generations", status_code=202)
def start_gateway_generation(
    req: ScopedGenerationRequest,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    result = governance_service.start_generation(
        db,
        control_client(ws.context),
        ws.context,
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
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_service.generation_view(
        control_client(ws.context),
        gateway_id,
        generation_id,
    )


@router.get("/governance/gateways/{gateway_id}/audit")
def get_gateway_audit(
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"changes": governance_service.list_audit(db, ws.id, gateway_id)}


@router.get("/governance/gateways/{gateway_id}/decisions")
def get_gateway_decisions(
    gateway_id: str = GATEWAY_ID,
    range: Literal["1h", "6h", "24h", "7d"] = "24h",
    policy_id: str | None = None,
    force: bool = False,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return governance_evidence.gateway_decisions(
        control_client(ws.context),
        cw_client(ws.context),
        gateway_id,
        range,
        ws.id,
        policy_id,
        force,
        logs=logs_client(ws.context),
    )


@router.get("/governance/operations/{operation_id}")
def get_governance_operation(
    operation_id: str = OPERATION_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return {"operation": governance_service.get_operation(db, ws.id, operation_id)}


@router.get("/governance/policies")
def get_policies(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    control = control_client(ws.context)
    engine_id = governance_service.require_attached_policy_engine_id(control, ws.context)
    engine = policy_api.find_policy_engine(control, engine_id)
    if engine is None:
        raise AppError(
            "policy.engine_deleted",
            governance_service.DANGLING_ENGINE_REASON,
            {"engine_id": engine_id},
            status_code=503,
        )
    gateway = control.get_gateway(
        gatewayIdentifier=ws.context.resources.get("gateway_id")
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
    username: str = Field(default="demo", pattern="^(admin|demo)$")


"""The gateway refused the call on authorization grounds — a real decision."""
_DENY_CODES = frozenset({"gateway.unauthorized"})  # gateway answered 401/403

"""No authorization answer was ever obtained. These must never be journaled: the
decision ledger is audit-facing evidence, and a Cognito or config outage recorded as
a Cedar denial manufactures a denial that never happened."""
_ERROR_CODES = frozenset(
    {
        "gateway.credentials_rejected",
        "gateway.identity_unavailable",
        "gateway.no_credentials",
        "gateway.not_bootstrapped",
        "gateway.bad_response",
    }
)

# JSON-RPC error code the Gateway returns for a Cedar denial, captured from a real
# response (see the task research note). Preferred over message matching because it
# survives wording changes.
POLICY_DENIED_RPC_CODE = -32002
DETERMINING_POLICY_RE = re.compile(
    r"Policy evaluation denied due to ([A-Za-z0-9][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)


def _is_policy_denial(exc: AppError) -> bool:
    """`gateway.rpc_error` wraps any JSON-RPC error, so it needs discriminating.

    Every signal below is lifted from the captured denial

        code -32002, "Tool Execution Denied: Tool call not allowed due to policy
        enforcement [Policy evaluation denied due to <policy-id>]"

    and any one of them is enough, so a reworded message or a changed error code does
    not silently break detection. Anything unrecognised is deliberately NOT treated as
    a denial: it degrades to a non-decision, which is visible and leaves the ledger
    clean, rather than inventing either an ALLOW or a DENY.
    """
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    if detail.get("code") == POLICY_DENIED_RPC_CODE:
        return True
    message = f"{detail.get('message') or ''} {exc.message}".lower()
    if "tool execution denied" in message:
        return True
    return "policy" in message and ("denied" in message or "not allowed" in message)


def _classify(exc: AppError) -> str:
    if exc.code in _DENY_CODES:
        return "DENY"
    if exc.code in _ERROR_CODES:
        return "ERROR"
    if exc.code == "gateway.rpc_error" and _is_policy_denial(exc):
        return "DENY"
    return "ERROR"


def _determining_policy_id(exc: AppError) -> str | None:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    explicit = detail.get("policy")
    if isinstance(explicit, str) and explicit:
        return explicit
    message = f"{detail.get('message') or ''} {exc.message}"
    match = DETERMINING_POLICY_RE.search(message)
    return match.group(1) if match else None


@router.post("/governance/policy-test")
def policy_test(
    req: PolicyTestRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Evaluate a real tools/call as the chosen principal and record the decision.

    Only ALLOW and DENY are authorization decisions and get journaled. ERROR means
    the evaluation never happened (bad credentials, gateway unreachable, an
    unrecognised failure) and writes nothing — an error is not a decision.
    """
    principal = f"{req.username}@{ROLE_BY_USER.get(req.username, 'user')}"
    outcome, reason = "ALLOW", None
    policy_id = None
    try:
        result = mcp_client.tools_call(
            ws.context, req.tool, req.arguments, username=req.username
        )
        # A tool that fails its own validation returns a successful MCP result with
        # isError: true — the authorization question was still answered with a permit,
        # so this stays ALLOW.
        excerpt = str(result)[:300]
    except AppError as exc:
        outcome = _classify(exc)
        reason = str(exc.detail or exc.message)[:300]
        excerpt = reason
        if outcome == "DENY":
            policy_id = _determining_policy_id(exc)

    decision_id = None
    if outcome in ("ALLOW", "DENY"):
        decision = PolicyDecision(
            workspace_id=ws.id,
            principal=principal,
            tool=req.tool,
            outcome=outcome,
            reason=reason,
        )
        db.add(decision)
        db.commit()
        decision_id = decision.id
    return {
        "principal": principal,
        "tool": req.tool,
        "outcome": outcome,
        "detail": excerpt,
        "policy_id": policy_id,
        "decision_id": decision_id,
        "recorded": decision_id is not None,
    }


@router.get("/governance/decisions")
def decision_log(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    rows = (
        db.query(PolicyDecision)
        .filter(PolicyDecision.workspace_id == ws.id)
        .order_by(PolicyDecision.created_at.desc())
        .limit(30)
        .all()
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
def get_trace(
    session_id: str,
    lookback_hours: int = 3,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return trace_service.session_trace(
        session_id, ws.context, lookback_hours=lookback_hours
    )


class GenerationRequest(BaseModel):
    text: str = Field(min_length=10, max_length=2000)
    name: str = Field(default="launchpad_generated", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")


@router.post("/governance/policy-generation")
def start_generation(
    req: GenerationRequest,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """AI policy generation from natural language (preview API — surfaced honestly)."""
    gateway_arn = ws.context.resources.get("gateway_arn")
    control = control_client(ws.context)
    engine_id = governance_service.require_attached_policy_engine_id(control, ws.context)
    if not gateway_arn:
        raise AppError(
            "policy.not_bootstrapped",
            "this workspace has no gateway — run its bootstrap first",
            status_code=503,
        )
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
def get_generation(
    generation_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    control = control_client(ws.context)
    engine_id = governance_service.require_attached_policy_engine_id(control, ws.context)
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
