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
* **Pinned releases.** The install pins ``{version, digest, files}`` of one validated
  in-memory bundle snapshot on the job **in the same commit** as the job row; the
  package stage refuses to run without that pin, refuses to publish anything else,
  publishes with S3 conditional writes so a competing writer can never replace a
  version, and verifies every uploaded object against the snapshot.
* **Uninstall is a durable job.** The row moves to the non-terminal ``uninstalling``
  status (identity kept, unique index still held) together with an
  ``uninstall_system_agent`` job; only the worker's successful teardown marks it
  ``deleted``. A crash resumes through ``resume_pending_jobs``; a failed teardown is
  retried by another explicit uninstall; install/repair refuse meanwhile.
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
STATUS_UNINSTALLING = "uninstalling"

# Statuses a repair claim may take over (a deploying row belongs to its job, an
# uninstalling row to its teardown).
_CLAIMABLE = ("active", "failed", "draft")
# Statuses an uninstall claim may take over: a failed earlier teardown is retried.
_UNINSTALLABLE = ("active", "failed", "draft", STATUS_UNINSTALLING)

MANIFEST_KEY = ".bundle-manifest.json"
UNINSTALL_JOB_TYPE = "uninstall_system_agent"
_LIVE_JOB = ("queued", "running")


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


def latest_uninstall_job(db: Session, agent_id: str) -> Job | None:
    """The newest uninstall job for this row (jobs carry the agent id in payload)."""
    return _owner_job(db, agent_id)


def _owner_job(db: Session, agent_id: str) -> Job | None:
    """The job that currently owns the row's teardown: the newest uninstall job."""
    rows = (
        db.query(Job)
        .filter(Job.type == UNINSTALL_JOB_TYPE)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .all()
    )
    for job in rows:
        if (job.payload or {}).get("agent_id") == agent_id:
            return job
    return None


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
    elif agent.status == STATUS_UNINSTALLING:
        status = STATUS_UNINSTALLING
    elif agent.status == "active":
        status = STATUS_ACTIVE
    else:
        status = STATUS_FAILED
    deployment = _latest_deployment(db, agent.id) if agent else None
    spec = (agent.spec or {}) if agent else {}
    installed_version = catalogue.skill_version_from_spec(spec) if agent else None
    installed = agent is not None
    settled = installed and agent.status not in ("deploying", STATUS_UNINSTALLING)
    uninstall_job = latest_uninstall_job(db, agent.id) if agent else None
    uninstall_live = bool(uninstall_job and uninstall_job.status in _LIVE_JOB)
    operation = None
    if agent is not None and agent.status == STATUS_UNINSTALLING and uninstall_job is not None:
        operation = {
            "kind": "uninstall",
            "job_id": uninstall_job.id,
            "job_status": uninstall_job.status,
            "attempt": (uninstall_job.payload or {}).get("attempt", 1),
            "error": uninstall_job.error,
            # a failed teardown is retried by another explicit uninstall request
            "retryable": not uninstall_live,
        }
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
        # The in-flight or failed maintenance operation, when one owns the row.
        "operation": operation,
        # Operation-specific server verdicts — the console disables on these, the
        # routes re-check them.
        "can_install": is_admin and not installed and not requirements and holder is None,
        "can_repair": is_admin and settled and not requirements,
        "can_uninstall": is_admin and installed and (
            settled or (agent.status == STATUS_UNINSTALLING and not uninstall_live)
        ),
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


def _require_kb_prerequisites(row: Workspace, options: InstallOptions | None) -> None:
    """Mounting a KB needs the KB gateway's OAuth2 credential provider to exist in
    the workspace resource map: the preset's role is scoped to exactly that provider
    (see agent_iam._preset_kb_oauth_statements), so without it nothing could ever
    fetch a token. Refused at request time, before any row is written."""
    if options and options.knowledge_bases and not (row.resources or {}).get("oauth_provider_arn"):
        raise AppError(
            "system_agent.workspace_not_ready",
            "mounting a knowledge base on the preset requires the workspace's KB gateway "
            "OAuth2 credential provider (oauth_provider_arn) — run the workspace bootstrap",
            {"requirements": [{"code": "missing_oauth_provider",
                               "message": "oauth_provider_arn missing from the resource map"}],
             "workspace_id": row.id},
            status_code=409,
        )


