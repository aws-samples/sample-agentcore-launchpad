"""Install / repair / status / uninstall of system-managed presets.

Everything that touches the ledger or AWS for a preset lives here so the router stays a
thin HTTP mapping and the rest of the app can ask one question (``is_system_agent`` /
``refuse_system_mutation``).

Invariants this module upholds:

* **No AWS on read.** ``preset_status`` reads the ledger and the workspace row only.
* **Server-owned identity.** The spec is derived from the preset catalogue; the only
  client-influenced values are the administrator's install options.
* **Never adopt.** A live ordinary agent that already holds the reserved name is a
  refusal, not a takeover — an operator removes or renames it first.
* **Durable, atomic maintenance claims.** Fresh installs race into the partial unique
  index on ``(workspace_id, system_key)``; repair and uninstall race through a
  compare-and-set on ``agents.status`` executed in the same transaction as the job
  row they create. Whoever loses re-reads the winner and returns *its* job, so
  repeated or concurrent requests converge on one outcome and never stack jobs.
* **Pinned releases.** The install pins ``{version, digest}`` of the skill bundle on
  the job; the package stage refuses to publish anything else and treats an already
  published version as immutable.
* **Dedicated role or nothing.** A preset is never created or updated on the shared
  workspace execution role (see ``require_dedicated_role``).
"""

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.deployer.pipeline import StageContext, StageResult, create_deployment
from app.models.ledger import Agent, Deployment, Job, Workspace
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services.workspace import WorkspaceContext
from app.system_agents import presets as catalogue
from app.system_agents.presets import InstallOptions, SystemPreset

logger = logging.getLogger("launchpad.system_agents")

READY = "ready"

STATUS_CONFIGURATION_REQUIRED = "configuration_required"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_DEPLOYING = "deploying"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"

# Statuses a maintenance claim may take over (a deploying row belongs to its job).
_CLAIMABLE = ("active", "failed", "draft")

MANIFEST_KEY = ".bundle-manifest.json"


# ---------------------------------------------------------------------------
# identity helpers used across the app
# ---------------------------------------------------------------------------


def is_system_agent(agent: Agent | None) -> bool:
    return bool(agent is not None and agent.system_key)


def refuse_system_mutation(agent: Agent, action: str) -> None:
    """Raise the one error every ordinary mutation path answers for a preset.

    Called *before* any AWS client is built, so a refusal has no side effect. The
    ``perm:*`` a member holds is irrelevant here: presets are not member-lifecycle
    agents, and even an administrator uses ``/api/system-agents`` instead.
    """
    if is_system_agent(agent):
        raise AppError(
            "agent.system_managed",
            f"'{agent.name}' is a system-managed preset; {action} is not available on "
            "it. Administrators maintain it through the System presets panel.",
            {
                "agent_id": agent.id,
                "system_key": agent.system_key,
                "action": action,
                "maintenance_route": f"/api/system-agents/{agent.system_key}",
            },
            status_code=403,
        )


def assert_not_system_agent(db: Session, agent_id: str | None, action: str) -> None:
    """Router-side guard for rows that *reference* an agent (experiments, canaries)."""
    if not agent_id:
        return
    agent = db.get(Agent, agent_id)
    if agent is not None:
        refuse_system_mutation(agent, action)


def refuse_system_agent_id(agent_id: str | None, action: str) -> None:
    """Service-side guard with its own short-lived session (background actions)."""
    if not agent_id:
        return
    db = SessionLocal()
    try:
        assert_not_system_agent(db, agent_id, action)
    finally:
        db.close()


def system_projection(agent: Agent) -> dict[str, Any] | None:
    """The ``system`` member of the agent API projection (None for ordinary rows)."""
    if not is_system_agent(agent):
        return None
    preset = catalogue.get_preset(agent.system_key or "")
    return {
        "managed": True,
        "key": agent.system_key,
        "label": preset.label if preset else agent.system_key,
        "skill_version": catalogue.skill_version_from_spec(agent.spec or {}),
        "protected_actions": ["redeploy", "delete", "convert", "experiment", "canary"],
    }


