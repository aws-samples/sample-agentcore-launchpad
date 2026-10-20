"""Agents API — create/deploy, list, invoke, delete; jobs polling; BYOC uploads."""

import hashlib
import json
import logging
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.assistant import purge as assistant_purge
from app.core.billing_policy import SOURCE_INVOKE
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.deployer import byoc as byoc_method
from app.deployer import container as container_method
from app.deployer import harness as harness_method
from app.deployer import zip_runtime as zip_method
from app.deployer.pipeline import create_deployment, start_deploy_async
from app.models.ledger import Agent, Deployment, Job
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import (
    AgentSpec,
    ByocPythonVersion,
    InvokeRequest,
    InvokeResponse,
    RuntimeImportRequest,
)
from app.schemas.inbound_auth import JWT_CAPABLE_METHODS, InboundAuth
from app.services import (
    agent_access,
    agent_iam,
    agent_identity,
    agent_names,
    agent_source,
    budgets,
    byoc_uploads,
    context_layer,
    creators,
    identity_providers,
    memory_ownership,
    oauth_sessions,
    registry_console,
    snapshots,
)
from app.services import inbound_auth as inbound_auth_service
from app.services.agent_versions import list_agent_versions
from app.services.agentcore import harness as harness_api
from app.services.agentcore.client import control_client
from app.services.attachments import attachment_capability, prepare_attachments
from app.services.creators import Creator
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
from app.services.usage_meter import UsageMeter
from app.services.visibility import Viewer, require_viewer
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.system_agents import service as system_agents
from app.system_agents.presets import is_reserved_name
from app.templates import context_layer_support

logger = logging.getLogger("launchpad.agents")

router = APIRouter(prefix="/api", tags=["agents"])

SUPPORTED_METHODS = {"harness", "zip_runtime", "container", "studio", "byoc"}
BYOC_PYTHON_VERSIONS = set(get_args(ByocPythonVersion))


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
        # the creating principal (NULL for discovered / preset / legacy rows)
        **creators.fields(agent),
        "error": agent.error,
        "spec": agent.spec,
        # Server-owned; None for every ordinary agent. The console renders the
        # SYSTEM chip and disables the protected actions from this, never from spec.
        "system": system_agents.system_projection(agent),
        "experiment_capability": experiment_capability(agent),
        "canary_capability": canary_capability(agent),
        "invoke_capability": invoke_capability(agent),
        "attachment_capability": attachment_capability(agent),
        # Inbound auth of the LIVE runtime (deploy-time snapshot; discovered
        # imports project their scanned authorizer_type). NULL snapshot on a
        # launchpad row = deployed before the feature, i.e. IAM.
        "inbound_auth_mode": inbound_auth_service.display_mode(agent),
        "inbound_auth_config": agent.inbound_auth_config,
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


