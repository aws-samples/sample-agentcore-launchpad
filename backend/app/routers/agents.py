"""Agents API — create/deploy, list, invoke, delete; jobs polling."""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.deployer import container as container_method
from app.deployer import harness as harness_method
from app.deployer import zip_runtime as zip_method
from app.deployer.pipeline import create_deployment, start_deploy_async
from app.models.ledger import Agent, Deployment, Job
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import AgentSpec, InvokeRequest, InvokeResponse, RuntimeImportRequest
from app.services import agent_iam
from app.services.agent_versions import list_agent_versions
from app.services.agentcore.client import control_client
from app.services.invoke import invoke_agent_text
from app.services.memory import scoped_actor
from app.services.runtime_discovery import (
    DISCOVERED_METHOD,
    import_harnesses,
    import_runtimes,
    invoke_capability,
    require_invoke_capability,
    scan_harnesses,
    scan_runtimes,
)
from app.services.workspace import WorkspaceContext

logger = logging.getLogger("launchpad.agents")

router = APIRouter(prefix="/api", tags=["agents"])

SUPPORTED_METHODS = {"harness", "zip_runtime", "container", "studio"}


def _agent_out(agent: Agent, deployment: Deployment | None = None) -> dict[str, Any]:
    from app.optimization.service import canary_capability, experiment_capability

    out = {
        "id": agent.id,
        "name": agent.name,
        "method": agent.method,
        "status": agent.status,
        "arn": agent.arn,
        "resource_id": agent.resource_id,
        "registry_record_id": agent.registry_record_id,
        "version": agent.version,
        "owner": agent.owner,
        "error": agent.error,
        "spec": agent.spec,
        "experiment_capability": experiment_capability(agent),
        "canary_capability": canary_capability(agent),
        "invoke_capability": invoke_capability(agent),
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }
    if deployment is not None:
        out["deployment"] = _deployment_out(deployment)
    return out


def _deployment_out(dep: Deployment) -> dict[str, Any]:
    return {
        "id": dep.id,
        "agent_id": dep.agent_id,
        "job_id": dep.job_id,
        "status": dep.status,
        "stages": dep.stages,
        "started_at": dep.started_at.isoformat() if dep.started_at else None,
        "ended_at": dep.ended_at.isoformat() if dep.ended_at else None,
    }