# ---------------------------------------------------------------------------
# workspace readiness
# ---------------------------------------------------------------------------

REQ_BOOTSTRAP = "bootstrap_not_ready"
REQ_BUCKET = "missing_artifacts_bucket"
REQ_ROLE = "missing_execution_role"
REQ_PER_AGENT_ROLES = "per_agent_roles_disabled"


def workspace_requirements(row: Workspace, settings: Any | None = None) -> list[dict[str, str]]:
    """What still stands between this workspace and a preset install.

    Each entry is ``{code, message}``: the console localizes on ``code`` and shows the
    English ``message`` only as a fallback. Ledger + settings only — no AWS.
    """
    missing: list[dict[str, str]] = []
    if row.bootstrap_status != READY:
        missing.append({
            "code": REQ_BOOTSTRAP,
            "message": f"workspace bootstrap is '{row.bootstrap_status}', needs 'ready'",
        })
    resources = row.resources or {}
    if not resources.get("artifacts_bucket"):
        missing.append({
            "code": REQ_BUCKET,
            "message": "artifacts_bucket missing from the workspace resource map",
        })
    if not resources.get("execution_role_arn"):
        missing.append({
            "code": REQ_ROLE,
            "message": "execution_role_arn missing from the workspace resource map",
        })
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    if not settings.per_agent_execution_roles:
        missing.append({
            "code": REQ_PER_AGENT_ROLES,
            "message": (
                "per-agent execution roles are disabled (LAUNCHPAD_PER_AGENT_EXECUTION_"
                "ROLES=false) — a preset never runs on the shared role"
            ),
        })
    return missing


def require_dedicated_role(
    agent: Agent, role_arn: str | None, workspace: WorkspaceContext, settings: Any
) -> None:
    """Fail closed: a preset is deployed on its own restricted role or not at all.

    ``role_arn`` is the ARN about to be sent (None at generate time, when only the
    setting can be checked). The shared CDK role can read every member's skill
    bundle and mutate gateways — the opposite of the preset contract.
    """
    if not settings.per_agent_execution_roles:
        raise RuntimeError(
            f"system preset '{agent.system_key}' requires per-agent execution roles; "
            "set LAUNCHPAD_PER_AGENT_EXECUTION_ROLES=true (the default) and re-run "
            "the install — the preset is never deployed on the shared workspace role"
        )
    shared = agent_iam.shared_role_arn(workspace)
    if role_arn is not None and (not role_arn or role_arn == shared):
        raise RuntimeError(
            f"system preset '{agent.system_key}' resolved to the shared workspace "
            f"execution role ({shared or 'unset'}); refusing to deploy it — the "
            "provision stage must produce the dedicated launchpad-agent-* role"
        )


def verify_knowledge_bases(ctx: StageContext, spec: AgentSpec) -> None:
    """Prove every referenced KB exists, is MANAGED and ACTIVE in *this* workspace.

    Runs in the provision stage before any gateway target is created, so a typo or a
    KB from another account fails with an actionable stage error instead of a wired
    target pointing at nothing.
    """
    if not spec.knowledge_bases:
        return
    from app.services.agentcore.client import agent_client
    from app.services.knowledge import _kb_type

    client = agent_client(ctx.workspace)
    where = f"workspace {ctx.workspace.id} ({ctx.workspace.region})"
    for kb in spec.knowledge_bases:
        try:
            detail = client.get_knowledge_base(knowledgeBaseId=kb.kb_id)["knowledgeBase"]
        except client.exceptions.ResourceNotFoundException as exc:
            raise RuntimeError(
                f"knowledge base {kb.kb_id} does not exist in {where} — mount an "
                "existing, authorized knowledge base or install without it"
            ) from exc
        if _kb_type(detail) != "MANAGED":
            raise RuntimeError(
                f"knowledge base {kb.kb_id} in {where} is not a MANAGED knowledge base"
            )
        status = detail.get("status")
        if status != "ACTIVE":
            raise RuntimeError(
                f"knowledge base {kb.kb_id} in {where} is {status or 'in an unknown state'}, "
                "not ACTIVE — wait for it or install without it"
            )
        ctx.log(f"knowledge base {kb.kb_id} verified · MANAGED · ACTIVE")


