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
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.deployer.pipeline import StageContext, StageResult, create_deployment
from app.models.ledger import Agent, Deployment, Job, Workspace
from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec, catalog_version
from app.services import agent_iam, canary_references
from app.services.workspace import WorkspaceContext
from app.system_agents import presets as catalogue
from app.system_agents.presets import InstallOptions, PresetEdit, SystemPreset

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
    """Router-side guard for rows that *reference* an agent (canaries)."""
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
        "protected_actions": ["redeploy", "delete", "convert", "canary"],
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


def _latest_deployment(
    db: Session, agent_id: str, *, exclude_failed: bool = False
) -> Deployment | None:
    """The newest deployment row; with ``exclude_failed`` the newest one that did NOT fail
    (running or succeeded) — the one whose rendered snapshot the Runtime can be serving.
    N17-obs259: a failed re-publish is pinned at the new catalog by the pipeline before its
    `generate` stage runs (or not pinned at all when it failed earlier), while the live
    version still serves the previous snapshot; reading that row's pin masked the preset as
    current."""
    query = db.query(Deployment).filter(Deployment.agent_id == agent_id)
    if exclude_failed:
        query = query.filter(Deployment.status != "failed")
    return query.order_by(Deployment.started_at.desc(), Deployment.id.desc()).first()


def serving_pin(db: Session, agent_id: str) -> int | None:
    """The model-catalog version the preset's snapshot was rendered from: the pin of the
    newest NON-failed deployment (None before one). The one source for `catalog_behind`,
    the status read's `catalog_version.installed` and the banner (N17-obs234 / obs259)."""
    deployment = _latest_deployment(db, agent_id, exclude_failed=True)
    return deployment.catalog_version if deployment else None


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


# Per-process memo of ``release_info`` (review #5): the bundle / template files are
# immutable for the life of the process, and ``GET /api/system-agents`` is polled every
# 4 s by the presets panel — re-hashing them on every read is waste. Keyed by
# ``(preset key, published model-catalog version)``: the rendered SKILL.md of a Harness
# preset states the catalog, so a registry write (D21) moves the digest — a memo keyed on
# the preset alone kept answering the pre-write tag until a restart (N-ARCH-obs19).
# Successes only: a bundle that failed to validate is re-tried on the next read so a fix
# (a registry write, a new build) is seen.
_RELEASE_INFO: dict[tuple[str, int], dict[str, Any]] = {}

E_BUNDLE_INVALID = "system_agent.bundle_invalid"


def _release_outcome(preset: SystemPreset) -> tuple[dict[str, Any] | None, str | None]:
    """``(release, None)`` when this build's release of the preset validates,
    ``(None, message)`` when it does not — the one place the bundle / template is proven
    for a status read. Never raises."""
    key = (preset.key, int(catalog_version()))
    cached = _RELEASE_INFO.get(key)
    if cached is not None:
        return dict(cached), None
    try:
        if preset.template:
            rel = catalogue.template_release(preset)
            info = {"version": rel["version"], "digest": str(rel["digest"])[:12],
                    "tag": rel["tag"]}
        else:
            rel = catalogue.bundle_release(preset)
            digest = str(rel["digest"])[:12]
            info = {"version": rel["version"], "digest": digest,
                    "tag": f"{rel['version']}-{digest}"}
    except Exception as exc:  # noqa: BLE001 — a read must not 500 on a bad bundle
        logger.warning("system preset %s: release unavailable: %s", preset.key, exc)
        return None, str(exc)
    # one entry per preset — the newest catalog version wins (bounded memo, rev-r11 nit)
    for stale in [k for k in _RELEASE_INFO if k[0] == preset.key]:
        del _RELEASE_INFO[stale]
    _RELEASE_INFO[key] = info
    return dict(info), None


def release_info(preset: SystemPreset) -> dict[str, Any] | None:
    """This build's release of the preset — what an install pins and what a repair
    compares the stored spec against: ``{version, digest (12 hex), tag}``. The template
    release of a ``zip_runtime`` preset, the skill bundle snapshot of a Harness one. A
    bundle this build cannot validate yields ``None`` (``release_error`` carries why; the
    install route answers ``409 system_agent.bundle_invalid``); a status read never fails
    on it. Memoised per preset key and catalog version. (N12-obs31 c)"""
    return _release_outcome(preset)[0]