def _agent_in(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent | None:
    """The agent, but only if it lives in this workspace and ``viewer`` may see it
    (workspace isolation). Deleted rows are returned; callers decide.

    A foreign or hidden id is indistinguishable from a missing one on purpose: the
    caller learns nothing about other workspaces' (or members') agents.
    """
    return agent_access.agent_in(db, ws.id, agent_id, viewer=viewer, include_deleted=True)


def _holder_id(db: Session, viewer: Viewer, holder: Agent) -> str | None:
    """The live name holder's id for the 409 detail — only when the caller may see it."""
    return holder.id if agent_access.agent_visible(db, viewer, holder) else None


def _latest_deployment(db: Session, agent_id: str) -> Deployment | None:
    return (
        db.query(Deployment)
        .filter(Deployment.agent_id == agent_id)
        .order_by(Deployment.started_at.desc())
        .first()
    )


def _delete_agent_resources(agent: Agent, workspace: WorkspaceContext) -> bool:
    """Tear down the method-specific AWS resource for an agent (idempotent)."""
    if not delete_agent_cloud_resource(agent, workspace):
        return False
    # After the resource, never before: deleting the execution role while the
    # runtime still references it can wedge the runtime's own deletion. A failed
    # role delete must not block deleting the agent, so this returns rather than
    # raises and logs the role name for a later sweep.
    delete_agent_role(agent, workspace)
    delete_agent_registry_record(agent, workspace)
    return True


def delete_agent_cloud_resource(agent: Agent, workspace: WorkspaceContext) -> bool:
    """Request deletion of the method-specific AWS resource only (no role, no ledger);
    ``False`` for a discovered runtime, which is never ours to delete. Deletion is
    asynchronous: the conversation purge reads it back before the role goes."""
    if agent.method == DISCOVERED_METHOD:
        return False
    if agent.method == "harness":
        harness_method.delete_agent_resources(agent, workspace)
    elif agent.method in ("zip_runtime", "studio"):
        zip_method.delete_agent_resources(agent, workspace)
    elif agent.method == "container":
        container_method.delete_agent_resources(agent, workspace)
    elif agent.method == "byoc":
        byoc_method.delete_agent_resources(agent, workspace)
    return True


def delete_agent_registry_record(agent: Agent, workspace: WorkspaceContext) -> bool:
    """Best-effort removal of the A2A record the deploy ``register`` stage created for
    this agent (a record the id no longer names as this agent's stays); never raises —
    ``False`` logs the record id for a later sweep."""
    try:
        registry_console.delete_agent_record(agent, workspace)
        return True
    except Exception as exc:  # noqa: BLE001 — a registry hiccup must not block the delete
        logger.warning("agent %s: could not delete registry record %s: %s", agent.id,
                       agent.registry_record_id, f"{type(exc).__name__}: {exc}")
        return False


def delete_agent_role(agent: Agent, workspace: WorkspaceContext) -> bool:
    """Delete the agent's own execution role (the shared role is never touched);
    never raises — ``False`` leaves the role findable for a later sweep."""
    return agent_iam.delete_execution_role(
        agent,
        get_settings(),
        workspace,
        lambda msg: logger.info("agent %s: %s", agent.id, msg),
    )


def _validate_tool_auth(spec: AgentSpec, ws: WorkspaceScope) -> None:
    """Every tool auth block names a live Connection of the declared kind.

    Only reaches AWS when some tool carries auth, so an ordinary deploy's
    request path is unchanged. A spec attaching the COA context layer needs the
    workspace to have one connected (``context_layer.not_connected``)."""
    if any(tool.auth is not None for tool in spec.tools):
        identity_providers.validate_tool_auth(control_client(ws.context), spec)
    if spec.context_layer is not None:
        context_layer.require_connected(ws.id)
    if spec.inbound_auth is not None:
        # A spec-pinned JWT config gets the same save-time discovery probe as
        # the workspace default, so a bad URL fails the request, not the deploy.
        inbound_auth_service.validate_inbound_auth(spec.inbound_auth)


@router.post("/agents", status_code=202)
def create_agent(
    spec: AgentSpec,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    return start_create(db, ws, spec, creator=Creator.of_request(request), viewer=viewer)


def start_create(
    db: Session, ws: WorkspaceScope, spec: AgentSpec, *, creator: Creator | None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """The create guards + a deploy job for a new agent stamped with ``creator`` —
    shared by ``POST /api/agents`` and the coding agent's deploy handoff
    (``app/coding/deploy.py``), so a project deployed from the coding console is an
    ordinary governed Agent by construction. ``viewer`` decides which KB / memory
    pins the caller may use and whether a name holder's id is disclosed."""
    if spec.method not in SUPPORTED_METHODS:
        raise AppError(
            "agent.method_not_available",
            f"method '{spec.method}' ships in a later phase",
            {"supported": sorted(SUPPORTED_METHODS)},
            status_code=400,
        )
    if is_reserved_name(spec.name):
        # Reserved for a system-managed preset: an ordinary agent can never hold it,
        # so a later install cannot be confused with (or adopt) a member's agent.
        raise AppError(
            "agent.name_reserved",
            f"'{spec.name}' is reserved for a system-managed preset",
            {"name": spec.name},
            status_code=409,
        )
    # Names are unique per workspace, not per ledger: two environments own their
    # own AgentCore resource namespaces.
    existing = agent_names.live_holder(db, ws.id, spec.name)
    if existing:
        raise agent_names.name_exists_error(spec.name, _holder_id(db, viewer, existing))
    # every KB / memory the spec pins must be visible to the caller (isolation)
    agent_access.check_pins(db, viewer, ws.context, spec.model_dump())
    _validate_tool_auth(spec, ws)
    # a pinned memory must be one this workspace manages and ACTIVE (issue #55);
    # the deploy job re-checks before any stage, this answers before any row
    memory_ownership.require_spec_memory(db, ws.context, spec.model_dump())
    agent = Agent(
        workspace_id=ws.id,
        name=spec.name,
        method=spec.method,
        status="deploying",
        spec=spec.model_dump(),
    )
    creators.stamp(agent, creator)
    db.add(agent)
    db.flush()
    # Atomic reservation shared with the assistant approval path: a concurrent
    # creator of the same name loses here with the same 409, before any job.
    agent_names.claim_agent_name(db, ws.id, spec.name, agent.id, viewer=viewer)
    deployment, job = create_deployment(db, agent)
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


@router.get("/agents")
def list_agents(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agents = (
        db.query(Agent)
        .filter(
            Agent.workspace_id == ws.id,
            Agent.status != "deleted",
            agent_access.agent_clause(viewer),
        )
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    control = control_client(ws.context)
    runtimes = scan_runtimes(control, db, workspace_id=ws.id)
    harnesses, harness_scan_error = scan_harnesses(control, db, workspace_id=ws.id)
    hidden = agent_access.hidden_agent_ids(db, viewer, ws.id)
    if hidden:
        for candidate in [*runtimes, *harnesses]:
            _mask_managed(candidate, hidden)
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Import selected Runtimes and/or Harnesses; result rows are keyed by kind."""
    control = control_client(ws.context)
    result = import_runtimes(control, db, req.runtime_ids, workspace_id=ws.id)
    for bucket, rows in import_harnesses(
        control, db, req.harness_ids, workspace_id=ws.id
    ).items():
        result[bucket].extend(rows)
    db.commit()
    hidden = agent_access.hidden_agent_ids(db, viewer, ws.id)
    if hidden:
        for rows in result.values():
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    _mask_managed(row, hidden)
    return result


def _mask_managed(row: dict[str, Any], hidden: set[str]) -> None:
    """Under isolation a runtime managed by an agent the caller cannot see stays
    marked managed (``managed: true``; it is not importable either way) but names
    no owner — scan candidates (``managed_agent_*``) and import results
    (``already_managed`` → ``agent_id`` / ``agent_name``) alike."""
    for id_key, name_key in (("managed_agent_id", "managed_agent_name"),
                             ("agent_id", "agent_name")):
        if row.get(id_key) and row[id_key] in hidden:
            row[id_key] = None
            row[name_key] = None
            row["managed"] = True
            if "managed_agent_method" in row:
                row["managed_agent_method"] = None
            if "importable" in row:  # a scan candidate: still not importable
                row["importable"] = False
                row["reason_code"] = "already-managed"
                row["reason"] = "This resource is already managed by a Launchpad agent."


@router.post("/agents/uploads", status_code=201)
async def upload_byoc_artifact(
    request: Request,
    python_version: str = "PYTHON_3_13",
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Stage a BYOC source zip (multipart, single part ``file``, .zip only).

    Streams to a temp file (250 MiB cap enforced mid-stream — the Content-Length
    guard in ``byoc_uploads.upload_body_limit_middleware`` already refused
    known-oversize bodies before the parser ran), validates the archive without
    executing anything in it, stores zip + manifest to the artifacts bucket under
    ``byoc/{workspace_id}/{upload_id}/`` and returns the detection summary —
    including a dry resolve of the zip's requirements.txt against the deploy
    target for ``python_version``, so the wizard can flag an unresolvable file
    before deploy. Staging runs in the threadpool: the resolve may take tens of
    seconds and must not stall the event loop.
    """
    identity = require_identity(request)
    if python_version not in BYOC_PYTHON_VERSIONS:
        raise AppError(
            "byoc.invalid_python_version",
            f"python_version must be one of {sorted(BYOC_PYTHON_VERSIONS)}",
            status_code=422,
        )
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise AppError("byoc.invalid_upload", "expected a .zip part named 'file'",
                       status_code=400)
    filename = Path(upload.filename or "").name
    if not filename.lower().endswith(".zip"):
        raise AppError("byoc.invalid_upload", "expected a .zip file", status_code=400)

    digest = hashlib.sha256()
    size = 0
    with tempfile.TemporaryDirectory(prefix="byoc-upload-") as tmp:
        tmp_zip = Path(tmp) / "source.zip"
        with tmp_zip.open("wb") as target:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > byoc_uploads.MAX_ZIP_BYTES:
                    raise AppError(
                        "byoc.upload_too_large",
                        "BYOC upload exceeds the 250 MiB zip limit",
                        status_code=413,
                    )
                digest.update(chunk)
                target.write(chunk)
        if size == 0:
            raise AppError("byoc.invalid_upload", "the uploaded file is empty",
                           status_code=400)
        manifest = await run_in_threadpool(
            byoc_uploads.stage_upload,
            ws.context,
            filename=filename,
            tmp_zip=tmp_zip,
            sha256=digest.hexdigest(),
            size_bytes=size,
            uploaded_by=identity.username,
            uploaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            python_version=python_version,
        )
    logger.info(
        "byoc upload %s staged by %s (%s, %d bytes, sha256 %s)",
        manifest["upload_id"], identity.username, filename, size, manifest["sha256"][:12],
    )
    return manifest


@router.get("/agents/uploads/{upload_id}")
def get_byoc_upload(
    upload_id: str,
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The stored detection summary + provenance for one staged BYOC upload."""
    return byoc_uploads.get_manifest(ws.context, upload_id)


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id, viewer)
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


@router.get("/agents/{agent_id}/conversions")
def list_agent_conversions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The runtime twins converted from this agent, newest first.

    A conversion stamps ``spec.source_harness.agent_id`` on the new ``-rt`` agent
    and never touches the source, so the ledger already holds the relation; this
    read projects it (with each twin's latest deployment) for the assistant's
    NEXT STEPS, which switches to the twin once it is active and needs to find it
    again after a reload — or when the operator converted from the Agents page.
    Pure ledger read; nothing on AWS is called.
    """
    source = _agent_in(db, ws, agent_id, viewer)
    if source is None or source.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    twins = [
        a
        for a in db.query(Agent)
        .filter(
            Agent.workspace_id == ws.id,
            Agent.status != "deleted",
            agent_access.agent_clause(viewer),
        )
        .all()
        if ((a.spec or {}).get("source_harness") or {}).get("agent_id") == agent_id
    ]
    twins.sort(key=lambda a: a.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return {
        "source": {
            "id": source.id,
            "name": source.name,
            "method": source.method,
            "status": source.status,
        },
        "conversions": [_agent_out(a, _latest_deployment(db, a.id)) for a in twins],
    }


@router.get("/agents/{agent_id}/versions")
def get_agent_versions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Read-only AWS versions + endpoints of the agent's Runtime or Harness.

    Follows every list page; the projection is allow-listed (no environment,
    artifact, role or authorizer values). 409 ``agent.no_resource`` when the row
    has no AWS resource to ask about.
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return list_agent_versions(control_client(ws.context), agent)


@router.get("/agents/{agent_id}/source")
def get_agent_source(
    agent_id: str,
    path: str | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Read-only CODE view: the code AWS runs (or a Harness's configuration).

    ``path`` reads one listed file the response budget left out. 409
    ``agent.source_unavailable`` when the platform holds no code for the agent
    (see ``services/agent_source``)."""
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    latest = _latest_deployment(db, agent.id)
    return agent_source.agent_source(
        control_client(ws.context),
        ws.context.client("s3"),
        ws.context,
        agent,
        path=path,
        deployment_status=latest.status if latest else None,
    )


@router.get("/agents/{agent_id}/identity")
def get_agent_identity(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Read-only identity view: workload identity, inbound mode, and every
    downstream with its acting mode and Connection (see docs/identity.md §5)."""
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    return agent_identity.agent_identity(
        control_client(ws.context), agent, ws.context.resources or {}
    )


@router.post("/agents/{agent_id}/redeploy", status_code=202)
def redeploy_agent(
    agent_id: str,
    spec: AgentSpec,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
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
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    return republish_agent(
        db, agent, spec, ws, actor=Creator.of_request(request), viewer=viewer
    )


def republish_agent(
    db: Session, agent: Agent, spec: AgentSpec, ws: WorkspaceScope,
    *, actor: Creator | None = None, note: str | None = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """The redeploy guards + an update-mode deploy job for an edited spec — shared
    by the redeploy route, accepting an AI recommendation (a new Harness version) and
    snapshot rollback, so a rollback is by construction an ordinary redeploy that
    carries an older spec. ``actor`` / ``note`` land on the Job payload and on the
    spec snapshot the publish writes. ``viewer`` only decides which *newly added* KB /
    memory pins the caller may use; the caller has already checked the agent itself
    is visible."""
    _refuse_redeploy(agent)
    if spec.name != agent.name or spec.method != agent.method:
        raise AppError(
            "agent.redeploy_immutable",
            "name and method cannot change on re-publish — clone to a new agent instead",
            {"name": agent.name, "method": agent.method},
            status_code=400,
        )
    # A converted agent's exported code bakes its system prompt and model: an edit of
    # either would be silently ignored, and a model change would re-scope the execution
    # role away from the model the code actually calls.
    stored = agent.spec or {}
    if stored.get("code_bundle") and spec.code_bundle:
        changed = [
            field for field in ("model_id", "system_prompt")
            if getattr(spec, field) != stored.get(field)
        ]
        if changed:
            raise AppError(
                "agent.converted_locked",
                "a converted agent's system prompt and model are baked into its exported "
                "code and cannot change on re-publish",
                {"fields": changed},
                status_code=400,
            )

    return _start_redeploy(db, ws, agent, spec, actor=actor, note=note, viewer=viewer)


def _redeployable(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent:
    """The agent row a re-publish may update in place, or the refusal."""
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    _refuse_redeploy(agent)
    return agent


def _refuse_redeploy(agent: Agent) -> None:
    """Refuse a re-publish of a system preset, a discovered runtime or a busy agent."""
    system_agents.refuse_system_mutation(agent, "redeploy")
    assistant_purge.refuse_while_clearing(agent.id, "redeploy")
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


def _start_redeploy(
    db: Session, ws: WorkspaceScope, agent: Agent, spec: AgentSpec,
    *, actor: Creator | None = None, note: str | None = None,
    viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    # only pins ADDED relative to the stored spec must be visible: an agent pinned
    # while the workspace was shared keeps re-publishing after a switch to isolated
    agent_access.check_pins(
        db, viewer, context_for_workspace(agent.workspace_id), spec.model_dump(),
        previous_spec=agent.spec or {},
    )
    _validate_tool_auth(spec, ws)
    # a pinned memory must be one this workspace manages and ACTIVE (issue #55) —
    # refused before the stored spec changes, so a refusal leaves the agent as is
    memory_ownership.require_spec_memory(
        db, context_for_workspace(agent.workspace_id), spec.model_dump()
    )
    agent.spec = spec.model_dump()
    agent.status = "deploying"
    agent.error = None
    agent.updated_at = datetime.now(UTC)
    db.flush()
    deployment, job = create_deployment(db, agent, mode="update", actor=actor, note=note)
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


# ── spec snapshots: one per publish, field-level diff, rollback ──────────────────


def _snapshot_agent(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer
) -> Agent:
    """The agent whose snapshots are read — a deleted agent's history stays readable;
    a foreign or hidden one is ``agent.not_found``."""
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent


@router.get("/agents/{agent_id}/snapshots")
def list_agent_snapshots(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The agent's spec snapshots, newest first — without specs (fetch one for it)."""
    agent = _snapshot_agent(db, ws, agent_id, viewer)
    rows = snapshots.list_snapshots(db, agent.id)
    statuses = snapshots.deployment_statuses(db, rows)
    return {
        "snapshots": [
            snapshots.snapshot_out(snap, deployment_status=statuses.get(snap.deployment_id or ""))
            for snap in rows
        ]
    }


# Declared before `/snapshots/{seq}`, so "diff" is never parsed as a sequence number.
@router.get("/agents/{agent_id}/snapshots/diff")
def diff_agent_snapshots(
    agent_id: str,
    from_seq: int | None = None,
    to_seq: int | None = None,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Field-level difference between two snapshots' specs. An omitted side is the
    agent's **current** spec (``from`` / ``to`` read null); omitting both is 422."""
    agent = _snapshot_agent(db, ws, agent_id, viewer)
    if from_seq is None and to_seq is None:
        raise AppError(
            "snapshot.diff_side_required",
            "name at least one snapshot (from_seq or to_seq); an omitted side is the "
            "current spec",
            status_code=422,
        )
    before = snapshots.get_snapshot(db, agent.id, from_seq) if from_seq is not None else None
    after = snapshots.get_snapshot(db, agent.id, to_seq) if to_seq is not None else None
    current = agent.spec or {}
    statuses = snapshots.deployment_statuses(db, [s for s in (before, after) if s is not None])

    def side(snap: Any) -> dict[str, Any] | None:
        if snap is None:
            return None
        return snapshots.snapshot_out(
            snap, deployment_status=statuses.get(snap.deployment_id or "")
        )

    return {
        "from": side(before),
        "to": side(after),
        "changes": snapshots.diff_specs(
            before.spec if before is not None else current,
            after.spec if after is not None else current,
        ),
    }


@router.get("/agents/{agent_id}/snapshots/{seq}")
def get_agent_snapshot(
    agent_id: str,
    seq: int,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """One snapshot, with its full spec."""
    agent = _snapshot_agent(db, ws, agent_id, viewer)
    snap = snapshots.get_snapshot(db, agent.id, seq)
    statuses = snapshots.deployment_statuses(db, [snap])
    return snapshots.snapshot_out(
        snap, deployment_status=statuses.get(snap.deployment_id or ""), with_spec=True
    )


@router.post("/agents/{agent_id}/snapshots/{seq}/rollback", status_code=202)
def rollback_agent_snapshot(
    agent_id: str,
    seq: int,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Re-publish a stored spec as a NEW publish (never an AWS-side version revert).

    Goes through `republish_agent`, so every redeploy guard applies unchanged (system
    preset, discovered runtime, a deploy in flight, an assistant purge, name / method
    change, a converted agent's baked prompt and model, added KB / memory pins, tool
    auth). Refused first, before any AWS deploy call: a snapshot that never deployed,
    a spec that no longer validates, and a knowledge base it would re-add that is gone.
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    system_agents.refuse_system_mutation(agent, "rollback")
    _refuse_redeploy(agent)
    snap = snapshots.get_snapshot(db, agent.id, seq)
    if not snap.aws_version:
        raise AppError(
            "snapshot.never_deployed",
            f"snapshot #{seq} never deployed successfully, so there is nothing known-good "
            "to roll back to",
            {"seq": seq},
            status_code=409,
        )
    try:
        spec = AgentSpec.model_validate(snap.spec or {})
    except ValidationError as exc:
        raise AppError(
            "snapshot.spec_invalid",
            f"snapshot #{seq}'s spec no longer validates against the current schema",
            {"seq": seq, "errors": len(exc.errors())},
            status_code=409,
        ) from exc
    stale = snapshots.stale_kb_ids(ws.context, snap.spec, agent.spec)
    if stale:
        raise AppError(
            "snapshot.stale_reference",
            f"snapshot #{seq} mounts knowledge base(s) that no longer exist",
            {"seq": seq, "kb_ids": stale},
            status_code=409,
        )
    return republish_agent(
        db, agent, spec, ws,
        actor=Creator.of_request(request), note=f"rollback to #{seq}", viewer=viewer,
    )


class InboundAuthSwitch(BaseModel):
    """``inbound_auth: null`` drops the pin, so the agent inherits the workspace default.

    The key is required and unknown keys are refused: every accepted body starts a
    full redeploy, so a mistyped one (``{}``, or the config sent at the top level)
    must fail with a 422, not quietly re-publish the agent unpinned.
    """

    model_config = ConfigDict(extra="forbid")

    inbound_auth: InboundAuth | None


@router.post("/agents/{agent_id}/inbound-auth", status_code=202)
def switch_inbound_auth(
    agent_id: str,
    body: InboundAuthSwitch,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Switch the agent's inbound auth IN PLACE: pin (or unpin) ``inbound_auth``
    on the stored spec and re-publish it — UpdateAgentRuntime on the same
    runtime, a new version, the DEFAULT endpoint rolls. Every other spec field
    is re-published unchanged, so this is the wizard's re-publish minus the form.
    """
    agent = _redeployable(db, ws, agent_id, viewer)
    stored = agent.spec or {}
    if body.inbound_auth is not None and body.inbound_auth.mode == "jwt" and (
        agent.method not in JWT_CAPABLE_METHODS or stored.get("protocol") == "a2a"
    ):
        raise AppError(
            "agent.inbound_auth_unsupported",
            "only HTTP Runtime agents (zip / studio / container / byoc) can carry a "
            "JWT authorizer",
            {"method": agent.method},
            status_code=422,
        )
    try:
        spec = AgentSpec(**{**stored, "inbound_auth": body.inbound_auth})
    except ValidationError as exc:
        raise AppError(
            "agent.inbound_auth_invalid",
            "the stored spec cannot be re-published with this inbound auth",
            {"errors": exc.errors(
                include_url=False, include_context=False, include_input=False
            )},
            status_code=422,
        ) from exc
    return _start_redeploy(
        db, ws, agent, spec, actor=Creator.of_request(request),
        note="inbound auth switched", viewer=viewer,
    )


@router.post("/agents/{agent_id}/convert", status_code=202)
def convert_agent(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Convert a managed-harness agent into a NEW runtime-backed agent.

    Exports the harness code (agentcore CLI), grafts the launchpad config-
    bundle contract onto the entrypoint (so experiments can A/B it — the
    export alone would no-op, same as the harness), and deploys the result
    through the standard zip pipeline. The source harness is untouched.
    """
    from app.deployer.zip_runtime import platform_requirements
    from app.services import harness_convert as hc

    source = _agent_in(db, ws, agent_id, viewer)
    if source is None or source.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    system_agents.refuse_system_mutation(source, "convert")
    assistant_purge.refuse_while_clearing(source.id, "convert")
    if context_layer_support.attached(source.spec):
        # the exported code carries no COA tool, and a code_bundle spec cannot attach one
        raise AppError(
            "context_layer.unsupported_method",
            "an agent with a COA context layer cannot be converted to a runtime — the "
            "exported code would lose the context layer",
            {"reason": "conversion"},
            status_code=422,
        )
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
        # the mutex is workspace-wide; the message names the twin only when visible
        twin = in_flight[0]
        label = f" ({twin.name})" if agent_access.agent_visible(db, viewer, twin) else ""
        raise AppError(
            "agent.convert_in_flight",
            f"a conversion of this harness is already deploying{label}",
            status_code=409,
        )
    # the twin inherits the source's KB / memory pins — refuse before the export
    agent_access.check_pins(db, viewer, ws.context, source.spec or {})

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
        # The live Harness is the source of the Gateway clients the export drops.
        harness = (
            harness_api.get_harness(control_client(ws.context), source.resource_id)
            if any(
                isinstance(ref, dict) and ref.get("type") == "gateway"
                for ref in (source.spec or {}).get("tools") or []
            )
            else None
        )
        spec = hc.build_conversion_spec(
            source, files, platform, new_name, ws.context, harness=harness
        )
    except hc.ConversionError as exc:
        raise AppError("agent.convert_failed", str(exc), status_code=502) from exc
    # the twin inherits the source's KB / memory pins: the converter must see them
    agent_access.check_pins(db, viewer, ws.context, spec.model_dump())
    # the twin inherits the source's memory pin — same ownership rule (issue #55)
    memory_ownership.require_spec_memory(db, ws.context, spec.model_dump())

    agent = Agent(
        workspace_id=ws.id, name=spec.name, method=spec.method, status="deploying",
        spec=spec.model_dump(),
    )
    # the twin is a new agent: its creator is the converter, not the source's
    creators.stamp(agent, Creator.of_request(request))
    db.add(agent)
    db.flush()
    agent_names.claim_agent_name(db, ws.id, spec.name, agent.id, viewer=viewer)
    deployment, job = create_deployment(db, agent)
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


@router.post("/agents/{agent_id}/invoke", response_model=InvokeResponse)
def invoke_agent(
    agent_id: str,
    req: InvokeRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> InvokeResponse:
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    started = time.monotonic()
    extra: dict[str, Any] = {"attachments": prepared} if prepared else {}
    if context_layer_support.attached(agent.spec):
        # a COA context layer acts as the signed-in member, never as a client-chosen actor
        extra["runtime_user_id"] = require_identity(request).username
    # charged to the caller (the route-level billing gate checked the budget)
    meter = UsageMeter.for_agent(
        budgets.payer_of_request(request), SOURCE_INVOKE, agent, workspace_id=ws.id,
        session_id=req.session_id,
    )
    result: dict[str, Any] | None = None
    try:
        result = invoke_agent_text(
            agent, req.prompt, session_id=req.session_id,
            actor_id=scoped_actor(agent.id, req.actor_id),
            workspace=ws.context,
            on_usage=meter.on_usage,
            **extra,
        )
    finally:
        meter.settle(failed=result is None,
                     session_id=result.get("session_id") if result else None)
    coa_asks = [
        ask for ask in result.get("auth_required") or []
        if ask.get("tool") in (context_layer_support.GRANT_TOOL, context_layer_support.TOOL_NAME)
    ]
    if coa_asks and context_layer_support.attached(agent.spec):
        # this response has no consent card: name the missing consent instead of
        # answering with the empty text of a turn that never reached COA
        raise AppError(
            "context_layer.authorization_required",
            "authorize Launchpad to act as you in COA first (from Chat or the Context "
            "Layer page), then retry",
            {
                "authorization_url": coa_asks[0].get("url"),
                "connection": coa_asks[0].get("provider"),
                "agent_id": agent.id,
            },
            status_code=409,
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
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    # the conversation purge owns this agent's teardown while it runs
    assistant_purge.refuse_while_clearing(agent.id, "delete")
    aws_resource_deleted = delete_agent_row(db, agent, ws.context)
    return {
        "deleted": True,
        "agent_id": agent_id,
        "aws_resource_deleted": aws_resource_deleted,
    }


def delete_agent_row(db: Session, agent: Agent, workspace: WorkspaceContext) -> bool:
    """The one agent teardown: refuse a protected preset BEFORE any AWS call, delete the
    method-specific resource + execution role, mark the ledger row deleted and release
    its name claim. Shared by the route above and the architect assistant's
    conversation purge, so both delete an agent the same way."""
    # Before the AWS teardown: a refused delete must leave the harness untouched.
    system_agents.refuse_system_mutation(agent, "delete")
    aws_resource_deleted = _delete_agent_resources(agent, workspace)
    mark_agent_deleted(db, agent)
    return aws_resource_deleted


def mark_agent_deleted(db: Session, agent: Agent) -> None:
    """The ledger half of an agent delete: forget its workload identity (and every 3LO
    token keyed by it, which went with the runtime), mark it deleted, release its name."""
    oauth_sessions.forget_agent(db, agent.workspace_id, agent.id)
    agent.status = "deleted"
    agent.updated_at = datetime.now(UTC)
    agent_names.release_agent_name(db, agent.workspace_id, agent.name, agent.id)
    db.commit()


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    job = db.get(Job, job_id)
    if job is None or job.workspace_id != ws.id:
        raise NotFoundError("job.not_found", "job not found")
    # a deploy job of an agent hidden by workspace isolation reads as missing
    payload = job.payload if isinstance(job.payload, dict) else {}
    job_agent_id = payload.get("agent_id") if viewer.filters else None
    if job_agent_id and job_agent_id in agent_access.hidden_agent_ids(db, viewer, ws.id):
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