# ---------------------------------------------------------------------------
# status (ledger-only)
# ---------------------------------------------------------------------------


def find_installed(db: Session, workspace_id: str, preset: SystemPreset) -> Agent | None:
    return (
        db.query(Agent)
        .filter(
            Agent.workspace_id == workspace_id,
            Agent.system_key == preset.key,
            Agent.status != "deleted",
        )
        .first()
    )


def find_name_holder(db: Session, workspace_id: str, preset: SystemPreset) -> Agent | None:
    """A live *ordinary* agent squatting on the reserved name (never adopted)."""
    return (
        db.query(Agent)
        .filter(
            Agent.workspace_id == workspace_id,
            Agent.name == preset.name,
            Agent.status != "deleted",
            Agent.system_key.is_(None),
        )
        .first()
    )


def _latest_deployment(db: Session, agent_id: str) -> Deployment | None:
    return (
        db.query(Deployment)
        .filter(Deployment.agent_id == agent_id)
        .order_by(Deployment.started_at.desc(), Deployment.id.desc())
        .first()
    )


def _latest_job(db: Session, agent: Agent) -> tuple[Deployment | None, Job | None]:
    deployment = _latest_deployment(db, agent.id)
    job = db.get(Job, deployment.job_id) if deployment and deployment.job_id else None
    return deployment, job


def preset_status(
    db: Session, row: Workspace, preset: SystemPreset, *, is_admin: bool
) -> dict[str, Any]:
    """One preset's state in one workspace. Ledger + workspace row only — no AWS."""
    requirements = workspace_requirements(row)
    agent = find_installed(db, row.id, preset)
    holder = find_name_holder(db, row.id, preset)
    if agent is None:
        status = STATUS_CONFIGURATION_REQUIRED if requirements else STATUS_NOT_INSTALLED
    elif agent.status == "deploying":
        status = STATUS_DEPLOYING
    elif agent.status == "active":
        status = STATUS_ACTIVE
    else:
        status = STATUS_FAILED
    deployment = _latest_deployment(db, agent.id) if agent else None
    spec = (agent.spec or {}) if agent else {}
    installed_version = catalogue.skill_version_from_spec(spec) if agent else None
    installed = agent is not None
    settled = installed and agent.status != "deploying"
    return {
        "key": preset.key,
        "name": preset.name,
        "label": preset.label,
        "description": preset.description,
        "method": "harness",
        "skill_version": preset.skill_version,
        "installed_skill_version": installed_version,
        "update_available": bool(agent and installed_version != preset.skill_version),
        "status": status,
        "requirements": requirements,
        "name_collision": (
            {"agent_id": holder.id, "agent_name": holder.name, "method": holder.method}
            if holder
            else None
        ),
        "agent_id": agent.id if agent else None,
        "agent_status": agent.status if agent else None,
        "error": agent.error if agent else None,
        "job_id": deployment.job_id if deployment else None,
        "deployment_id": deployment.id if deployment else None,
        "deployment_status": deployment.status if deployment else None,
        "model_id": spec.get("model_id"),
        "model_source": spec.get("model_source"),
        "knowledge_bases": spec.get("knowledge_bases") or [],
        "allowed_tools": spec.get("allowed_tools") or list(preset.allowed_tools),
        "memory": "disabled",
        # Operation-specific server verdicts — the console disables on these, the
        # routes re-check them.
        "can_install": is_admin and not installed and not requirements and holder is None,
        "can_repair": is_admin and settled and not requirements,
        "can_uninstall": is_admin and settled,
        "updated_at": agent.updated_at.isoformat() if agent and agent.updated_at else None,
    }