def release_error(preset: SystemPreset) -> str | None:
    """Why this build cannot release the preset (``None`` when it can) — the message the
    install / repair route would refuse with (N-ARCH-obs19)."""
    return _release_outcome(preset)[1]


def current_catalog_version(db: Session) -> int:
    """N17-obs281 (C1): THE "current model-catalog version" of every preset staleness signal —
    the ledger stamp (`model_registry.stamp`): `catalog_behind` (banner, `snapshot_behind`,
    the repair decision) and the status read's `catalog_version.current`. Before, the status
    read took `current` from the in-process snapshot while `catalog_behind` read the ledger,
    so a second process could show "current v12" next to `snapshot_behind: true` for ≤ 30 s
    (model-registry.md §9: the behind signals never disagree). The snapshot's
    `catalog_version()` stays what a render pins (`Deployment.catalog_version`) and the
    release memo key — that is the version the rendered bundle states, not a staleness read."""
    from app.services import model_registry

    return model_registry.stamp(db)


def catalog_behind(db: Session, agent: Agent) -> tuple[int, int] | None:
    """``(installed, current)`` when the preset's newest NON-failed deployment pinned an
    OLDER model catalog than the ledger's (`current_catalog_version`), else None. The ONE
    predicate behind the `preset_snapshot_behind` banner, the status read's `snapshot_behind`
    and the repair decision (N17-obs234): a row without a pin (no `generate` stage succeeded
    yet) or a pin equal to / newer than the stamp is never behind. Failed deployments are
    skipped (N17-obs259): their pin describes a snapshot the Runtime never received."""
    installed = serving_pin(db, agent.id)
    if installed is None:
        return None
    current = current_catalog_version(db)
    if int(installed) >= current:
        return None
    return int(installed), int(current)


def model_pinned(agent: Agent) -> bool:
    """Whether the preset row's model is an administrator's explicit choice (N17-obs202).

    ``Agent.preset_model_pinned`` records it from this build on; a row written before the
    flag is pinned iff its stored model differs from the then-only default (the built-in
    constant) — exactly the two states a pre-flag row could be in. Not pinned = the row
    FOLLOWS the ``agent_default`` model role: a repair re-resolves the model."""
    if agent.preset_model_pinned is not None:
        return bool(agent.preset_model_pinned)
    return str((agent.spec or {}).get("model_id") or "") not in ("", DEFAULT_MODEL_ID)


def default_changed(agent: Agent | None, preset: SystemPreset) -> bool:
    """``True`` when an installed, role-following preset's stored model is not what the
    ``agent_default`` role resolves to NOW — the next repair re-publishes it with the new
    model (the presets panel's "model default changed — repair to apply" note). Never
    true for a pinned row: an override wins until reset."""
    if agent is None or model_pinned(agent):
        return False
    return str((agent.spec or {}).get("model_id") or "") != preset.resolved_model_id()


def catalog_snapshot_warnings(db: Session) -> list[dict[str, Any]]:
    """D21 (M1-d): one `preset_snapshot_behind` warning per installed preset whose latest
    deployment pinned an OLDER model catalog than the ledger's (`model_registry.stamp`):
    its rendered snapshot (eval worker tables, architect SKILL catalog lines) is stale until
    a repair. Registered as a `model_registry.warnings` producer from `app/main.py`."""
    from app.services import model_registry

    out: list[dict[str, Any]] = []
    rows = (
        db.query(Agent)
        .filter(Agent.system_key.isnot(None), Agent.status != "deleted")
        .order_by(Agent.workspace_id, Agent.system_key)
        .all()
    )
    for agent in rows:
        behind = catalog_behind(db, agent)
        if behind is None:
            continue
        installed, current = behind
        out.append({
            "code": model_registry.W_PRESET_SNAPSHOT_BEHIND,
            "message": (
                f"system preset {agent.system_key}: the installed snapshot states model "
                f"catalog v{installed}, the current catalog is v{current} — open "
                "New agent → System presets and repair the snapshot"
            ),
            "detail": {"installed": int(installed), "current": int(current),
                       "preset_key": agent.system_key, "workspace_id": agent.workspace_id,
                       "agent_id": agent.id},
        })
    return out


