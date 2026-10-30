"""Agents API — create/deploy, list, invoke, delete; jobs polling; BYOC uploads."""

import hashlib
import json
import logging
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.assistant import purge as assistant_purge
from app.core.config import get_settings
from app.core.db import SQLITE_MAX_INT, get_db
from app.core.errors import AppError, NotFoundError
from app.deployer import byoc as byoc_method
from app.deployer import teardown
from app.deployer import zip_runtime as zip_method
from app.deployer.pipeline import (
    create_deployment,
    refuse_deploy_in_progress,
    start_deploy_async,
)
from app.models.ledger import Agent, Deployment, Job
from app.routers.auth import Identity, require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import (
    MODEL_NOT_VISIBLE_CODE,
    AgentSpec,
    AgentSpecRequest,
    ByocPythonVersion,
    InvokeRequest,
    InvokeResponse,
    ModelRefusal,
    RuntimeImportRequest,
    assert_spec_models_available,
    catalog_version,
    model_descriptor,
    principal_of_identity,
)
from app.services import (
    agent_access,
    agent_identity,
    agent_names,
    byoc_uploads,
    canary_references,
    creators,
    identity_providers,
    memory_ownership,
    method_validation,
    oauth_sessions,
    ownership,
    policy_enforcement,
    principal_mint,
    snapshots,
    trace_status,
    visibility,
)
from app.services import inbound_auth as inbound_auth_service
from app.services.agent_versions import list_agent_versions
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.attachments import attachment_capability, prepare_attachments
from app.services.creators import Creator
from app.services.invoke import invoke_agent_text, validate_session_id
from app.services.memory_backend import scoped_actor
from app.services.policy_entities import Principal
from app.services.runtime_discovery import (
    DISCOVERED_METHOD,
    import_harnesses,
    import_runtimes,
    invoke_capability,
    require_invoke_capability,
    scan_harnesses,
    scan_runtimes,
)
from app.services.visibility import Viewer, require_viewer
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.system_agents import service as system_agents
from app.system_agents.presets import refuse_reserved_name

logger = logging.getLogger("launchpad.agents")

router = APIRouter(prefix="/api", tags=["agents"])

SUPPORTED_METHODS = {"harness", "zip_runtime", "studio", "byoc"}
BYOC_PYTHON_VERSIONS = set(get_args(ByocPythonVersion))


def _agent_out(agent: Agent, deployment: Deployment | None = None) -> dict[str, Any]:
    # N12-bug2: the Global baseline emitted the verdict; the CN view dropped it and the
    # canary Start page crashed on `.eligible`. Resolved here (not at import) exactly as
    # the baseline did, and through the SAME function the canary router refuses with, so
    # the view and the router always agree. (`experiment_capability` retired, N13-obs2.)
    from app.optimization.capability import canary_capability

    out = {
        "id": agent.id,
        "name": agent.name,
        "method": agent.method,
        "status": agent.status,
        "arn": agent.arn,
        "resource_id": agent.resource_id,
        "registry_record_id": agent.registry_record_id,
        "version": agent.version,
        # the creating console principal (`services.creators`; upstream #263 shape):
        # `created_by` = users.id, `created_by_name` = username at creation; both NULL
        # for presets, discovered runtimes and rows with no resolvable legacy owner
        **creators.fields(agent),
        "error": agent.error,
        # N10v3-bug7: an active agent whose LATER deployment failed keeps serving its
        # live version; the failure travels here (the pipeline writes `Agent.error` on
        # an active row only in that case, and a new attempt clears it).
        "last_deploy_failed": agent.status == "active" and bool(agent.error),
        "last_deploy_error": agent.error if agent.status == "active" and agent.error else None,
        # N17-obs31: a forced KB delete stripped a mount this agent's live Runtime still carries
        # as a retrieval tool — "re-publish to drop the deleted KB"; cleared by the next publish
        "kb_stale": bool(agent.last_strip),
        "last_strip": agent.last_strip,
        "spec": agent.spec,
        # Server-owned; None for every ordinary agent. The console renders the
        # SYSTEM chip and disables the protected actions from this, never from spec.
        "system": system_agents.system_projection(agent),
        "canary_capability": canary_capability(agent),
        "invoke_capability": invoke_capability(agent),
        "attachment_capability": attachment_capability(agent),
        # Inbound auth of the LIVE runtime (deploy-time snapshot; discovered
        # imports project their scanned authorizer_type). NULL snapshot on a
        # launchpad row = deployed before the feature, i.e. IAM.
        "inbound_auth_mode": inbound_auth_service.display_mode(agent),
        # Always None: this build is AWS_IAM-only (no `authorizerConfiguration`
        # to echo). The raw column can still hold a legacy pre-N3 {"mode":
        # "jwt", ...} blob on an old row — never surface that stale shape
        # beside an `inbound_auth_mode` that always reads "iam" now.
        "inbound_auth_config": None,
        # Global parity (2026-10-27): `policy_stale` = the live version does not render the
        # principal carrier the interceptor switch now asks for (POST /api/policies/apply
        # re-deploys it); `policy_carrier` = whether the live version renders it (None until
        # a deploy under the policy module stamped the row). Nothing is narrowed any more.
        "policy_stale": policy_enforcement.stale_flag(agent),
        "policy_carrier": policy_enforcement.carrier_flag(agent),
        # D21 (M1-d, additive): the model catalog the latest deployment rendered from vs the
        # published one, and whether THIS agent's model row changed in between (a new
        # unrelated model does not raise it). None until a deployment carries the pin.
        "catalog_hint": catalog_hint(agent, deployment),
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }
    if deployment is not None:
        out["deployment"] = _deployment_out(deployment)
    return out