def list_status(db: Session, row: Workspace, *, is_admin: bool) -> list[dict[str, Any]]:
    return [
        preset_status(db, row, preset, is_admin=is_admin)
        for preset in catalogue.PRESETS.values()
    ]


# ---------------------------------------------------------------------------
# install / repair
# ---------------------------------------------------------------------------


@dataclass
class InstallOutcome:
    agent: Agent
    job: Job | None
    deployment: Deployment | None
    created: bool
    changed: bool  # True ⇔ this call started a new job (caller launches it)

    @property
    def status_code(self) -> int:
        return 202 if self.job is not None and self.agent.status == "deploying" else 200


def _require_ready(row: Workspace) -> str:
    missing = workspace_requirements(row)
    if missing:
        raise AppError(
            "system_agent.workspace_not_ready",
            "this workspace cannot host a system preset yet: "
            + "; ".join(m["message"] for m in missing),
            {"requirements": missing, "workspace_id": row.id},
            status_code=409,
        )
    return str(row.resources["artifacts_bucket"])


def _pin_release(db: Session, job: Job, preset: SystemPreset) -> None:
    """Record the exact bundle release this job may publish."""
    job.payload = {**(job.payload or {}), "preset_bundle": catalogue.bundle_release(preset)}
    db.commit()


def _in_flight(db: Session, agent: Agent) -> InstallOutcome:
    deployment, job = _latest_job(db, agent)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                          changed=False)


def install_preset(
    db: Session,
    row: Workspace,
    preset: SystemPreset,
    options: InstallOptions | None,
    *,
    force: bool = False,
) -> InstallOutcome:
    """Install, or repair, one preset in one workspace. Idempotent.

    * not installed → row + create job (a racing twin collapses onto the winner and
      returns the winner's job)
    * deploying → the in-flight job, never a second one
    * active, same version and same options, not forced → no-op (200, no job)
    * failed, changed options, newer bundle, or ``force`` → update job (re-publish)

    The only AWS work this starts is the deploy job on a background thread; the
    request itself makes no cloud call.
    """
    bucket = _require_ready(row)
    holder = find_name_holder(db, row.id, preset)
    if holder is not None:
        raise AppError(
            "system_agent.name_collision",
            f"an ordinary agent named '{preset.name}' already exists in this workspace; "
            "the preset never adopts it — delete or rename that agent first",
            {"agent_id": holder.id, "agent_name": holder.name, "method": holder.method},
            status_code=409,
        )

    existing = find_installed(db, row.id, preset)
    if existing is not None:
        return _repair(db, existing, preset, bucket, options, force=force)

    spec = catalogue.build_spec(preset, bucket, options or InstallOptions())
    agent = Agent(
        workspace_id=row.id,
        name=preset.name,
        method="harness",
        status="deploying",
        spec=spec.model_dump(),
        owner="system",
        system_key=preset.key,
    )
    db.add(agent)
    try:
        db.flush()
        deployment, job = create_deployment(db, agent)  # commits the row + job together
    except IntegrityError:
        # A concurrent install won the unique index; hand back its row AND its job.
        db.rollback()
        winner = find_installed(db, row.id, preset)
        if winner is None:  # pragma: no cover — the index only fires for a live twin
            raise
        return _in_flight(db, winner)
    _pin_release(db, job, preset)
    logger.info("system preset %s: install job %s in workspace %s", preset.key, job.id, row.id)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=True, changed=True)