def installed_release(spec: dict[str, Any]) -> str | None:
    """The release tag (``<version>-<digest12>``, or the bare version for a pre-digest
    install) the stored spec pins — the counterpart of ``release_info()['tag']``."""
    tag = catalogue.template_release_from_spec(spec)
    if tag:
        return tag
    rel = catalogue.skill_release_from_spec(spec)
    if rel:
        return f"{rel[0]}-{rel[1]}" if rel[1] else rel[0]
    return None


def _job_summary(job: Job | None) -> dict[str, Any] | None:
    if job is None:
        return None
    payload = job.payload or {}
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        # N17-obs206 (additive): who asked for a repair (`auto` = the sweep, null = a card /
        # API action) and why — the card renders a deploying `auto` job as 自动修复中
        "trigger": payload.get("repair_trigger"),
        "repair_reason": payload.get("repair_reason"),
    }


def _newest(*jobs: Job | None) -> Job | None:
    # SQLite hands a reloaded row a naive UTC datetime and a row created in this session
    # an aware one; both are UTC, so compare them stripped of tzinfo.
    def key(j: Job) -> tuple[datetime, str]:
        at = j.created_at or datetime.min
        return (at.replace(tzinfo=None), j.id)

    live = [j for j in jobs if j is not None]
    return max(live, key=key) if live else None


def job_belongs_to_preset(db: Session, job: Job, workspace_id: str, preset: SystemPreset) -> bool:
    """Whether ``job`` is one of this preset's deploy/uninstall jobs in ``workspace_id`` —
    judged by the agent row it names, not by ``job.workspace_id`` (a preset's install job
    can be filed under a system workspace id the agents API does not expose, N12-obs31 a).
    Rows an uninstall already marked ``deleted`` still count, so the last uninstall's log
    stays readable."""
    agent = db.get(Agent, (job.payload or {}).get("agent_id") or "")
    return (
        agent is not None
        and agent.system_key == preset.key
        and agent.workspace_id == workspace_id
        and job.type in ("deploy_agent", UNINSTALL_JOB_TYPE)
    )