# Descriptor fields that may differ between two renders of the SAME row without the model's
# behaviour changing — the version stamp itself (every catalog write bumps it).
_HINT_IGNORED_FIELDS = frozenset({"catalog_version"})


def descriptor_changed(deployed: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    """Whether the catalog row an agent was deployed with differs from today's (D21 §5.5):
    compares the two descriptors minus the version stamp; one side missing (the model
    entered or left the catalog) counts as a change, both missing (an id outside the
    catalog, e.g. a custom BYOC id) does not."""
    if deployed is None and current is None:
        return False
    if deployed is None or current is None:
        return True
    strip = lambda d: {k: v for k, v in d.items() if k not in _HINT_IGNORED_FIELDS}  # noqa: E731
    return strip(deployed) != strip(current)


def catalog_hint(agent: Agent, deployment: Deployment | None) -> dict[str, Any] | None:
    """``{deployed, current, changed}`` for the agent view, or None when the deployment
    carries no catalog pin (pre-D21 rows, or no deployment yet)."""
    if deployment is None or deployment.catalog_version is None:
        return None
    model_id = str((agent.spec or {}).get("model_id") or "")
    now = model_descriptor(model_id) if model_id else None
    return {
        "deployed": int(deployment.catalog_version),
        "current": int(catalog_version()),
        "changed": descriptor_changed(deployment.model_descriptor, now),
    }


def _deployment_out(dep: Deployment) -> dict[str, Any]:
    return {
        "id": dep.id,
        "agent_id": dep.agent_id,
        "job_id": dep.job_id,
        "status": dep.status,
        "stages": dep.stages,
        "started_at": dep.started_at.isoformat() if dep.started_at else None,
        "ended_at": dep.ended_at.isoformat() if dep.ended_at else None,
        # D21 (M1-d, additive): the model catalog version this deployment rendered from
        "catalog_version": dep.catalog_version,
    }


def _agent_in(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer = Viewer.ALL
) -> Agent | None:
    """The agent, but only if it lives in this workspace, is not deleted and ``viewer``
    may see it (workspace isolation, ``services/agent_access.py``).

    A foreign or hidden id is indistinguishable from a missing one on purpose: the
    caller learns nothing about other workspaces' (or members') agents. A deleted row
    reads as missing too (N10v2-bug7) — the same answer the public ``/v1`` route gives,
    so a stale link lands on 404 ``agent.not_found`` on every entrance.
    """
    return agent_access.agent_in(db, ws.id, agent_id, viewer=viewer)


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


def require_agent_creator(identity: Identity, agent: Agent) -> None:
    """Creator-or-admin gate for the mutations that destroy or re-publish an agent
    (N10v2-bug4, re-pointed by Family B step 1 from the retired ``Agent.owner``
    username to ``created_by`` = the creator's ``users.id``): an administrator may act
    on every agent, the member whose account created the row may act on it, anyone
    else answers 403 ``agent.forbidden`` **before** any AWS call. Reads, invoke and
    chat are deliberately not gated — within a workspace they are the shared member
    surface (``route_policy``).

    A discovered import records no creator (externally owned; its delete only
    detaches the row and destroys nothing), so the gate lets any member who holds
    ``agents.delete`` detach it. A system preset (no creator either) is left to its
    own guard, ``refuse_system_mutation`` — one refusal code for presets, whoever asks.
    A legacy row whose pre-#261 owner could not be resolved to an account (NULL
    creator) is an administrator's.

    The creator-or-admin rule itself is `services/ownership.require_creator_or_admin`
    (r13-g), shared with the canary / Skill Lab / dataset / evaluator mutations; this
    wrapper only adds the two agent-specific exemptions above.
    """
    if agent.method == DISCOVERED_METHOD or system_agents.is_system_agent(agent):
        return
    ownership.require_creator_or_admin(
        agent, identity, "agent.forbidden",
        message="only the agent's creator or an administrator may re-publish or delete it",
        detail={"agent_id": agent.id},
    )


def _latest_deployment(db: Session, agent_id: str) -> Deployment | None:
    return (
        db.query(Deployment)
        .filter(Deployment.agent_id == agent_id)
        .order_by(Deployment.started_at.desc())
        .first()
    )


def delete_agent_cloud_resource(agent: Agent, workspace: WorkspaceContext) -> str | None:
    """Request deletion of the method-specific AWS resource only (no residue, no ledger);
    ``None`` for a discovered runtime, which is never ours to delete. Otherwise the
    runtime outcome: ``rt.RUNTIME_ABSENT`` when the runtime was already gone (N16-obs87 —
    the readback, not an AccessDenied from the delete, decides), else ``rt.RUNTIME_DELETED``
    (a method delete that reports nothing counts as deleted). Deletion is asynchronous:
    the conversation purge reads it back before the residue sweep runs."""
    if agent.method == DISCOVERED_METHOD:
        return None
    outcome = None
    if agent.method in ("zip_runtime", "studio"):
        outcome = zip_method.delete_agent_resources(agent, workspace)
    elif agent.method == "byoc":
        outcome = byoc_method.delete_agent_resources(agent, workspace)
    elif agent.method == "harness":
        outcome = zip_method.delete_agent_resources(agent, workspace)
    return rt.RUNTIME_ABSENT if outcome == rt.RUNTIME_ABSENT else rt.RUNTIME_DELETED


def _delete_agent_resources(agent: Agent, workspace: WorkspaceContext) -> teardown.TeardownReport:
    """Tear down the method-specific AWS resource for an agent (idempotent), then
    everything it left behind (N10-bug7/bug8: execution role + inline policies, runtime
    log groups, S3 artifacts, memory snapshots, A2A registry record).

    The Runtime delete is the one hard step — its exception propagates and the ledger
    row stays, so the member can retry. The residue steps run **after** it (deleting the
    execution role while the runtime still references it can wedge the runtime's own
    deletion) and are best-effort: each is logged and reported, none fails the delete.
    The architect's conversation purge splits the two halves (``delete_agent_cloud_resource``,
    then an AWS readback, then ``teardown.cleanup_residue``) so the role goes only once the
    Runtime is read back gone.
    """
    outcome = delete_agent_cloud_resource(agent, workspace)
    if outcome is None:
        return teardown.TeardownReport(
            aws_resource_deleted=False,
            steps=[teardown.TeardownStep(
                teardown.STEP_RUNTIME, teardown.SKIPPED,
                "imported runtime — detached, not owned by this workspace",
            )],
        )
    if agent.resource_id and outcome == rt.RUNTIME_ABSENT:
        # N16-obs87: the runtime was gone before this delete (read back ResourceNotFound);
        # the row and its residue still go, but nothing was removed here
        runtime_step = teardown.TeardownStep(
            teardown.STEP_RUNTIME, teardown.SKIPPED,
            f"runtime {agent.resource_id} already gone (not found on readback)",
        )
    elif agent.resource_id:
        runtime_step = teardown.TeardownStep(teardown.STEP_RUNTIME, teardown.DELETED,
                                             agent.resource_id)
    else:
        # N16-obs76: the deploy never reached provision (failed at generate / package), so
        # there was no Runtime to delete — say so instead of reporting one "deleted"
        runtime_step = teardown.TeardownStep(
            teardown.STEP_RUNTIME, teardown.SKIPPED,
            f"no runtime was created for this {agent.method} agent (deploy never provisioned)",
        )
    report = teardown.TeardownReport(
        aws_resource_deleted=runtime_step.status == teardown.DELETED, steps=[runtime_step],
    )
    # captured once: the log-group settle thread keeps logging after this request has
    # deleted the row and closed its session, when the ORM instance may be expired
    agent_id = agent.id
    report.steps.extend(teardown.cleanup_residue(
        agent,
        workspace,
        settings=get_settings(),
        log=lambda msg: logger.info("agent %s: %s", agent_id, msg),
    ))
    return report


def _as_report(value: Any) -> teardown.TeardownReport:
    """Normalise what ``_delete_agent_resources`` (or a test stub standing in for it)
    returned: a report passes through, a bare truthy/falsy value becomes a report
    with no residue steps."""
    if isinstance(value, teardown.TeardownReport):
        return value
    return teardown.TeardownReport(aws_resource_deleted=bool(value))


def _require_spec_models_available(spec: AgentSpec, ws: WorkspaceScope,
                                   identity: Identity | None = None) -> None:
    """N-R6-obs1: every model the spec could invoke has a configured provider in this
    workspace — 422 `model.provider_unconfigured` naming the SSM parameter to create,
    instead of a deploy that only fails at invoke time. The catalog itself is enforced
    by `AgentSpec`; this guard is at the request boundary so stored specs keep parsing.
    N17-obs203 §6: a model of ANOTHER member's personal provider is 422
    `agent.model_not_visible` first — its owner or an administrator may still deploy it."""
    try:
        assert_spec_models_available(spec, ws.context.resources,
                                     principal=principal_of_identity(identity))
    except ModelRefusal as exc:
        code = "agent.model_not_visible" if exc.code == MODEL_NOT_VISIBLE_CODE else exc.code
        raise AppError(code, exc.reason, {"model_id": spec.model_id},
                       status_code=422) from exc


def _validate_tool_auth(spec: AgentSpec, ws: WorkspaceScope) -> None:
    """Every tool auth block names a live Connection of the declared kind.

    Only reaches AWS when some tool carries auth, so an ordinary deploy's
    request path is unchanged."""
    if any(tool.auth is not None for tool in spec.tools):
        identity_providers.validate_tool_auth(control_client(ws.context), spec)


@router.post("/agents", status_code=202)
def create_agent(
    spec: AgentSpecRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    # The creator is the signed-in account (N10v2-obs9: a configured default used to
    # show instead), and it is what the creator-or-admin gate checks later.
    identity = require_identity(request)
    if spec.method not in SUPPORTED_METHODS:
        raise AppError(
            "agent.method_not_available",
            f"method '{spec.method}' ships in a later phase",
            {"supported": sorted(SUPPORTED_METHODS)},
            status_code=400,
        )
    # round 16 (N15-obs16 generalised): an option the method's generate stage cannot render,
    # or a workspace resource the deploy needs, is refused here — before any row / job
    method_validation.require_method_support(spec, ws.context, db)
    # Reserved for a system-managed preset (the preset's name, its base name and every
    # `<base>-<suffix>` — N16-obs82): an ordinary agent can never hold it, so a later
    # install cannot be confused with (or adopt) a member's agent, and the Runtime / role
    # / S3 names derived from it never collide with the preset family's.
    refuse_reserved_name(spec.name)
    # Names are unique per workspace, not per ledger: two environments own their
    # own AgentCore resource namespaces.
    existing = agent_names.live_holder(db, ws.id, spec.name)
    if existing:
        # the 409 names the holder only when the caller may see it (isolation)
        raise agent_names.name_exists_error(
            spec.name, agent_names.visible_holder_id(db, viewer, existing)
        )
    # every KB / memory the spec pins must be visible to the caller (isolation) —
    # answered before the model / tool-auth / policy checks describe anything
    agent_access.check_pins(db, viewer, ws.context, spec.model_dump())
    _require_spec_models_available(spec, ws, identity)
    _validate_tool_auth(spec, ws)
    # a pinned memory must be one this workspace manages and ACTIVE (issue #55);
    # the deploy job re-checks before any stage, this answers before any row
    memory_ownership.require_spec_memory(db, ws.context, spec.model_dump())
    # D20 §4.2: `policy_ids` must name this workspace's policies (422 policy.unknown_policy)
    policy_enforcement.validate_spec_policy_ids(db, ws.id, spec)
    agent = Agent(
        workspace_id=ws.id,
        name=spec.name,
        method=spec.method,
        status="deploying",
        spec=spec.model_dump(),
    )
    creators.stamp(agent, creators.Creator.of(identity))
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
            agent_access.agent_clause(viewer),  # workspace isolation
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
    if hidden:  # a hidden owner is never named
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
            uploaded_by_id=identity.user_id,
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
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """The stored detection summary + provenance for one staged BYOC upload.

    N16-obs77: the workspace-visibility rule (users-and-ownership §7) applies — under
    ``isolated`` a member reads only an upload they staged, an administrator staged, or
    one with no recorded uploader (a manifest written before ``uploaded_by_id`` existed);
    a hidden upload is the same 404 as a missing one. ``shared`` keeps by-id reads open."""
    manifest = byoc_uploads.get_manifest(ws.context, upload_id)
    visibility.require_seen(
        viewer, db, manifest.get("uploaded_by_id"),
        NotFoundError("byoc.upload_not_found", "upload not found"),
    )
    return manifest


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
    # the newest deployment carries the catalog pin the hint compares (D21 M1-d)
    out = _agent_out(agent, deployments[0] if deployments else None)
    out["deployments"] = [_deployment_out(d) for d in deployments]
    return out


@router.get("/agents/{agent_id}/versions")
def get_agent_versions(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Read-only AWS versions + endpoints of the agent's AgentCore Runtime.

    Follows every list page; the projection is allow-listed (no environment,
    artifact, role or authorizer values). 409 ``agent.no_resource`` when the row
    has no Runtime resource to ask about (e.g. an imported, discovered managed
    Harness — N1 residue, see ``is_discovered_harness``).
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return list_agent_versions(control_client(ws.context), agent)


@router.get("/agents/{agent_id}/trace-status")
def get_agent_trace_status(
    agent_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """N17-obs465: whether the agent's traces reach CloudWatch — the aws-cn trace exporter
    layer's start-up line (primary) and whether the newest session left spans (secondary);
    `badge`: ok | unavailable | not_arriving | unknown. Read-only."""
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return trace_status.trace_status(db, agent, ws.context)


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
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    return agent_identity.agent_identity(
        control_client(ws.context), agent, ws.context.resources or {}
    )


@router.post("/agents/{agent_id}/redeploy", status_code=202)
def redeploy_agent(
    agent_id: str,
    spec: AgentSpecRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Re-publish an agent in place with an edited spec.

    Runs the pipeline in "update" mode: the deploy stage calls UpdateAgentRuntime
    instead of Create, so AgentCore publishes a NEW VERSION on the SAME resource
    — the agentRuntimeId and ARN are unchanged and the DEFAULT endpoint
    auto-rolls to the new version (near-zero downtime, versioned +
    rollback-able). package/provision still rebuild the artifact so edited
    code/requirements ship. If the agent has no live resource yet (e.g. a
    failed first deploy), the deploy stage falls back to Create.

    Name and method are immutable — changing either would be a different agent.
    Creator-or-admin (``require_agent_creator``); 403 ``agent.forbidden`` otherwise.
    """
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    identity = require_identity(request)
    _require_spec_models_available(spec, ws, identity)
    return republish_agent(
        db, agent, spec, ws, identity=identity, actor=Creator.of(identity), viewer=viewer
    )


def republish_agent(
    db: Session, agent: Agent, spec: AgentSpec, ws: WorkspaceScope, *, identity: Identity,
    actor: Creator | None = None, note: str | None = None, viewer: Viewer = Viewer.ALL,
) -> dict[str, Any]:
    """The redeploy guards + an update-mode deploy job for an edited spec — the ONE
    entry point for every spec update (the redeploy route today; an AI-recommendation
    accept or a future PUT/PATCH tomorrow). ``identity`` is required so no caller can
    reach a re-publish without the creator-or-admin gate (``require_agent_creator``).
    ``actor`` / ``note`` land on the Job payload and on the spec snapshot the publish
    writes (upstream #277)."""
    require_agent_creator(identity, agent)
    _refuse_redeploy(agent)
    # N17-obs16 (agents.md §1.1): a running Runtime canary owns the agent's base — a
    # re-publish under it would let the canary's later `complete` revert production to a
    # candidate built on the pre-redeploy spec. Same 409 agent.in_use as the delete, after
    # the deploy-job guard, before any validation that reads the workspace.
    # canaries only: a queued run is designed to pick up the version live at dispatch (§22)
    canary_references.refuse_agent_in_use(db, agent, action="re-publishing the agent",
                                          kinds=("canaries",))
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
    # round 16: the edited spec is a creation for the pipeline — same domain check first
    method_validation.require_method_support(spec, context_for_workspace(agent.workspace_id), db)
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
    policy_enforcement.validate_spec_policy_ids(db, ws.id, spec)  # D20 §4.2
    agent.spec = spec.model_dump()
    agent.status = "deploying"
    agent.error = None
    agent.updated_at = datetime.now(UTC)
    db.flush()
    deployment, job = create_deployment(db, agent, mode="update", actor=actor, note=note)
    start_deploy_async(job.id)
    return {"agent": _agent_out(agent), "job_id": job.id, "deployment_id": deployment.id}


# ── spec snapshots: one per publish, field-level diff (upstream #277, reads only) ──


def _snapshot_agent(
    db: Session, ws: WorkspaceScope, agent_id: str, viewer: Viewer
) -> Agent:
    """The agent whose snapshots are read — a deleted agent's history stays readable
    (the scorecards keep a deleted agent's runs); a foreign or hidden one is
    ``agent.not_found``."""
    agent = agent_access.agent_in(db, ws.id, agent_id, viewer=viewer, include_deleted=True)
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
    from_seq: Annotated[int | None, Query(ge=1, le=SQLITE_MAX_INT)] = None,
    to_seq: Annotated[int | None, Query(ge=1, le=SQLITE_MAX_INT)] = None,
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
    seq: Annotated[int, PathParam(ge=1, le=SQLITE_MAX_INT)],
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


@router.post("/agents/{agent_id}/invoke", response_model=InvokeResponse)
def invoke_agent(
    agent_id: str,
    req: InvokeRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> InvokeResponse:
    validate_session_id(req.session_id)  # N-SCN-obs16: same bounds as Chat and /v1
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    prepared = prepare_attachments(
        agent, req.attachments, prompt=req.prompt, session_id=req.session_id,
    )
    started = time.monotonic()
    extra = {"attachments": prepared} if prepared else {}
    result = invoke_agent_text(
        agent, req.prompt, session_id=req.session_id,
        actor_id=scoped_actor(agent.id, req.actor_id),
        workspace=ws.context,
        # D20: the console caller is the policy principal, like Chat
        principal=Principal.from_identity(require_identity(request)),
        token_channel="console",  # stream A2: the I1 `ch` of the console invoke
        **extra,
    )
    return InvokeResponse(
        text=result["text"],
        session_id=result["session_id"],
        latency_ms=int((time.monotonic() - started) * 1000),
    )


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Creator-or-admin (``require_agent_creator``) — the N10 v2 case 12 boundary: a
    member must not be able to destroy another account's agent and its AWS
    resources. Already-deleted rows answer 404 like an unknown id."""
    agent = _agent_in(db, ws, agent_id, viewer)
    if agent is None:
        raise NotFoundError("agent.not_found", "agent not found")
    require_agent_creator(require_identity(request), agent)
    # the conversation purge owns this agent's teardown while it runs
    assistant_purge.refuse_while_clearing(agent.id, "delete")
    report = teardown_agent_row(db, agent, ws.context)
    # `removed`/`failed`/`steps` (N10-bug7/bug8) report what the delete took with it;
    # `aws_resource_deleted` keeps its historical meaning (the Runtime itself).
    return {"deleted": True, "agent_id": agent_id, **report.as_dict()}


def teardown_agent_row(
    db: Session, agent: Agent, workspace: WorkspaceContext
) -> teardown.TeardownReport:
    """The one agent teardown: refuse a protected preset BEFORE any AWS call, delete the
    method-specific resource and its residue (role, log groups, artifacts, snapshots,
    registry record), mark the ledger row deleted and release its name claim. Shared by
    the route above and the architect assistant's conversation purge (through
    ``delete_agent_row``), so both delete an agent the same way."""
    # Before the AWS teardown: a refused delete must leave the harness untouched.
    system_agents.refuse_system_mutation(agent, "delete")
    refuse_delete_while_deploying(db, agent)
    refuse_delete_with_running_canary(db, agent)
    report = _as_report(_delete_agent_resources(agent, workspace))
    mark_agent_deleted(db, agent)
    return report


def refuse_delete_while_deploying(db: Session, agent: Agent) -> None:
    """N16-obs83 (live, t1): a DELETE while the agent's deploy job was in `package`
    answered 200 and the job kept running through provision / deploy / register to
    "succeeded" — an AgentCore Runtime, an execution role, the S3 package, a log group
    and a registry record with no agent row. RULING: an agent whose deploy job is still
    `queued` / `running` cannot be deleted — 409 ``agent.deploy_in_progress {job_id,
    stage}`` for members and administrators alike (no force); the member waits for the
    job to end (succeeded or failed) and deletes then. Consistent with the 409
    ``aws.conflict`` AWS already answers while the Runtime is CREATING; this closes the
    window before any resource exists. The pipeline's own per-stage re-read of the row
    (``pipeline.agent_gone``) is the second belt for a client that never saw this 409."""
    refuse_deploy_in_progress(db, agent.id, then="delete the agent")


def refuse_delete_with_running_canary(db: Session, agent: Agent) -> None:
    """N16-obs111 (r16-o reference graph): a DELETE answered 200 while a Runtime canary on
    the agent was `running` — the canary row kept routing to a `deleted` Agent, its A/B
    row and per-arm online configs stayed live, and every later action but `verdict`
    re-resolved the agent to `404 agent.not_found`, so only `cleanup` could ever settle
    it. RULING: an agent that is the champion or challenger of a canary whose status is
    not terminal cannot be deleted — 409 ``agent.in_use {canaries}`` for members and
    administrators alike (no force). The one rule lives in
    ``services/canary_references.refuse_agent_in_use`` and is shared with the system-preset
    uninstall (N16-obs111a); the architect purge is NOT this path: it cleans the
    conversation's canaries (`_step_canaries`) before its own agent step and marks the row
    through ``mark_agent_deleted``, so a clear still removes both."""
    canary_references.refuse_agent_in_use(db, agent)  # canaries AND active runs (N17-obs18)


def mark_agent_deleted(db: Session, agent: Agent) -> None:
    """The ledger half of an agent delete: forget its workload identity (and every 3LO
    token keyed by it, which went with the runtime), mark it deleted, release its name."""
    oauth_sessions.forget_agent(db, agent.workspace_id, agent.id)
    principal_mint.forget_agent_sessions(db, agent.id)  # per-user policy: its lpp- records
    agent.status = "deleted"
    agent.updated_at = datetime.now(UTC)
    agent_names.release_agent_name(db, agent.workspace_id, agent.name, agent.id)
    db.commit()



def delete_agent_row(db: Session, agent: Agent, workspace: WorkspaceContext) -> bool:
    """``teardown_agent_row`` reduced to the historical boolean (the Runtime was torn
    down) for callers that record only that — the assistant's conversation purge."""
    return teardown_agent_row(db, agent, workspace).aws_resource_deleted


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
    return job_out(job)


def job_out(job: Job) -> dict[str, Any]:
    """The one job projection (status, payload, JSONL events) — shared with the
    system-preset job route, which reads a preset's job regardless of the workspace id
    it was filed under (N12-obs31 a)."""
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