def _repair(
    db: Session,
    agent: Agent,
    preset: SystemPreset,
    bucket: str,
    options: InstallOptions | None,
    *,
    force: bool,
) -> InstallOutcome:
    db.refresh(agent)  # never decide on a stale ORM snapshot
    if agent.status == "deploying":
        return _in_flight(db, agent)  # repeated clicks while a job runs stack nothing

    stored = agent.spec or {}
    resolved = options or catalogue.options_from_spec(stored)
    desired = catalogue.build_spec(preset, bucket, resolved).model_dump()
    if agent.status == "active" and desired == stored and not force:
        deployment, job = _latest_job(db, agent)
        return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                              changed=False)

    # Durable claim: one UPDATE conditioned on the status we are taking over, in the
    # same transaction as the job row. Two sessions that both loaded an active row
    # serialize on the write lock; the second sees 0 rows and returns the winner.
    now = datetime.now(UTC)
    claimed = db.execute(
        update(Agent)
        .where(Agent.id == agent.id, Agent.status.in_(_CLAIMABLE))
        .values(status="deploying", spec=desired, error=None, updated_at=now)
    ).rowcount
    if claimed != 1:
        db.rollback()
        db.expire_all()
        current = db.get(Agent, agent.id)
        if current is None or current.status == "deleted":
            raise NotFoundError(
                "system_agent.not_installed", f"'{preset.key}' was uninstalled meanwhile"
            )
        return _in_flight(db, current)
    db.expire(agent)
    deployment, job = create_deployment(db, agent, mode="update")  # commits the claim
    _pin_release(db, job, preset)
    logger.info("system preset %s: repair job %s for agent %s", preset.key, job.id, agent.id)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                          changed=True)


# ---------------------------------------------------------------------------
# package stage: versioned, checksummed, pinned skill upload (inside the deploy job)
# ---------------------------------------------------------------------------


def _pinned_release(ctx: StageContext) -> dict[str, Any] | None:
    db = ctx.session()
    try:
        job = db.get(Job, ctx.job_id)
        return dict((job.payload or {}).get("preset_bundle") or {}) if job else None
    finally:
        db.close()


