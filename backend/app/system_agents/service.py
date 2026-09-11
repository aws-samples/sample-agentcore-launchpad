"""Install / repair / status / uninstall of system-managed presets.

Everything that touches the ledger or AWS for a preset lives here so the router stays a
thin HTTP mapping and the agents router can ask one question (``is_system_agent``).

Invariants this module upholds:

* **No AWS on read.** ``preset_status`` reads the ledger and the workspace row only.
* **Server-owned identity.** The spec is derived from the preset catalogue; the only
  client-influenced values are the administrator's install options.
* **Never adopt.** A live ordinary agent that already holds the reserved name is a
  refusal, not a takeover — an operator removes or renames it first.
* **Idempotent under races.** The partial unique index on
  ``(workspace_id, system_key)`` is the arbiter; the loser of a concurrent install
  re-reads the winner and returns it instead of creating a second row or job.
"""

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.deployer.pipeline import StageContext, StageResult, create_deployment
from app.models.ledger import Agent, Deployment, Job, Workspace
from app.system_agents import presets as catalogue
from app.system_agents.presets import InstallOptions, SystemPreset

logger = logging.getLogger("launchpad.system_agents")

READY = "ready"

STATUS_CONFIGURATION_REQUIRED = "configuration_required"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_DEPLOYING = "deploying"
STATUS_ACTIVE = "active"
STATUS_FAILED = "failed"


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


def workspace_requirements(row: Workspace) -> list[str]:
    """What this workspace still lacks before a preset can be installed."""
    missing: list[str] = []
    if row.bootstrap_status != READY:
        missing.append(f"workspace bootstrap is '{row.bootstrap_status}', needs 'ready'")
    resources = row.resources or {}
    if not resources.get("artifacts_bucket"):
        missing.append("artifacts_bucket missing from the workspace resource map")
    if not resources.get("execution_role_arn"):
        missing.append("execution_role_arn missing from the workspace resource map")
    return missing


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
        .order_by(Deployment.started_at.desc())
        .first()
    )


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
        "can_install": is_admin and not requirements and holder is None,
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
    changed: bool

    @property
    def status_code(self) -> int:
        return 202 if self.job is not None else 200


def _require_ready(row: Workspace) -> str:
    missing = workspace_requirements(row)
    if missing:
        raise AppError(
            "system_agent.workspace_not_ready",
            "this workspace cannot host a system preset yet: " + "; ".join(missing),
            {"requirements": missing, "workspace_id": row.id},
            status_code=409,
        )
    return str(row.resources["artifacts_bucket"])


def install_preset(
    db: Session,
    row: Workspace,
    preset: SystemPreset,
    options: InstallOptions | None,
    *,
    force: bool = False,
) -> InstallOutcome:
    """Install, or repair, one preset in one workspace. Idempotent.

    * not installed → create the row (racing installs collapse onto one) + a create job
    * deploying → return the in-flight job, never a second one
    * active, same version and same options, not forced → no-op (200, no job)
    * failed, changed options, newer bundle, or ``force`` → update job (re-publish)

    The only AWS work this starts is the deploy job on a background thread; the request
    itself makes no cloud call.
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
    except IntegrityError:
        # A concurrent install won the unique index; hand back its row untouched.
        db.rollback()
        winner = find_installed(db, row.id, preset)
        if winner is None:  # pragma: no cover — the index only fires for a live twin
            raise
        return InstallOutcome(
            agent=winner,
            job=None,
            deployment=_latest_deployment(db, winner.id),
            created=False,
            changed=False,
        )
    deployment, job = create_deployment(db, agent)
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
    if agent.status == "deploying":
        # Repeated clicks while a job runs must not stack jobs.
        deployment = _latest_deployment(db, agent.id)
        job = db.get(Job, deployment.job_id) if deployment and deployment.job_id else None
        return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                              changed=False)

    stored = agent.spec or {}
    resolved = options or catalogue.options_from_spec(stored)
    desired = catalogue.build_spec(preset, bucket, resolved).model_dump()
    unchanged = desired == stored
    if agent.status == "active" and unchanged and not force:
        return InstallOutcome(agent=agent, job=None, deployment=_latest_deployment(db, agent.id),
                              created=False, changed=False)

    agent.spec = desired
    agent.status = "deploying"
    agent.error = None
    agent.updated_at = datetime.now(UTC)
    db.flush()
    deployment, job = create_deployment(db, agent, mode="update")
    logger.info("system preset %s: repair job %s for agent %s", preset.key, job.id, agent.id)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                          changed=True)


# ---------------------------------------------------------------------------
# package stage: versioned skill upload (runs inside the deploy job)
# ---------------------------------------------------------------------------


def package_preset_skills(ctx: StageContext, agent: Agent) -> StageResult:
    """Upload the preset's repository bundle to its versioned S3 prefix.

    Every object is sent with a SHA-256 checksum S3 verifies on receipt, so a corrupted
    upload fails the stage instead of silently shipping a broken skill. Re-running the
    stage (resume, repair) rewrites identical bytes — idempotent by construction.
    """
    preset = catalogue.get_preset(agent.system_key or "")
    if preset is None:
        raise RuntimeError(f"unknown system preset key {agent.system_key!r} on agent {agent.id}")
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError("artifacts_bucket missing from this workspace's resource map")
    prefix = preset.skill_prefix()
    digest, files = catalogue.bundle_digest(preset.skill_path())
    bundle = catalogue.load_bundle(preset)
    try:
        s3 = ctx.workspace.client("s3")
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
    """Admin-only teardown: AWS resources through the same helper ordinary deletes use,
    then the ledger row is marked deleted so the partial index frees the key."""
    agent = find_installed(db, row.id, preset)
    if agent is None:
        raise NotFoundError("system_agent.not_installed", f"'{preset.key}' is not installed here")
    if agent.status == "deploying":
        raise AppError(
            "agent.deploy_in_progress",
            "a deployment is in progress for this preset — wait for it to finish",
            status_code=409,
        )
    aws_deleted = delete_resources(agent)
    agent.status = "deleted"
    agent.updated_at = datetime.now(UTC)
    db.commit()
    return {"deleted": True, "agent_id": agent.id, "aws_resource_deleted": aws_deleted}