def _release_pin(preset: SystemPreset) -> dict[str, Any]:
    """The exact validated snapshot identity, captured BEFORE the queue commit."""
    return {"preset_bundle": catalogue.snapshot_bundle(preset).release()}


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
    _require_kb_prerequisites(row, options)
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

    pin = _release_pin(preset)  # validated snapshot identity, before anything is written
    spec = catalogue.build_spec(
        preset, bucket, options or InstallOptions(), digest=pin["preset_bundle"]["digest"]
    )
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
        # row + deployment + job + release pin land in ONE commit
        deployment, job = create_deployment(db, agent, payload_extra=pin)
    except IntegrityError:
        # A concurrent install won the unique index; hand back its row AND its job.
        db.rollback()
        winner = find_installed(db, row.id, preset)
        if winner is None:  # pragma: no cover — the index only fires for a live twin
            raise
        return _in_flight(db, winner)
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
    if agent.status == STATUS_UNINSTALLING:
        _refuse_uninstalling(db, agent, preset)

    stored = agent.spec or {}
    resolved = options or catalogue.options_from_spec(stored)
    _require_kb_prerequisites(_workspace_row(db, agent.workspace_id), resolved)
    pin = _release_pin(preset)  # before the claim, so nothing is claimed for a bad bundle
    desired = catalogue.build_spec(
        preset, bucket, resolved, digest=pin["preset_bundle"]["digest"]
    ).model_dump()
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
        if current.status == STATUS_UNINSTALLING:
            _refuse_uninstalling(db, current, preset)
        return _in_flight(db, current)
    db.expire(agent)
    # claim + deployment + job + release pin commit together
    deployment, job = create_deployment(db, agent, mode="update", payload_extra=pin)
    logger.info("system preset %s: repair job %s for agent %s", preset.key, job.id, agent.id)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                          changed=True)


def _workspace_row(db: Session, workspace_id: str | None) -> Workspace:
    row = db.get(Workspace, workspace_id) if workspace_id else None
    if row is None:  # pragma: no cover — a preset row always belongs to a workspace
        raise NotFoundError("workspace.not_found", "workspace not found")
    return row


def _refuse_uninstalling(db: Session, agent: Agent, preset: SystemPreset) -> None:
    job = latest_uninstall_job(db, agent.id)
    raise AppError(
        "system_agent.uninstalling",
        f"'{preset.key}' is being uninstalled (or its last uninstall failed and awaits a "
        "retry); install and repair are refused until the teardown completes",
        {
            "agent_id": agent.id,
            "job_id": job.id if job else None,
            "job_status": job.status if job else None,
            "error": job.error if job else None,
        },
        status_code=409,
    )


# ---------------------------------------------------------------------------
# package stage: versioned, checksummed, pinned skill upload (inside the deploy job)
# ---------------------------------------------------------------------------


def assert_job_release_pinned(
    payload: dict[str, Any] | None, agent: Agent, workspace: WorkspaceContext
) -> dict[str, Any]:
    """Job-entry guard for every system-preset deploy job, run BEFORE any stage —
    including a resumed job whose package stage already succeeded or was skipped.

    The pin must exist and be well-formed; the stored spec, the pin and this build's
    snapshot must describe the same release; and the spec's ``skills`` must be
    exactly the one **complete** expected URI for this workspace —
    ``s3://<workspace artifacts bucket>/system-skills/<preset name>/<version>-<digest12>/``
    — never a legacy plain-version directory, another bucket, another preset's path
    or an extra skill. Reads may still display such a spec; execution fails closed
    with the repair instruction before any AWS write.
    """
    preset = catalogue.get_preset(agent.system_key or "")
    if preset is None:
        raise RuntimeError(f"unknown system preset key {agent.system_key!r} on agent {agent.id}")
    pinned = (payload or {}).get("preset_bundle")
    if (
        not isinstance(pinned, dict)
        or not isinstance(pinned.get("files"), dict)
        or not pinned.get("digest")
        or not pinned.get("version")
    ):
        raise RuntimeError(
            "this job carries no valid release pin (payload.preset_bundle) — it was not "
            "queued by the preset install/repair route; re-run the install from the "
            "System presets panel so the bundle identity is pinned with the job"
        )
    release = catalogue.skill_release_from_spec(agent.spec or {})
    spec_version = release[0] if release else None
    if spec_version != preset.skill_version or pinned.get("version") != preset.skill_version:
        raise RuntimeError(
            f"queued release mismatch: spec pins v{spec_version}, job pins "
            f"v{pinned.get('version')}, this build ships v{preset.skill_version} — re-run "
            "the preset install so spec, pin and bundle are re-derived together"
        )
    snapshot = catalogue.snapshot_bundle(preset)
    if pinned.get("digest") != snapshot.digest or pinned.get("files") != snapshot.file_digests():
        raise RuntimeError(
            f"pinned bundle mismatch: the job was queued for digest "
            f"{str(pinned.get('digest'))[:12]}, the checkout now holds {snapshot.digest[:12]} — "
            "re-run the preset install"
        )
    bucket = (workspace.resources or {}).get("artifacts_bucket") or ""
    if not bucket:
        raise RuntimeError("artifacts_bucket missing from this workspace's resource map")
    expected = preset.skill_uri(bucket, snapshot.digest)
    skills = list((agent.spec or {}).get("skills") or [])
    if skills != [expected]:
        raise RuntimeError(
            f"the stored spec loads {skills or ['<nothing>']} but this workspace's pinned "
            f"release is exactly {expected} — a legacy, foreign or extra skill source is "
            "refused; re-run the preset install so the spec is re-derived"
        )
    return {"pin": pinned, "snapshot": snapshot, "expected_uri": expected}


def _pinned_release(ctx: StageContext) -> dict[str, Any] | None:
    db = ctx.session()
    try:
        job = db.get(Job, ctx.job_id)
        pin = (job.payload or {}).get("preset_bundle") if job else None
        return dict(pin) if isinstance(pin, dict) else None
    finally:
        db.close()


def _s3_error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return type(exc).__name__


_MISSING = ("NoSuchKey", "404", "NotFound")
_PRECONDITION = ("PreconditionFailed", "412")


def _get_bytes(s3: Any, bucket: str, key: str) -> tuple[bytes, str] | None:
    """(body, etag) or None when the object is absent."""
    try:
        res = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — code-dispatched below
        if _s3_error_code(exc) in _MISSING:
            return None
        raise
    return res["Body"].read(), str(res.get("ETag", ""))


def _put_if_absent(s3: Any, bucket: str, key: str, body: bytes, content_type: str) -> bool:
    """Conditional create (``If-None-Match: *``). Returns False on 412 (exists)."""
    try:
        s3.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType=content_type,
            ChecksumSHA256=base64.b64encode(sha256(body).digest()).decode(),
            IfNoneMatch="*",
        )
    except Exception as exc:  # noqa: BLE001
        if _s3_error_code(exc) in _PRECONDITION:
            return False
        raise
    return True


def _put_if_match(
    s3: Any, bucket: str, key: str, body: bytes, content_type: str, etag: str
) -> bool:
    """Conditional replace of a known object (``If-Match: <etag>``)."""
    try:
        s3.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType=content_type,
            ChecksumSHA256=base64.b64encode(sha256(body).digest()).decode(),
            IfMatch=etag,
        )
    except Exception as exc:  # noqa: BLE001
        if _s3_error_code(exc) in _PRECONDITION:
            return False
        raise
    return True


def _content_type(rel: str) -> str:
    return "text/markdown; charset=utf-8" if rel.endswith(".md") else "application/octet-stream"