def _read_manifest(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    except Exception as exc:  # noqa: BLE001 — stub clients raise their own shapes
        if type(exc).__name__ in ("NoSuchKey", "NotFound"):
            return None
        raise
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {"digest": None, "corrupt": True}


def package_preset_skills(ctx: StageContext, agent: Agent) -> StageResult:
    """Publish the preset's repository bundle to its versioned S3 prefix.

    Order of checks, all before the first write:

    1. the stored spec's version must equal this build's catalogue version (a job
       queued under one release is not silently served another);
    2. the job's pinned ``{version, digest}`` must equal the bytes on disk (a checkout
       mutated between queueing and running fails here);
    3. an already published version is immutable: an existing manifest with the same
       digest makes this a verified no-op, a different digest is a hard failure.

    Objects are sent with a SHA-256 checksum S3 verifies on receipt; the manifest is
    written last, so a partial upload has no manifest and the next run re-publishes.
    """
    preset = catalogue.get_preset(agent.system_key or "")
    if preset is None:
        raise RuntimeError(f"unknown system preset key {agent.system_key!r} on agent {agent.id}")
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError("artifacts_bucket missing from this workspace's resource map")

    spec_version = catalogue.skill_version_from_spec(agent.spec or {})
    if spec_version != preset.skill_version:
        raise RuntimeError(
            f"queued release mismatch: the stored spec pins skill bundle v{spec_version} "
            f"but this build ships v{preset.skill_version} — re-run the preset install "
            "so the spec and the bundle are re-derived together"
        )
    digest, files = catalogue.bundle_digest(preset.skill_path())
    pinned = _pinned_release(ctx)
    if pinned and (pinned.get("version") != preset.skill_version or pinned.get("digest") != digest):
        raise RuntimeError(
            f"pinned bundle mismatch: the job was queued for v{pinned.get('version')} "
            f"digest {str(pinned.get('digest'))[:12]}, the checkout now holds "
            f"v{preset.skill_version} digest {digest[:12]} — re-run the preset install"
        )

    prefix = preset.skill_prefix()
    s3 = ctx.workspace.client("s3")
    manifest_key = f"{prefix}{MANIFEST_KEY}"
    published = _read_manifest(s3, bucket, manifest_key)
    if published is not None:
        if published.get("digest") == digest:
            detail = (
                f"skill bundle {preset.name} v{preset.skill_version} already published · "
                f"sha256 {digest[:12]} verified · s3://{bucket}/{prefix}"
            )
            ctx.log(detail)
            return StageResult(detail=detail)
        raise RuntimeError(
            f"s3://{bucket}/{prefix} already holds v{preset.skill_version} with digest "
            f"{str(published.get('digest'))[:12]} ≠ {digest[:12]}; a published version is "
            "immutable — bump the preset's skill_version (and the SKILL.md version)"
        )

    bundle = catalogue.load_bundle(preset)
    try:
        for rel in bundle.files:
            body = (bundle.root / rel).read_bytes()
            checksum = base64.b64encode(sha256(body).digest()).decode()
            s3.put_object(
                Bucket=bucket,
                Key=f"{prefix}{rel}",
                Body=body,
                ChecksumSHA256=checksum,
                ContentType="text/markdown; charset=utf-8" if rel.endswith(".md")
                else "application/octet-stream",
            )
            ctx.log(f"uploaded s3://{bucket}/{prefix}{rel}")
    finally:
        bundle.close()
    manifest = json.dumps(
        {"name": preset.name, "version": preset.skill_version, "digest": digest,
         "files": files, "published_at": datetime.now(UTC).isoformat()},
        ensure_ascii=False,
    ).encode()
    s3.put_object(
        Bucket=bucket, Key=manifest_key, Body=manifest,
        ChecksumSHA256=base64.b64encode(sha256(manifest).digest()).decode(),
        ContentType="application/json",
    )
    detail = (
        f"skill bundle {preset.name} v{preset.skill_version} · {len(files)} files · "
        f"sha256 {digest[:12]} → s3://{bucket}/{prefix}"
    )
    ctx.log(detail)
    return StageResult(detail=detail)


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def uninstall_preset(db: Session, row: Workspace, preset: SystemPreset, delete_resources) -> dict:
    """Admin-only teardown, serialized against repair by the same status CAS.

    The ledger claim (``status='deleted'``) lands first so a concurrent repair sees a
    gone preset instead of re-publishing a harness being torn down; the AWS teardown
    then uses the helper ordinary deletes use. If the teardown raises, the row is put
    back to ``failed`` with the reason so the console can retry the uninstall.
    """
    agent = find_installed(db, row.id, preset)
    if agent is None:
        raise NotFoundError("system_agent.not_installed", f"'{preset.key}' is not installed here")
    now = datetime.now(UTC)
    claimed = db.execute(
        update(Agent)
        .where(Agent.id == agent.id, Agent.status.in_(_CLAIMABLE))
        .values(status="deleted", updated_at=now)
    ).rowcount
    db.commit()
    if claimed != 1:
        db.expire_all()
        current = db.get(Agent, agent.id)
        if current is None or current.status == "deleted":
            raise NotFoundError(
                "system_agent.not_installed", f"'{preset.key}' is not installed here"
            )
        raise AppError(
            "agent.deploy_in_progress",
            "a deployment is in progress for this preset — wait for it to finish",
            status_code=409,
        )
    db.refresh(agent)
    try:
        aws_deleted = delete_resources(agent)
    except Exception as exc:
        agent.status = "failed"
        agent.error = f"uninstall failed: {type(exc).__name__}: {exc}"[:500]
        agent.updated_at = datetime.now(UTC)
        db.commit()
        raise
    return {"deleted": True, "agent_id": agent.id, "aws_resource_deleted": aws_deleted}