def preset_status(
    db: Session, row: Workspace, preset: SystemPreset, *, is_admin: bool
) -> dict[str, Any]:
    """One preset's state in one workspace. Ledger + workspace row only — no AWS."""
    # resolved here: skill_registry imports this module for the S3 read helpers
    from app.services import model_roles
    from app.system_agents import skill_registry

    model_roles.ensure_loaded(db)  # rev-r25 F1: `default_changed` judged on the ledger's roles
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
    deploy_job = db.get(Job, deployment.job_id) if deployment and deployment.job_id else None
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
            # N10v2-bug8: what the attempt verified gone / never manages / stopped at
            **{
                key: list(((uninstall_job.payload or {}).get("teardown") or {}).get(key, []))
                for key in ("removed", "skipped", "failed")
            },
        }
    release, release_error_message = _release_outcome(preset)
    return {
        "key": preset.key,
        "name": preset.name,
        "label": preset.label,
        "description": preset.description,
        "method": preset.method,
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
        # N10v3-bug7: a failed repair of an ACTIVE preset keeps it serving; `error` then
        # describes that failed attempt, not the live version
        "last_deploy_failed": bool(agent and agent.status == "active" and agent.error),
        "job_id": deployment.job_id if deployment else None,
        "deployment_id": deployment.id if deployment else None,
        "deployment_status": deployment.status if deployment else None,
        # N12-obs31 (c): the live runtime version and the releases — this build's (what
        # a repair compares against) and the one the stored spec pins
        "version": agent.version if agent else None,
        "release": release,
        # N-ARCH-obs19: why `release` is null — this build cannot validate / render the
        # preset's bundle against the published catalog; install and repair refuse with
        # `409 system_agent.bundle_invalid` carrying the same message
        "release_error": release_error_message,
        "installed_release": installed_release(spec) if agent else None,
        # D21 (M1-d): the model catalog the SERVING snapshot was rendered from (the newest
        # non-failed deployment's pin, N17-obs259; None before one) vs the published one —
        # the presets panel's "snapshot states catalog v12, current v14 → Repair" banner
        "catalog_version": {
            "installed": serving_pin(db, agent.id) if agent else None,
            "current": current_catalog_version(db),  # N17-obs281: catalog_behind's source
        },
        # N17-obs234: the ledger verdict the repair decision and the banner share — a
        # bodiless install / repair of such a preset re-publishes WITHOUT `force`, so the
        # card's "Repair snapshot" action can never answer "already current"
        "snapshot_behind": (agent is not None and catalog_behind(db, agent) is not None),
        # N12-obs31 (a): the preset's current/last job (install, repair or uninstall);
        # its log is read through GET /api/system-agents/{key}/jobs/{id} (admin)
        "last_job": _job_summary(_newest(deploy_job, uninstall_job)),
        "model_id": spec.get("model_id"),
        "model_source": spec.get("model_source"),
        # N17-obs202: whether `model_id` follows the model-defaults page (`default` — the
        # `agent_default` role at the last install / repair) or is an administrator's pin
        # (`override`, set through the settings edit; `null` when not installed), and
        # whether a following row's model lags the role as it stands now — the card's
        # "model default changed — repair to apply" note; the repair re-publishes it
        "model_origin": (
            ("override" if model_pinned(agent) else "default") if agent is not None else None
        ),
        "default_changed": default_changed(agent, preset),
        "knowledge_bases": spec.get("knowledge_bases") or [],
        "allowed_tools": spec.get("allowed_tools") or list(preset.allowed_tools),
        "memory": "disabled",
        # The administrator-editable members: what is stored (``{}`` when not
        # installed) and what this build would install by default. Members read the
        # same projection; only the maintenance route (admin) can change it.
        "settings": catalogue.effective_settings(preset, spec if agent else None),
        "defaults": catalogue.default_settings(preset),
        "editable_fields": list(catalogue.EDITABLE_FIELDS),
        # The in-flight or failed maintenance operation, when one owns the row.
        "operation": operation,
        # Operation-specific server verdicts — the console disables on these, the
        # routes re-check them.
        "can_install": is_admin and not installed and not requirements and holder is None,
        "can_repair": is_admin and settled and not requirements,
        # editing the stored settings = a repair with an explicit body
        "can_configure": is_admin and settled and not requirements,
        "can_uninstall": is_admin and installed and (
            settled or (agent.status == STATUS_UNINSTALLING and not uninstall_live)
        ),
        # SE-043: the preset's Skill as its own Registry record — the ledger mapping
        # (identifiers + registered release; approval is read from AWS in the
        # Registry) and the admin verdict for the register/verify action.
        "skill_registration": skill_registry.status_projection(
            db, row.id, (row.resources or {}).get("registry_id"), preset
        ),
        "can_register_skill": (
            is_admin and installed and agent.status == "active" and not requirements
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
    """Mounting a KB needs a knowledge-base backend registered in this build: the
    preset gets the same direct ``kb_search``/``kb_deep_search`` tool grants as any
    other agent (R13/R16 — the KB-gateway OAuth path has no CN implementation and is
    gone), so without a backend nothing could ever answer the mount. Refused at
    request time, before any row is written."""
    from app.services import knowledge_backend

    if options and options.knowledge_bases and knowledge_backend.backend_or_none() is None:
        raise AppError(
            "system_agent.workspace_not_ready",
            "mounting a knowledge base on the preset requires a knowledge-base backend "
            "to be registered in this build",
            {"requirements": [{"code": "missing_knowledge_backend",
                               "message": "no knowledge-base backend is registered"}],
             "workspace_id": row.id},
            status_code=409,
        )


def validate_options(preset: SystemPreset, options: InstallOptions | None) -> None:
    """Prove the administrator's choices produce a valid preset spec — before any
    row, job or AWS call. The bucket and digest are placeholders: only the spec's
    own field and cross-field rules (bounds, an OpenAI-only reasoning effort, …) are
    under test here."""
    if options is None:
        return
    try:
        catalogue.build_spec(preset, "validation-placeholder", options)
    except ValidationError as exc:
        errors = [
            {"loc": list(e.get("loc") or ()), "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        raise AppError(
            "system_agent.invalid_options",
            "the requested preset settings are not valid: "
            + "; ".join(e["msg"] for e in errors),
            {"errors": errors, "preset": preset.key},
            status_code=422,
        ) from exc


def _refuse_deploy_in_progress(db: Session, agent: Agent, preset: SystemPreset) -> None:
    """An explicit settings change while a deploy job owns the row is refused, never
    coalesced: the in-flight job carries the *previous* spec, so answering with its id
    would silently drop the administrator's accepted-looking update. A bodiless
    repair click keeps coalescing onto the in-flight job (``_in_flight``)."""
    deployment, job = _latest_job(db, agent)
    raise AppError(
        "system_agent.deploy_in_progress",
        f"'{preset.key}' is being deployed; retry the settings change once the current "
        "job has finished",
        {
            "agent_id": agent.id,
            "job_id": job.id if job else None,
            "job_status": job.status if job else None,
            "deployment_id": deployment.id if deployment else None,
        },
        status_code=409,
    )


def _release_pin(preset: SystemPreset) -> dict[str, Any]:
    """The exact validated release identity, captured BEFORE the queue commit: the skill
    bundle snapshot of a Harness preset, the template release of a ``zip_runtime`` one.

    A bundle this build cannot validate or render (the SKILL.md fails the catalog check,
    a stale version pin, an over-sized bundle, a broken template) is a structured
    ``409 system_agent.bundle_invalid`` — never a 500: nothing has been claimed or
    written yet, the status read exposes the same message as ``release_error``, and the
    administrator acts on the registry or the build (N-ARCH-obs19)."""
    try:
        if preset.template:
            rel = catalogue.template_release(preset)
            return {"preset_bundle": {"version": rel["version"], "digest": rel["digest"],
                                      "files": rel["files"], "template": preset.template}}
        return {"preset_bundle": catalogue.snapshot_bundle(preset).release()}
    except Exception as exc:  # noqa: BLE001 — a bad bundle is a verdict, not a crash
        logger.warning("system preset %s: release unavailable: %s", preset.key, exc)
        raise AppError(
            E_BUNDLE_INVALID,
            f"this build cannot publish preset '{preset.key}' {preset.skill_version}: {exc}",
            {"preset_key": preset.key, "skill_version": preset.skill_version,
             "catalog_version": int(catalog_version()), "reason": str(exc)},
            status_code=409,
        ) from exc


def _in_flight(db: Session, agent: Agent) -> InstallOutcome:
    deployment, job = _latest_job(db, agent)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                          changed=False)


def install_preset(
    db: Session,
    row: Workspace,
    preset: SystemPreset,
    edit: PresetEdit | None,
    *,
    force: bool = False,
    trigger: str | None = None,
) -> InstallOutcome:
    """Install, or repair, one preset in one workspace. Idempotent.

    * not installed → row + create job (a racing twin collapses onto the winner and
      returns the winner's job — unless it asked for *different* settings, which is
      ``409 system_agent.deploy_in_progress`` rather than a false acceptance)
    * deploying, no explicit edit → the in-flight job, never a second one;
      deploying with an explicit edit → ``409 system_agent.deploy_in_progress``
    * active, same version and same settings, not forced → no-op (200, no job) — unless
      the latest deployment pinned an OLDER model catalog than the ledger's
      (``catalog_behind``, N17-obs234): that re-publishes exactly like ``force``
    * failed, changed settings, newer bundle, a behind catalog snapshot, or ``force`` →
      update job (re-publish)

    ``edit=None`` means "the preset defaults" on a first install and "exactly what is
    stored" on a repair. The only AWS work this starts is the deploy job on a
    background thread; the request itself makes no cloud call.

    ``trigger`` (N17-obs206) names a non-interactive caller of a repair — ``auto`` for the
    sweep; it rides on the job payload
    (``repair_trigger``) and prefixes ``repair_reason`` so the status read can say so. It
    changes no decision.
    """
    from app.services import model_roles

    # rev-r25 F1 (N17-obs202): resolve the model roles from the LEDGER, whatever process this
    # is — the bootstrap CLI never ran `load_overrides`, and a role-following preset must
    # never be re-published on env / built-in values a console override replaced
    model_roles.ensure_loaded(db)
    bucket = _require_ready(row)
    if edit is not None and edit.is_empty():
        edit = None
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
        return _repair(db, existing, preset, bucket, edit, force=force, trigger=trigger)

    options = edit.resolve(preset, None) if edit else preset.default_options()
    validate_options(preset, options)
    _require_kb_prerequisites(row, options)
    pin = _release_pin(preset)  # validated snapshot identity, before anything is written
    spec = catalogue.build_spec(preset, bucket, options, digest=pin["preset_bundle"]["digest"])
    agent = Agent(
        workspace_id=row.id,
        name=preset.name,
        method=preset.method,
        status="deploying",
        spec=spec.model_dump(),
        # no creator: a preset is server-owned (`system_key`), whoever asked to install it
        system_key=preset.key,
        # N17-obs202: an explicit `model_id` in the install body pins the model; otherwise
        # the row follows the `agent_default` role (`spec.model_id` = the role's value now)
        preset_model_pinned=options.model_id is not None,
    )
    db.add(agent)
    try:
        db.flush()
        # row + deployment + job + release pin land in ONE commit
        deployment, job = create_deployment(db, agent, payload_extra=pin)
    except IntegrityError:
        # A concurrent install won the unique index; hand back its row AND its job —
        # but only when it installed what we asked for. A twin that asked for other
        # settings must not be told "accepted" while the winner's job carries the
        # winner's values.
        db.rollback()
        winner = find_installed(db, row.id, preset)
        if winner is None:  # pragma: no cover — the index only fires for a live twin
            raise
        if edit is not None and _normalized(winner.spec or {}) != spec.model_dump():
            _refuse_deploy_in_progress(db, winner, preset)
        return _in_flight(db, winner)
    logger.info("system preset %s: install job %s in workspace %s", preset.key, job.id, row.id)
    return InstallOutcome(agent=agent, job=job, deployment=deployment, created=True, changed=True)


_CLAIM_ATTEMPTS = 3


def _repair(
    db: Session,
    agent: Agent,
    preset: SystemPreset,
    bucket: str,
    edit: PresetEdit | None,
    *,
    force: bool,
    trigger: str | None = None,
) -> InstallOutcome:
    """Re-publish an installed preset, resolving the partial ``edit`` against the row
    **as it is claimed**.

    The claim is one UPDATE conditioned on the status being taken over AND on the
    row version (``updated_at``) this attempt resolved against. A row that changed
    in between — a concurrent edit that was accepted and then finished (active
    again, new spec), or anything that moved it while this attempt was pinning the
    bundle — fails the compare-and-set: the attempt re-reads the row and resolves
    the same partial edit again on the new state, so a member this edit omits can
    never be reverted to a stale value. A row that is meanwhile ``deploying`` is a
    ``409`` for an explicit edit (its job carries someone else's values) and the
    in-flight job for a bodiless repair.
    """
    for _attempt in range(_CLAIM_ATTEMPTS):
        db.refresh(agent)  # never decide on a stale ORM snapshot
        if agent.status == "deploying":
            if edit is not None:
                _refuse_deploy_in_progress(db, agent, preset)
            return _in_flight(db, agent)  # repeated clicks while a job runs stack nothing
        if agent.status == STATUS_UNINSTALLING:
            _refuse_uninstalling(db, agent, preset)

        stored = dict(agent.spec or {})
        seen = agent.updated_at  # the row version this resolution is valid for
        # N17-obs202: a row that follows the `agent_default` role resolves its model again
        # (`model_id=None` → `build_spec` reads the role NOW); a pinned row keeps its pin
        pinned = model_pinned(agent)
        resolved = (
            edit.resolve(preset, stored, model_pinned=pinned) if edit
            else catalogue.options_from_spec(stored, model_pinned=pinned)
        )
        validate_options(preset, resolved)  # against the CURRENT row, not a request snapshot
        _require_kb_prerequisites(_workspace_row(db, agent.workspace_id), resolved)
        pin = _release_pin(preset)  # before the claim, so nothing is claimed for a bad bundle
        desired = catalogue.build_spec(
            preset, bucket, resolved, digest=pin["preset_bundle"]["digest"]
        ).model_dump()
        # Compare through the schema so a row written before a spec member existed
        # (which simply lacks the key) equals the same choices re-derived today.
        # N17-obs234: an unchanged spec whose latest deployment pinned an OLDER model
        # catalog than the ledger's is NOT current — the rendered snapshot (eval worker
        # tables, SKILL catalog lines) is stale, so the repair re-publishes exactly like
        # `force` and records why. Equal / newer pins with an unchanged spec stay a no-op.
        behind = catalog_behind(db, agent)
        spec_changed = desired != _normalized(stored)
        if agent.status == "active" and not force and not spec_changed and behind is None:
            deployment, job = _latest_job(db, agent)
            return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                                  changed=False)
        reasons = []
        if behind is not None:
            reasons.append(f"catalog snapshot behind v{behind[0]} < v{behind[1]}")
        if (resolved.model_id is None and edit is None
                and desired.get("model_id") != stored.get("model_id")):
            # N17-obs202: a bodiless repair of a role-following row whose model default moved
            reasons.append(
                f"model default changed {stored.get('model_id')} -> {desired.get('model_id')}"
            )
        reason = "; ".join(reasons) or None
        if trigger is not None:
            # N17-obs206: a non-interactive repair records who asked and why (a newer bundle
            # or an added spec member re-derives a different spec; `force` is the rest)
            reason = f"{trigger}: " + (
                reason or ("release update" if spec_changed else "forced re-publish")
            )

        # Durable claim: status AND version, in the same transaction as the job row.
        now = datetime.now(UTC)
        claimed = db.execute(
            update(Agent)
            .where(
                Agent.id == agent.id,
                Agent.status.in_(_CLAIMABLE),
                Agent.updated_at == seen,
            )
            .values(status="deploying", spec=desired, error=None, updated_at=now,
                    # N17-obs202: the edit may pin (`model_id` set), unpin (`reset`) or keep
                    preset_model_pinned=resolved.model_id is not None)
        ).rowcount
        if claimed == 1:
            db.expire(agent)
            # claim + deployment + job + release pin commit together; the behind reason
            # rides on the job payload and the spec snapshot (`note`) as the journal
            deployment, job = create_deployment(
                db, agent, mode="update",
                payload_extra={
                    **pin,
                    **({"repair_reason": reason} if reason else {}),
                    **({"repair_trigger": trigger} if trigger else {}),
                },
                note=reason,
            )
            logger.info(
                "system preset %s: repair job %s for agent %s%s", preset.key, job.id, agent.id,
                f" ({reason})" if reason else "",
            )
            return InstallOutcome(agent=agent, job=job, deployment=deployment, created=False,
                                  changed=True)

        db.rollback()
        db.expire_all()
        current = db.get(Agent, agent.id)
        if current is None or current.status == "deleted":
            raise NotFoundError(
                "system_agent.not_installed", f"'{preset.key}' was uninstalled meanwhile"
            )
        if current.status == STATUS_UNINSTALLING:
            _refuse_uninstalling(db, current, preset)
        if current.status == "deploying":
            if edit is not None:
                _refuse_deploy_in_progress(db, current, preset)  # a twin claimed first
            return _in_flight(db, current)
        # settled again with a different version: resolve the edit on the new state
        agent = current
    raise AppError(
        "system_agent.conflict",
        f"'{preset.key}' kept changing while this request was being resolved; retry",
        {"agent_id": agent.id},
        status_code=409,
    )


def _normalized(stored: dict[str, Any]) -> dict[str, Any]:
    try:
        return AgentSpec(**stored).model_dump()
    except ValidationError:  # an unreadable stored spec is never "already current"
        return {}


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
    if preset.template:
        return _assert_template_release_pinned(pinned, agent, preset)
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


def _assert_template_release_pinned(
    pinned: dict[str, Any], agent: Agent, preset: SystemPreset
) -> dict[str, Any]:
    """The ``zip_runtime`` twin of the skill-bundle guard: the job's pin, the stored spec's
    release tag and this checkout's template release must agree, and the spec must carry no
    skill bundle at all (the worker's code is the template)."""
    current = catalogue.template_release(preset)
    if pinned.get("version") != preset.skill_version or pinned.get("digest") != current["digest"]:
        raise RuntimeError(
            f"queued release mismatch: the job pins v{pinned.get('version')} "
            f"{str(pinned.get('digest'))[:12]}, this checkout renders v{preset.skill_version} "
            f"{current['digest'][:12]} — re-run the preset install (repair) so the pin and the "
            "template are re-derived together"
        )
    tag = catalogue.template_release_from_spec(agent.spec or {})
    if tag != current["tag"]:
        raise RuntimeError(
            f"the stored spec pins template release {tag!r} but this checkout renders "
            f"{current['tag']!r} — re-run the preset install"
        )
    if list((agent.spec or {}).get("skills") or []):
        raise RuntimeError(
            f"a '{preset.template}' preset carries no skill bundle, but the stored spec loads "
            f"{(agent.spec or {}).get('skills')} — re-run the preset install"
        )
    return {"pin": pinned, "snapshot": None, "expected_uri": None}


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
    # N16-obs111a: a canary that still names the preset as an arm (a legacy row — the
    # create path refuses presets as `system-managed`) would route to a torn-down Runtime
    canary_references.refuse_agent_in_use(db, agent)
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