def _parse_manifest(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _describe_manifest(manifest: Any) -> str:
    if isinstance(manifest, dict):
        return f"digest {str(manifest.get('digest'))[:12]}"
    if manifest is None:
        return "not valid JSON"
    return f"JSON {type(manifest).__name__}, not an object"


def _list_keys(s3: Any, bucket: str, prefix: str) -> set[str]:
    keys: set[str] = set()
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        keys.update(item["Key"] for item in page.get("Contents", []) or [])
        if not page.get("IsTruncated"):
            return keys
        kwargs["ContinuationToken"] = page.get("NextContinuationToken")


def _manifest_matches(manifest: Any, snapshot: catalogue.BundleSnapshot) -> bool:
    return (
        isinstance(manifest, dict)
        and manifest.get("version") == snapshot.version
        and manifest.get("digest") == snapshot.digest
        and isinstance(manifest.get("files"), dict)
        and manifest.get("files") == snapshot.file_digests()
    )


def package_preset_skills(ctx: StageContext, agent: Agent) -> StageResult:
    """Publish (or verify and repair) the preset's bundle under its versioned prefix.

    One immutable byte snapshot drives everything: the same bytes are validated,
    hashed, uploaded and re-read. Ordered checks, all before the first write:

    1. the job MUST carry a release pin (``preset_bundle``); an unpinned or malformed
       pin fails closed — nothing "legacy" is accepted;
    2. the stored spec's version, the pin and this build's snapshot must agree
       (queued release drift and mutated checkouts fail here);
    3. publication is conflict-safe: every object is created with ``If-None-Match: *``;
       a 412 means someone else wrote it, and the existing bytes must equal ours
       (concurrent same-content retry / restart after a partial upload) or the stage
       fails without overwriting anything. The manifest is created last, also
       conditionally; a competing manifest is accepted only when identical;
    4. an existing manifest is trusted only when it matches the snapshot exactly
       (version, digest, per-file digests); a malformed or differing one fails;
    5. every object is then READ BACK and hashed against the snapshot. A missing
       object is restored conditionally; a corrupt one is replaced only with
       ``If-Match`` on the ETag that was read, so a concurrent writer cannot be
       clobbered. Only a fully verified prefix reports success.
    """
    preset = catalogue.get_preset(agent.system_key or "")
    if preset is None:
        raise RuntimeError(f"unknown system preset key {agent.system_key!r} on agent {agent.id}")
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError("artifacts_bucket missing from this workspace's resource map")

    db = ctx.session()
    try:
        job = db.get(Job, ctx.job_id)
        payload = dict(job.payload or {}) if job else {}
    finally:
        db.close()
    # same guard the job entry runs; its snapshot is the one immutable copy this
    # stage validates against, uploads and reads back
    snapshot = assert_job_release_pinned(payload, agent, ctx.workspace)["snapshot"]

    # The Harness loads exactly the directory the spec names; publish there and nowhere
    # else. The content-addressed name ties the directory to this snapshot.
    prefix = catalogue.skill_prefix_from_spec(agent.spec or {}) or preset.skill_prefix(
        snapshot.digest
    )
    if prefix != preset.skill_prefix(snapshot.digest):
        raise RuntimeError(
            f"the stored spec loads {prefix} but this snapshot publishes to "
            f"{preset.skill_prefix(snapshot.digest)} — re-run the preset install"
        )
    s3 = ctx.workspace.client("s3")
    manifest_key = f"{prefix}{MANIFEST_KEY}"

    published = _get_bytes(s3, bucket, manifest_key)
    if published is not None:
        manifest = _parse_manifest(published[0])
        if not _manifest_matches(manifest, snapshot):
            raise RuntimeError(
                f"s3://{bucket}/{prefix} already carries a manifest that is malformed or "
                f"describes different content ({_describe_manifest(manifest)} ≠ digest "
                f"{snapshot.digest[:12]}); a published release is immutable — inspect the "
                "prefix (nothing was written), then bump the preset's skill_version (and "
                "the SKILL.md version) to publish a new release"
            )

    # ---- objects: create-if-absent, else verify existing bytes; repair corrupt ----
    created: list[str] = []
    repaired: list[str] = []
    for rel, body in snapshot.files.items():
        key = f"{prefix}{rel}"
        if _put_if_absent(s3, bucket, key, body, _content_type(rel)):
            created.append(rel)
            continue
        existing = _get_bytes(s3, bucket, key)
        if existing is None:  # deleted between our 412 and the read — try once more
            if not _put_if_absent(s3, bucket, key, body, _content_type(rel)):
                raise RuntimeError(f"s3://{bucket}/{key} keeps changing underneath the publish")
            created.append(rel)
            continue
        if existing[0] == body:
            continue  # same content already there (concurrent retry / restart)
        if published is None:
            raise RuntimeError(
                f"s3://{bucket}/{key} already holds different bytes and no manifest claims "
                f"the prefix — a competing publication is in progress; refusing to overwrite"
            )
        # manifest says this version has OUR bytes → the object is corrupt; restore it
        # against the exact ETag we read so a concurrent writer is never clobbered
        if not _put_if_match(s3, bucket, key, body, _content_type(rel), existing[1]):
            raise RuntimeError(f"s3://{bucket}/{key} changed while being repaired; re-run")
        repaired.append(rel)
        ctx.log(f"repaired corrupt object s3://{bucket}/{key}")

    # ---- manifest: create-if-absent; a competing manifest must be identical ----
    if published is None:
        manifest_body = json.dumps(
            {"name": preset.name, **snapshot.release(),
             "published_at": datetime.now(UTC).isoformat()},
            ensure_ascii=False,
        ).encode()
        if not _put_if_absent(s3, bucket, manifest_key, manifest_body, "application/json"):
            other = _get_bytes(s3, bucket, manifest_key)
            other_manifest = _parse_manifest(other[0]) if other else None
            if not _manifest_matches(other_manifest, snapshot):
                raise RuntimeError(
                    f"a competing publication claimed s3://{bucket}/{prefix} with different "
                    "content while this job was uploading; refusing to overwrite it"
                )

    # ---- verification: the whole directory, the manifest and every object ----
    expected = {f"{prefix}{rel}" for rel in snapshot.files} | {manifest_key}
    listed = _list_keys(s3, bucket, prefix)
    unexpected = sorted(listed - expected)
    if unexpected:
        # never deleted here: they are not ours to remove and the Harness would load them
        raise RuntimeError(
            f"s3://{bucket}/{prefix} holds {len(unexpected)} object(s) outside the pinned "
            f"snapshot ({', '.join(k[len(prefix):] for k in unexpected[:5])}) — the "
            "release directory is not exactly the validated snapshot; inspect and remove "
            "the foreign objects, then re-run the install"
        )
    final_manifest = _get_bytes(s3, bucket, manifest_key)
    final_ok = final_manifest is not None and _manifest_matches(
        _parse_manifest(final_manifest[0]), snapshot
    )
    if not final_ok:
        raise RuntimeError(
            f"post-upload verification failed for s3://{bucket}/{manifest_key}: the manifest "
            "is missing or does not describe the pinned snapshot"
        )
    for rel, body in snapshot.files.items():
        key = f"{prefix}{rel}"
        readback = _get_bytes(s3, bucket, key)
        if readback is None or sha256(readback[0]).hexdigest() != sha256(body).hexdigest():
            raise RuntimeError(
                f"post-upload verification failed for s3://{bucket}/{key}: the object is "
                f"{'missing' if readback is None else 'different from the snapshot'}"
            )
    for rel in created:
        ctx.log(f"uploaded s3://{bucket}/{prefix}{rel}")
    action = (
        "published" if published is None
        else ("repaired" if (repaired or created) else "already published · verified")
    )
    detail = (
        f"skill bundle {preset.name} v{preset.skill_version} {action} · "
        f"{len(snapshot.files)} files · sha256 {snapshot.digest[:12]} → s3://{bucket}/{prefix}"
    )
    ctx.log(detail)
    return StageResult(detail=detail)


# ---------------------------------------------------------------------------
# uninstall: durable claim + job (the worker lives in system_agents/uninstall.py)
# ---------------------------------------------------------------------------


@dataclass
class UninstallOutcome:
    agent: Agent
    job: Job
    started: bool  # True ⇔ this call created the job (caller launches the worker)
    attempt: int


def uninstall_preset(db: Session, row: Workspace, preset: SystemPreset) -> UninstallOutcome:
    """Claim the row for teardown and queue the uninstall job. Idempotent.

    The claim is the non-terminal ``uninstalling`` status: the row keeps its system
    identity (and the partial unique index), so no install or repair can take the
    key while the AWS resources are still being removed. Claim and job commit
    together. A live job is returned as-is; a failed one is retried with a new job;
    only the worker's successful teardown marks the row ``deleted``.
    """
    agent = find_installed(db, row.id, preset)
    if agent is None:
        raise NotFoundError("system_agent.not_installed", f"'{preset.key}' is not installed here")
    if agent.status == "deploying":
        raise AppError(
            "agent.deploy_in_progress",
            "a deployment is in progress for this preset — wait for it to finish",
            status_code=409,
        )
    previous = latest_uninstall_job(db, agent.id)
    live = previous is not None and previous.status in _LIVE_JOB
    if agent.status == STATUS_UNINSTALLING and live:
        return UninstallOutcome(agent=agent, job=previous, started=False,
                                attempt=(previous.payload or {}).get("attempt", 1))
    attempt = ((previous.payload or {}).get("attempt", 0) + 1) if previous else 1
    now = datetime.now(UTC)
    # Optimistic CAS: the row version is its updated_at as this session read it. Two
    # simultaneous requests (initial or failed-retry) read the same value; the first
    # UPDATE moves it, the second matches 0 rows and adopts the first's job.
    seen = agent.updated_at
    claimed = db.execute(
        update(Agent)
        .where(
            Agent.id == agent.id,
            Agent.status.in_(_UNINSTALLABLE),
            Agent.updated_at == seen,
        )
        .values(status=STATUS_UNINSTALLING, updated_at=now)
    ).rowcount
    if claimed != 1:
        db.rollback()
        db.expire_all()
        current = db.get(Agent, agent.id)
        if current is None or current.status == "deleted":
            raise NotFoundError(
                "system_agent.not_installed", f"'{preset.key}' is not installed here"
            )
        if current.status == STATUS_UNINSTALLING:
            owner = _owner_job(db, current.id)
            if owner is not None and owner.status in _LIVE_JOB:
                return UninstallOutcome(agent=current, job=owner, started=False,
                                        attempt=(owner.payload or {}).get("attempt", 1))
        raise AppError(
            "agent.deploy_in_progress",
            "the preset changed underneath this request — reload and retry",
            status_code=409,
        )
    # a new attempt carries forward only steps the previous one VERIFIED done; the
    # worker re-checks that each carried step names the same exact resource id
    prior_progress = ((previous.payload or {}).get("progress") or {}) if previous else {}
    carried: dict[str, Any] = {}
    for step, entry in prior_progress.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("state") == "done":
            carried[step] = entry  # skip-eligible (the worker re-checks the resource id)
            continue
        # A failed/pending step runs again — but against the SAME pinned resource:
        # identity is carried, completion is not.
        identity = {k: entry[k] for k in ("target_id", "gateway_id", "resource_id") if entry.get(k)}
        if identity:
            carried[step] = {**identity, "state": "pinned",
                             "detail": f"identity carried from attempt {attempt - 1}"}
    payload: dict[str, Any] = {"agent_id": agent.id, "preset_key": preset.key, "attempt": attempt}
    if carried:
        payload["progress"] = carried
    job = Job(workspace_id=row.id, type=UNINSTALL_JOB_TYPE, payload=payload)
    db.add(job)
    db.commit()  # claim + job together
    db.refresh(agent)
    logger.info("system preset %s: uninstall job %s (attempt %s) for agent %s",
                preset.key, job.id, attempt, agent.id)
    return UninstallOutcome(agent=agent, job=job, started=True, attempt=attempt)