def _agent_in(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent | None:
    """The agent, but only if it lives in this workspace.

    A foreign id is indistinguishable from a missing one on purpose: the caller
    learns nothing about other workspaces' agents.
    """
    agent = db.get(Agent, agent_id)
    return agent if agent is not None and agent.workspace_id == ws.id else None


def _latest_deployment(db: Session, agent_id: str) -> Deployment | None:
    return (
        db.query(Deployment)
        .filter(Deployment.agent_id == agent_id)
        .order_by(Deployment.started_at.desc())
        .first()
    )


def _delete_agent_resources(agent: Agent, workspace: WorkspaceContext) -> bool:
    """Tear down the method-specific AWS resource for an agent (idempotent)."""
    if agent.method == DISCOVERED_METHOD:
        return False
    if agent.method == "harness":
        harness_method.delete_agent_resources(agent, workspace)
    elif agent.method in ("zip_runtime", "studio"):
        zip_method.delete_agent_resources(agent, workspace)
    elif agent.method == "container":
        container_method.delete_agent_resources(agent, workspace)
    # After the resource, never before: deleting the execution role while the
    # runtime still references it can wedge the runtime's own deletion. A failed
    # role delete must not block deleting the agent, so this returns rather than
    # raises and logs the role name for a later sweep.
    agent_iam.delete_execution_role(
        agent,
        get_settings(),
        workspace,
        lambda msg: logger.info("agent %s: %s", agent.id, msg),
    )
    return True


@router.post("/agents", status_code=202)
def create_agent(
    spec: AgentSpec,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    if spec.method not in SUPPORTED_METHODS:
        raise AppError(
            "agent.method_not_available",
            f"method '{spec.method}' ships in a later phase",
            {"supported": sorted(SUPPORTED_METHODS)},
            status_code=400,
        )
    # Names are unique per workspace, not per ledger: two environments own their
    # own AgentCore resource namespaces.
    existing = (
        db.query(Agent)
        .filter(
            Agent.workspace_id == ws.id,
            Agent.name == spec.name,
            Agent.status != "deleted",
        )
        .first()
    )
    if existing:
        raise AppError(
            "agent.name_exists",
            f"an agent named '{spec.name}' already exists",
            {"agent_id": existing.id},
            status_code=409,
        )
    agent = Agent(
        workspace_id=ws.id,
        name=spec.name,
        method=spec.method,
        status="deploying",
        spec=spec.model_dump(),
    )
    db.add(agent)
    db.flush()
    deployment, job = create_deployment(db, agent)
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


@router.get("/agents")
def list_agents(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agents = (
        db.query(Agent)
        .filter(Agent.workspace_id == ws.id, Agent.status != "deleted")
        .order_by(Agent.created_at.desc())
        .all()
    )
    out = []
    for a in agents:
        row = _agent_out(a, _latest_deployment(db, a.id))
        # each (re)publish is one Deployment row — the count is the revision no.
        row["revision"] = (
            db.query(Deployment).filter(Deployment.agent_id == a.id).count()
        )
        out.append(row)
    return {"agents": out}


@router.get("/agents/discovery")
def discover_runtimes(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    control = control_client(ws.context)
    runtimes = scan_runtimes(control, db, workspace_id=ws.id)
    harnesses, harness_scan_error = scan_harnesses(control, db, workspace_id=ws.id)
    return {
        "region": ws.context.region,
        "runtimes": runtimes,
        "harnesses": harnesses,
        "harness_scan_error": harness_scan_error,
    }


@router.post("/agents/discovery/import")
def import_discovered_runtimes(
    req: RuntimeImportRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Import selected Runtimes and/or Harnesses; result rows are keyed by kind."""
    control = control_client(ws.context)
    result = import_runtimes(control, db, req.runtime_ids, workspace_id=ws.id)
    for bucket, rows in import_harnesses(
        control, db, req.harness_ids, workspace_id=ws.id
    ).items():
        result[bucket].extend(rows)
    db.commit()
    return result


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    deployments = (
        db.query(Deployment)
        .filter(Deployment.agent_id == agent_id)
        .order_by(Deployment.started_at.desc())
        .all()
    )
    out = _agent_out(agent)
    out["deployments"] = [_deployment_out(d) for d in deployments]
    return out


@router.get("/agents/{agent_id}/versions")
def get_agent_versions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Read-only AWS versions + endpoints of the agent's Runtime or Harness.

    Follows every list page; the projection is allow-listed (no environment,
    artifact, role or authorizer values). 409 ``agent.no_resource`` when the row
    has no AWS resource to ask about.
    """
    agent = _agent_in(db, ws, agent_id)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return list_agent_versions(control_client(ws.context), agent)


@router.post("/agents/{agent_id}/redeploy", status_code=202)
def redeploy_agent(
    agent_id: str,
    spec: AgentSpec,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Re-publish an agent in place with an edited spec.

    Runs the pipeline in "update" mode: the deploy stage calls UpdateHarness /
    UpdateAgentRuntime instead of Create, so AgentCore publishes a NEW VERSION
    on the SAME resource — the agentRuntimeId/harnessId and ARN are unchanged
    and the DEFAULT endpoint auto-rolls to the new version (near-zero downtime,
    versioned + rollback-able). package/provision still rebuild the artifact so
    edited code/requirements ship. If the agent has no live resource yet (e.g. a
    failed first deploy), the deploy stage falls back to Create.

    Name and method are immutable — changing either would be a different agent.
    """
    agent = _agent_in(db, ws, agent_id)
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    if agent.method == DISCOVERED_METHOD:
        raise AppError(
            "agent.redeploy_external",
            "discovered runtimes are externally owned and cannot be re-published",
            status_code=400,
        )
    if agent.status == "deploying":
        raise AppError(
            "agent.deploy_in_progress",
            "a deployment is already in progress for this agent",
            status_code=409,
        )
    if spec.name != agent.name or spec.method != agent.method:
        raise AppError(
            "agent.redeploy_immutable",
            "name and method cannot change on re-publish — clone to a new agent instead",
            {"name": agent.name, "method": agent.method},
            status_code=400,
        )

    agent.spec = spec.model_dump()
    agent.status = "deploying"
    agent.error = None
    agent.updated_at = datetime.now(UTC)
    db.flush()
    deployment, job = create_deployment(db, agent, mode="update")
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


@router.post("/agents/{agent_id}/convert", status_code=202)
def convert_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Convert a managed-harness agent into a NEW runtime-backed agent.

    Exports the harness code (agentcore CLI), grafts the launchpad config-
    bundle contract onto the entrypoint (so experiments can A/B it — the
    export alone would no-op, same as the harness), and deploys the result
    through the standard zip pipeline. The source harness is untouched.
    """
    from app.deployer.zip_runtime import platform_requirements
    from app.services import harness_convert as hc

    source = _agent_in(db, ws, agent_id)
    if source is None or source.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    if source.method != "harness" or source.status != "active":
        raise AppError(
            "agent.convert_unsupported",
            "conversion targets active managed-harness agents only",
            status_code=400,
        )
    in_flight = [
        a for a in db.query(Agent)
        .filter(Agent.workspace_id == ws.id, Agent.status == "deploying")
        .all()
        if (a.spec or {}).get("source_harness", {}).get("agent_id") == agent_id
    ]
    if in_flight:
        raise AppError(
            "agent.convert_in_flight",
            f"a conversion of this harness is already deploying ({in_flight[0].name})",
            status_code=409,
        )

    # {name}-rt, suffixed -2/-3… until free (never overwrite, R5)
    taken = {
        a.name
        for a in db.query(Agent)
        .filter(Agent.workspace_id == ws.id, Agent.status != "deleted")
        .all()
    }
    new_name = f"{source.name}-rt"[:48]
    counter = 2
    while new_name in taken:
        new_name = f"{source.name}-rt-{counter}"[:48]
        counter += 1

    # The FULL platform contribution for the spec about to be built, not just the
    # template base list: it is both the dedupe set and the graph resolve_pins
    # resolves against, so an omission here produces pins the package stage
    # cannot lock (mcp==2.0.0 vs strands-agents' mcp<2.0.0).
    platform = platform_requirements(*hc.conversion_platform_inputs(source))
    try:
        files = hc.export_harness(source.arn)
        spec = hc.build_conversion_spec(
            source, files, platform, new_name, ws.context
        )
    except hc.ConversionError as exc:
        raise AppError("agent.convert_failed", str(exc), status_code=502) from exc

    agent = Agent(
        workspace_id=ws.id, name=spec.name, method=spec.method, status="deploying",
        spec=spec.model_dump(),
    )
    db.add(agent)
    db.flush()
    deployment, job = create_deployment(db, agent)
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


@router.post("/agents/{agent_id}/invoke", response_model=InvokeResponse)
def invoke_agent(
    agent_id: str,
    req: InvokeRequest,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> InvokeResponse:
    agent = _agent_in(db, ws, agent_id)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    started = time.monotonic()
    result = invoke_agent_text(
        agent, req.prompt, session_id=req.session_id,
        actor_id=scoped_actor(agent.id, req.actor_id),
        workspace=ws.context,
    )
    return InvokeResponse(
        text=result["text"],
        session_id=result["session_id"],
        latency_ms=int((time.monotonic() - started) * 1000),
    )


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    aws_resource_deleted = _delete_agent_resources(agent, ws.context)
    agent.status = "deleted"
    agent.updated_at = datetime.now(UTC)
    db.commit()
    return {
        "deleted": True,
        "agent_id": agent_id,
        "aws_resource_deleted": aws_resource_deleted,
    }


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None or job.workspace_id != ws.id:
        raise NotFoundError("job.not_found", "job not found")
    events = [json.loads(line) for line in job.log.splitlines() if line.strip()]
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "payload": job.payload,
        "error": job.error,
        "events": events,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }
