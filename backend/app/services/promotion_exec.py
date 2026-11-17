"""Promotion execution: an approved release, actually delivered (roadmap T27).

An approved `Promotion` becomes a background **job** (`jobs.type = promotion_execute`)
running against the TARGET workspace, with the same operational shape as a deploy: a `Job`
row whose JSONL log carries every stage, per-stage status persisted (here on the promotion
itself, because the console renders one card per stage), and a startup resume that
re-enters at the first stage that did not finish.

Stages, in order:

    resolve → copy → provision → deploy → smoke → canary → observe → complete

* **resolve** rewrites the frozen spec for the target through the logical resource mapping
  (T23) and refuses to continue while a reference has no mapping there.
* **copy** moves the stored artifact into the target account (T24). Skipped, with the
  reason logged, for a bundle that has none (a harness, or a zip built from the spec).
* **provision** stakes the target agent's ledger row and queues an ordinary deploy job for
  it. The per-agent IAM role is created by that deploy's own `provision` stage.
* **deploy** runs that deploy (create, or an in-place update that publishes a new version
  and keeps the prior spec in history) and waits for it. When the target already runs the
  agent and can host a runtime canary, *this stage instead mints the candidate version and
  stands up the canary gateway* — the champion must survive to be compared against, so the
  new spec cannot be published over it first.
* **smoke** sends a handful of fixed prompts to the target and stops the run if any fails.
* **canary** drives the existing runtime canary through 90/10 → 50/50 → 1/99, one traffic
  round and one verdict per weight. Skipped, with the reason logged, when there is no
  running agent to compare against or the agent cannot host a canary.
* **observe** holds at full traffic for the policy's observation window, then probes again.
* **complete** promotes the candidate (canary path) and cleans the canary up.

`rollback` runs the same stages against the *previous* bundle, with canary/observe skipped:
it re-publishes the earlier spec as a new version through the normal deploy path — it is
never an AWS-side revert.

Everything that touches AWS is reached through a seam a test can replace: the inner deploy
runs through `deployer.pipeline` (whose stage functions tests stub), the canary through
`CANARY`, invocation through `_invoke`, the artifact copy through `artifact_copy.copy_artifact`.
"""

import copy
import json
import logging
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.deployer import pipeline
from app.deployer.runtime_config import captured_runtime_config, carry_runtime_config
from app.models.ledger import Agent, Job, Promotion, ReleaseBundle, Workspace
from app.schemas.runtime_config import RUNTIME_CONFIG_METHODS, RuntimeConfig
from app.services import agent_names, release_gates
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.audit import record_audit_event
from app.services.workspace import WorkspaceContext, context_for_workspace

logger = logging.getLogger("launchpad.promotion")

JOB_TYPE = "promotion_execute"
STAGE_ORDER = ["resolve", "copy", "provision", "deploy", "smoke", "canary", "observe", "complete"]
ACTION_EXECUTE = "execute"
ACTION_ROLLBACK = "rollback"

STATUS_EXECUTING = "executing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_ROLLED_BACK = "rolled_back"

DEPLOY_WAIT_SECONDS = 45 * 60
OBSERVE_TICK_SECONDS = 30
REPLAY_PROMPT_FLOOR = 10  # a canary round needs enough sessions to split across arms
# One smoke/observe probe may take this long before it counts as a failure. Without a
# bound, a single stalled invocation — seen live on the first call to a Harness that had
# just turned READY — left the release `executing` forever, which also blocked any new
# release into that target and any rollback (`promotion.already_executing`).
PROBE_TIMEOUT_SECONDS = 150
PROBE_ATTEMPTS = 2  # one retry absorbs a cold start; a second stall is a real failure
# Pause before replaying a probe that failed with a transient upstream error (live: a
# Harness just redeployed by a rollback answered "Runtime initialization time exceeded"
# to 1 of 3 smoke prompts) — long enough for the cold start to finish.
PROBE_TRANSIENT_BACKOFF_SECONDS = 15


class StageFailed(RuntimeError):
    """A stage refused to advance. The message is what the operator reads."""


def _sleep(seconds: float) -> None:  # replaced in tests
    time.sleep(seconds)


def _now() -> datetime:
    return datetime.now(UTC)


# ── ledger helpers (short sessions: the worker thread owns none across stages) ────


def initial_stages() -> list[dict[str, Any]]:
    return [{"name": name, "status": "pending", "detail": ""} for name in STAGE_ORDER]


def _mutate(promotion_id: str, fn) -> None:
    db = SessionLocal()
    try:
        row = db.get(Promotion, promotion_id)
        if row is None:
            raise RuntimeError(f"promotion {promotion_id} no longer exists")
        fn(row)
        db.commit()
    finally:
        db.close()


def _patch_execution(promotion_id: str, **values: Any) -> None:
    def apply(row: Promotion) -> None:
        row.execution = {**(row.execution or {}), **values}

    _mutate(promotion_id, apply)


def _execution(promotion_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(Promotion, promotion_id)
        return dict(row.execution or {}) if row else {}
    finally:
        db.close()


def _set_stage(promotion_id: str, stage: str, status: str, detail: str = "") -> None:
    def apply(row: Promotion) -> None:
        stages = [dict(s) for s in (row.stages or initial_stages())]
        for entry in stages:
            if entry["name"] != stage:
                continue
            entry["status"] = status
            if detail:
                entry["detail"] = detail
            if status == "running":
                entry["started_at"] = pipeline._now_iso()
            if status in ("succeeded", "skipped", "failed"):
                entry["ended_at"] = pipeline._now_iso()
        row.stages = stages

    _mutate(promotion_id, apply)


def _log(job_id: str, stage: str, message: str, level: str = "info") -> None:
    db = SessionLocal()
    try:
        pipeline._append_log(db, job_id, stage, message, level)
    finally:
        db.close()


class Run:
    """Everything one worker needs, rehydrated from persisted rows."""

    def __init__(self, job: Job, promotion: Promotion, bundle: ReleaseBundle) -> None:
        self.job_id = job.id
        self.promotion_id = promotion.id
        self.action = (job.payload or {}).get("action", ACTION_EXECUTE)
        self.bundle_id = bundle.id
        # the release's project: the worker runs every stage as it (design §5.12)
        self.project_id = promotion.project_id
        self.source_id = promotion.workspace_id
        self.target_id = promotion.target_workspace_id
        # the TARGET context, rebuilt from the job's workspace_id, never ambient settings
        self.target: WorkspaceContext = context_for_workspace(job.workspace_id)
        self.stage = ""

    def log(self, message: str, level: str = "info") -> None:
        _log(self.job_id, self.stage, message, level)

    def bundle(self, db) -> ReleaseBundle:
        row = db.get(ReleaseBundle, self.bundle_id)
        if row is None:
            raise StageFailed(f"release bundle {self.bundle_id} no longer exists")
        return row

    def policy(self, db) -> dict[str, Any]:
        target = db.get(Workspace, self.target_id)
        if target is None:
            raise StageFailed(f"target workspace '{self.target_id}' no longer exists")
        return release_gates.effective_policy(target)


# ── seams ────────────────────────────────────────────────────────────────────────


class CanaryOps:
    """A narrow face on the runtime-canary machinery (`optimization.canary_service`).

    The promotion drives the canary by calling its action functions in-line from the job
    thread — the same `act_*` functions the canary console runs on its own thread — so a
    ramp step here is exactly a ramp step there. Tests substitute this object.
    """

    def start(self, agent_id: str, spec: dict[str, Any], workspace: WorkspaceContext) -> str:
        from app.optimization import canary_service
        from app.schemas.agent import AgentSpec

        db = SessionLocal()
        try:
            agent = db.get(Agent, agent_id)
            row = canary_service.start_canary(agent, AgentSpec(**spec), workspace)
            return row.id
        finally:
            db.close()

    def state(self, canary_id: str) -> dict[str, Any]:
        from app.optimization import canary_service

        row = canary_service._get(canary_id)
        _, current = canary_service._current_round(row)
        setup = (row.artifacts or {}).get("setup") or {}
        return {
            "status": row.status,
            "set_up": bool(setup),
            "ramp_stage": int(setup.get("ramp_stage", 0)),
            "weights": setup.get("weights") or {},
            "has_traffic": bool((current or {}).get("traffic_attempts")),
            "has_verdict": (current or {}).get("verdict") is not None,
            "verdict": ((current or {}).get("verdict") or {}).get("verdict"),
        }

    def setup(self, canary_id: str, progress) -> None:
        from app.optimization import canary_service

        canary_service.act_setup(canary_id, progress)

    def traffic(self, canary_id: str, prompts: list[str], info: dict[str, str], progress) -> None:
        from app.optimization import canary_service

        canary_service.act_traffic(canary_id, prompts, info, progress)

    def verdict(self, canary_id: str, progress) -> None:
        from app.optimization import canary_service

        canary_service.act_verdict(canary_id, progress)

    def check(self, canary_id: str) -> None:
        """Raise unless the recorded verdict lets the ramp advance (no silent override)."""
        from app.optimization import canary_service

        canary_service.assert_verdict_allows(
            canary_service._get(canary_id), allow_non_significant=False
        )

    def advance(self, canary_id: str, progress) -> None:
        from app.optimization import canary_service

        canary_service.act_advance(canary_id, progress, allow_non_significant=False)

    def complete(self, canary_id: str, progress) -> None:
        from app.optimization import canary_service

        canary_service.act_complete(canary_id, progress, allow_non_significant=False)

    def rollback(self, canary_id: str, progress) -> None:
        from app.optimization import canary_service

        canary_service.act_rollback(canary_id, progress)

    def cleanup(self, canary_id: str, progress) -> None:
        from app.optimization import canary_service

        canary_service.act_cleanup(canary_id, progress)


CANARY = CanaryOps()


def _invoke(agent_id: str, prompt: str, workspace: WorkspaceContext) -> str:
    """One prompt to the target agent through the shared invoke chain. The budget
    was checked when the execution started (`POST .../execute`): the smoke turn of a
    running job is not refused mid-way."""
    from app.services.invoke import invoke_agent_text
    from app.services.project_quotas import budget_exempt

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        if agent is None:
            raise StageFailed("the target agent no longer exists")
        with budget_exempt():
            result = invoke_agent_text(
                agent,
                prompt,
                session_id=f"promo-smoke-{uuid.uuid4().hex}",
                actor_id="promotion-smoke",
                workspace=workspace,
            )
    finally:
        db.close()
    return str(result.get("text") or "")


# ── stages ───────────────────────────────────────────────────────────────────────


def _stage_resolve(run: Run) -> pipeline.StageResult:
    from app.services import resource_mapping

    db = SessionLocal()
    try:
        bundle = run.bundle(db)
        target = db.get(Workspace, run.target_id)
        source = db.get(Workspace, bundle.workspace_id) if bundle.workspace_id else None
        if target is None:
            raise StageFailed(f"target workspace '{run.target_id}' no longer exists")
        if target.bootstrap_status != "ready":
            raise StageFailed(f"target workspace is {target.bootstrap_status}, not ready")
        resolution = resource_mapping.resolve_spec(
            db,
            bundle.spec or {},
            source_workspace_id=bundle.workspace_id,
            target_workspace_id=target.id,
            source_resources=getattr(source, "resources", None),
            target_resources=target.resources,
        )
        # the network profile first: its refusal names the mapping to create (§12)
        try:
            resource_mapping.assert_network_mapped(
                db, bundle.spec or {}, source_workspace_id=bundle.workspace_id,
                target_workspace_id=target.id, project_id=run.project_id,
            )
        except AppError as exc:
            raise StageFailed(exc.message) from exc
        if not resolution.complete:
            missing = "; ".join(u["reason"] for u in resolution.unmapped[:3])
            raise StageFailed(f"unmapped references: {missing}")
        from app.schemas.agent import AgentSpec

        try:
            AgentSpec.model_validate(resolution.spec)
        except Exception as exc:
            raise StageFailed(f"the mapped spec is not valid: {exc}") from exc
        # agent names are unique across the whole target workspace, so the holder is
        # looked up in every project: one of another project must stop the run here, not
        # be overwritten by the provision and deploy stages
        with project_context(None):
            existing = db.scalars(
                select(Agent).where(
                    Agent.workspace_id == target.id,
                    Agent.name == bundle.agent_name,
                    Agent.status != "deleted",
                )
            ).first()
        if existing is not None and existing.system_key:
            raise StageFailed("the target agent is a system-managed preset")
        if existing is None:
            _require_agent_project(db, run.project_id, target.id)
        if existing is not None and run.project_id and existing.project_id != run.project_id:
            raise StageFailed("the name is used by another project in the target")
        if existing is not None and existing.method != bundle.method:
            raise StageFailed(
                f"the target's '{existing.name}' is a {existing.method} agent; "
                f"the bundle is {bundle.method} (method cannot change on re-publish)"
            )
        if existing is not None and existing.status == "deploying":
            raise StageFailed("a deployment is already in progress for the target agent")
        _patch_execution(
            run.promotion_id,
            spec=resolution.spec,
            mapped=len(resolution.resolved),
            target_agent_id=existing.id if existing is not None else None,
        )
        return pipeline.StageResult(
            detail=f"{len(resolution.resolved)} reference(s) mapped; "
            + (f"replaces '{existing.name}'" if existing else f"creates '{bundle.agent_name}'")
        )
    finally:
        db.close()


def overlay_target_artifact(
    spec: dict[str, Any], target_artifact: dict[str, Any]
) -> dict[str, Any]:
    """Point a BYOC spec at the coordinates the copy created in the target account."""
    out = copy.deepcopy(spec)
    if out.get("byoc"):
        for key in ("image_uri", "upload_id"):
            if target_artifact.get(key):
                out["byoc"][key] = target_artifact[key]
    return out


def _stage_copy(run: Run) -> pipeline.StageResult:
    from app.services import artifact_copy

    db = SessionLocal()
    try:
        bundle = run.bundle(db)
        artifact = dict(bundle.artifact or {})
        if not any(artifact.get(k) for k in ("image_digest", "upload_id", "image_uri")):
            return pipeline.StageResult(
                skipped=True, detail="no stored artifact: the target builds from the frozen spec"
            )
        # the T24 service reads only bundle.artifact / .method; the row is passed as is
        result = artifact_copy.copy_artifact(
            bundle, context_for_workspace(run.source_id), run.target
        )
    finally:
        db.close()
    for step in getattr(result, "steps", None) or []:
        run.log(step)
    execution = _execution(run.promotion_id)
    spec = overlay_target_artifact(
        execution.get("spec") or {}, getattr(result, "target_artifact", None) or {}
    )
    _patch_execution(
        run.promotion_id, spec=spec, artifact_copy=result.as_dict(), target_touched=True
    )
    note = ""
    if bundle_method_rebuilds(run):
        note = "; the container method still rebuilds the image in the target's pipeline"
    return pipeline.StageResult(detail=f"{result.status} ({result.kind}){note}")


def bundle_method_rebuilds(run: Run) -> bool:
    db = SessionLocal()
    try:
        return run.bundle(db).method == "container"
    finally:
        db.close()


def _canary_verdict_for(run: Run, existing: Agent | None, bundle: ReleaseBundle) -> str | None:
    """None when a canary applies; otherwise the reason it does not."""
    from app.optimization.service import canary_capability

    if run.action != ACTION_EXECUTE:
        return "a rollback deploys the previous bundle directly"
    if existing is None:
        return "the target has no running agent to compare against"
    if existing.status != "active":
        return f"the target agent is {existing.status}, not active"
    if existing.method != bundle.method:
        return "the target agent uses a different method"
    capability = canary_capability(existing)
    if not capability["eligible"]:
        return capability["reason"] or "this agent cannot host a runtime canary"
    if capability.get("kind") == "harness":
        # A Harness canary A/Bs two versions that already exist; the release path mints
        # its candidate from the bundle's spec, which only a runtime canary can do
        return "a Harness release publishes its new version directly"
    return None


def _require_agent_project(db, project_id: str | None, workspace_id: str) -> None:
    """A new target agent obeys the agent-project rule (services/agent_project): the
    run's project must be a business project bound to the target workspace."""
    from app.services import agent_project

    try:
        agent_project.check_agent_project(db, project_id, workspace_id)
    except AppError as exc:
        raise StageFailed(f"{exc.code}: {exc.message}") from exc


def _stage_provision(run: Run) -> pipeline.StageResult:
    execution = _execution(run.promotion_id)
    if execution.get("provisioned"):
        return pipeline.StageResult(detail="target agent already staged")
    db = SessionLocal()
    try:
        bundle = run.bundle(db)
        spec = copy.deepcopy(execution.get("spec") or {})
        promotion = db.get(Promotion, run.promotion_id)
        existing = (
            db.get(Agent, execution["target_agent_id"])
            if execution.get("target_agent_id")
            else None
        )
        reason = _canary_verdict_for(run, existing, bundle)
        if reason is None:
            # candidate is minted by the deploy stage; nothing is published over the champion
            _patch_execution(
                run.promotion_id, provisioned=True, use_canary=True, canary_reason=None
            )
            return pipeline.StageResult(
                detail=f"'{existing.name}' stays live; the canary mints the candidate version"
            )
        run.log(f"canary not applicable: {reason}")
        previous_spec: dict[str, Any] | None = None
        previous_runtime_config: dict[str, Any] | None = None
        if existing is None:
            # a promotion that creates its target files it like any new agent
            _require_agent_project(db, run.project_id, run.target_id)
            agent = Agent(
                workspace_id=run.target_id,
                name=bundle.agent_name,
                method=bundle.method,
                status="deploying",
                spec=spec,
                owner=(promotion.requested_by if promotion else None) or "promotion",
                project_id=run.project_id,
            )
            db.add(agent)
            db.flush()
            with project_context(None):  # the name is reserved workspace-wide
                agent_names.claim_agent_name(db, run.target_id, agent.name, agent.id)
            mode = "create"
        else:
            agent = existing
            # what the target served before this run, so a failure after the publish can
            # put it back (a direct release is live the moment it deploys)
            previous_spec = copy.deepcopy(agent.spec or {})
            if (
                run.action == ACTION_EXECUTE
                and agent.method in RUNTIME_CONFIG_METHODS
                and agent.resource_id
                and previous_spec.get("runtime_config") is None
            ):
                # Capture BEFORE the direct publish rolls DEFAULT. A legacy spec
                # alone cannot restore billing/platform after a failed release.
                live = rt.get_runtime(control_client(run.target), agent.resource_id)
                previous_runtime_config = captured_runtime_config(live).model_dump()
            agent.spec = carry_runtime_config(spec, previous_spec)
            agent.status = "deploying"
            agent.error = None
            db.flush()
            mode = "update" if (agent.resource_id or agent.arn) else "create"
        note = (
            f"promotion {run.promotion_id}"
            if run.action == ACTION_EXECUTE
            else f"rollback of promotion {run.promotion_id}"
        )
        # the target workspace's network rules apply to the promoted spec (§12.6)
        from app.services.workspace import context_for_workspace

        network_extras = pipeline.network_gate(
            db, context_for_workspace(agent.workspace_id), agent.spec or {},
            previous_spec=previous_spec if mode == "update" else None,
        )
        deployment, deploy_job = pipeline.create_deployment(
            db,
            agent,
            mode=mode,
            payload_extra=network_extras or None,
            # an in-place update keeps the registry identity it already has
            skip_register=bool(mode == "update" and agent.registry_record_id),
            actor=(promotion.reviewed_by if promotion else None) or "promotion",
            note=note,
            commit=False,
        )
        # one commit: the staged agent, its deploy job and the pointer to both, so a crash
        # cannot leave a job the resumed stage would stage a second time
        promotion.execution = {
            **(promotion.execution or {}),
            "provisioned": True,
            "use_canary": False,
            "canary_reason": reason,
            "target_agent_id": agent.id,
            "deployment_id": deployment.id,
            "deploy_job_id": deploy_job.id,
            "deploy_mode": mode,
            "target_touched": True,
            "previous_spec": previous_spec if existing is not None and mode == "update" else None,
            "previous_runtime_config": previous_runtime_config,
        }
        db.commit()
        return pipeline.StageResult(
            detail=f"staged '{bundle.agent_name}' for {mode} (deploy job {deploy_job.id})"
        )
    finally:
        db.close()


def _wait_for_job(run: Run, job_id: str) -> None:
    """Block until the inner deploy job is terminal; start it only if nobody has."""
    deadline = time.monotonic() + DEPLOY_WAIT_SECONDS
    announced = False
    while True:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            status, error = (job.status, job.error) if job else ("missing", None)
        finally:
            db.close()
        if status == "succeeded":
            return
        if status in ("failed", "missing"):
            raise StageFailed(f"deploy job {job_id} {status}: {error or 'no detail'}")
        if status == "queued":
            pipeline.start_deploy_async(job_id)  # no-op while a worker is alive
        if not announced:
            run.log(f"waiting for deploy job {job_id}")
            announced = True
        if time.monotonic() > deadline:
            raise StageFailed(f"deploy job {job_id} did not finish within the wait limit")
        _sleep(2)


def _stage_deploy(run: Run) -> pipeline.StageResult:
    execution = _execution(run.promotion_id)
    if execution.get("use_canary"):
        return _deploy_canary_candidate(run, execution)
    _wait_for_job(run, execution["deploy_job_id"])
    db = SessionLocal()
    try:
        agent = db.get(Agent, execution["target_agent_id"])
        if agent is None or agent.status != "active":
            raise StageFailed(
                f"the deploy finished but the target agent is {agent.status if agent else 'gone'}"
            )
        version = agent.version
    finally:
        db.close()
    return pipeline.StageResult(detail=f"deployed ({execution['deploy_mode']}), version {version}")


def _deploy_canary_candidate(run: Run, execution: dict[str, Any]) -> pipeline.StageResult:
    canary_id = execution.get("canary_id")
    if not canary_id:
        canary_id = CANARY.start(execution["target_agent_id"], execution["spec"], run.target)
        _patch_execution(run.promotion_id, canary_id=canary_id, target_touched=True)
        run.log(f"created canary {canary_id}")
    if not CANARY.state(canary_id)["set_up"]:
        CANARY.setup(canary_id, run.log)
    return pipeline.StageResult(
        detail=f"candidate minted behind canary {canary_id}; the running version is the control"
    )


def _smoke_prompts(run: Run) -> list[str]:
    db = SessionLocal()
    try:
        return list(run.policy(db)["smoke_prompts"])
    finally:
        db.close()


def _probe_into(
    outcome: dict[str, Any], agent_id: str, prompt: str, workspace: WorkspaceContext,
    project_id: str | None = None,
) -> None:
    try:
        with project_context(project_id):  # a new thread does not inherit the run's
            outcome["answer"] = _invoke(agent_id, prompt, workspace)
    except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
        outcome["error"] = exc


def _bounded_invoke(run: Run, label: str, index: int, agent_id: str, prompt: str) -> str:
    """`_invoke` with a deadline per attempt, retried once on a stall or on a transient
    upstream error (`evaluation.service.transient_invoke_error`: throttling, a 5xx, a
    runtime client error such as a cold start); any other error fails at once.

    Each attempt runs on a **daemon** thread so a stall can be abandoned: Python cannot
    cancel the blocked network read, but the release stops waiting on it, and a daemon
    thread never holds up process shutdown (a `ThreadPoolExecutor` worker would — the
    interpreter joins those at exit).
    """
    from app.evaluation.service import transient_invoke_error

    last = "no attempt made"
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        # passed in, not closed over: an abandoned attempt that finishes late must write
        # into ITS OWN result, never into the attempt that replaced it
        outcome: dict[str, Any] = {}
        worker = threading.Thread(
            target=_probe_into, args=(outcome, agent_id, prompt, run.target, run.project_id),
            name="promotion-probe", daemon=True,
        )
        worker.start()
        worker.join(PROBE_TIMEOUT_SECONDS)
        if worker.is_alive():
            last = f"timed out after {PROBE_TIMEOUT_SECONDS}s"
            run.log(f"{label} #{index} attempt {attempt}/{PROBE_ATTEMPTS}: {last}", level="warn")
            continue
        if "error" in outcome:
            error = outcome["error"]
            if attempt == PROBE_ATTEMPTS or not transient_invoke_error(error):
                raise error
            last = f"{type(error).__name__}: {str(error)[:160]}"
            run.log(f"{label} #{index} attempt {attempt}/{PROBE_ATTEMPTS}: {last}", level="warn")
            _sleep(PROBE_TRANSIENT_BACKOFF_SECONDS)
            continue
        return str(outcome.get("answer") or "")
    raise TimeoutError(f"{last} on {PROBE_ATTEMPTS} attempts")


def _run_smoke(run: Run, label: str) -> str:
    execution = _execution(run.promotion_id)
    agent_id = execution["target_agent_id"]
    prompts = _smoke_prompts(run)
    failures: list[str] = []
    for index, prompt in enumerate(prompts, 1):
        try:
            answer = _bounded_invoke(run, label, index, agent_id, prompt)
        except Exception as exc:
            failures.append(f"#{index} raised {type(exc).__name__}: {str(exc)[:160]}")
            continue
        if not answer.strip():
            failures.append(f"#{index} returned an empty answer")
        else:
            run.log(f"{label} #{index} ok ({len(answer)} chars)")
    _patch_execution(
        run.promotion_id,
        **{label: {"passed": len(prompts) - len(failures), "total": len(prompts)}},
    )
    if failures:
        raise StageFailed(
            f"{label} test failed {len(failures)}/{len(prompts)}: " + "; ".join(failures)
        )
    return f"{len(prompts)}/{len(prompts)} prompts answered"


def _stage_smoke(run: Run) -> pipeline.StageResult:
    execution = _execution(run.promotion_id)
    if execution.get("use_canary"):
        run.log("canary path: prompts are routed by the live canary split")
    return pipeline.StageResult(detail=_run_smoke(run, "smoke"))


def _replay_prompts(run: Run) -> tuple[list[str], dict[str, str]]:
    """Prompts for one canary traffic round: the pinned dataset, else the smoke prompts."""
    from app.evaluation.models import EvalDataset
    from app.optimization import service as experiment_service

    db = SessionLocal()
    try:
        bundle = run.bundle(db)
        dataset_id = (bundle.evaluation or {}).get("dataset_id")
        dataset = db.get(EvalDataset, dataset_id) if dataset_id else None
        if dataset is not None:
            try:
                prompts = experiment_service.resolve_traffic_prompts(dataset)
            except ValueError:
                prompts = []
            if prompts:
                return prompts, {"dataset_id": dataset.id, "dataset_name": dataset.name}
        base = list(run.policy(db)["smoke_prompts"])
    finally:
        db.close()
    prompts = (base * REPLAY_PROMPT_FLOOR)[: max(REPLAY_PROMPT_FLOOR, len(base))]
    return prompts, {"dataset_id": "promotion-smoke", "dataset_name": "smoke prompts"}


def _stage_canary(run: Run) -> pipeline.StageResult:
    execution = _execution(run.promotion_id)
    if not execution.get("use_canary"):
        reason = execution.get("canary_reason") or "not applicable"
        return pipeline.StageResult(skipped=True, detail=f"canary skipped: {reason}")
    from app.optimization.canary_service import RAMP_WEIGHTS

    canary_id = execution["canary_id"]
    prompts, info = _replay_prompts(run)
    while True:
        state = CANARY.state(canary_id)
        if state["status"] != "running":
            raise StageFailed(f"canary {canary_id} is {state['status']}, not running")
        stage = state["ramp_stage"]
        weights = "/".join(str(w) for w in RAMP_WEIGHTS[stage])
        if not state["has_traffic"]:
            run.log(f"ramp {stage + 1}/{len(RAMP_WEIGHTS)} at {weights}: sending traffic")
            CANARY.traffic(canary_id, prompts, info, run.log)
        if not CANARY.state(canary_id)["has_verdict"]:
            run.log(f"ramp {stage + 1}/{len(RAMP_WEIGHTS)} at {weights}: recording verdict")
            CANARY.verdict(canary_id, run.log)
        try:
            CANARY.check(canary_id)
        except AppError as exc:
            raise StageFailed(f"canary blocked at {weights}: {exc.message}") from exc
        _patch_execution(run.promotion_id, canary_ramp=stage)
        if stage >= len(RAMP_WEIGHTS) - 1:
            return pipeline.StageResult(detail=f"ramped to {weights} with a passing verdict")
        run.log(f"advancing from {weights}")
        CANARY.advance(canary_id, run.log)


def _stage_observe(run: Run) -> pipeline.StageResult:
    if run.action != ACTION_EXECUTE:
        return pipeline.StageResult(skipped=True, detail="a rollback has no observation window")
    db = SessionLocal()
    try:
        seconds = int(run.policy(db)["observe_seconds"])
    finally:
        db.close()
    if seconds <= 0:
        return pipeline.StageResult(skipped=True, detail="no observation window configured")
    execution = _execution(run.promotion_id)
    until = execution.get("observe_until")
    if not until:
        until = (_now() + timedelta(seconds=seconds)).isoformat()
        _patch_execution(run.promotion_id, observe_until=until)
        run.log(f"observing at full traffic until {until}")
    deadline = datetime.fromisoformat(until)
    while (remaining := (deadline - _now()).total_seconds()) > 0:
        _sleep(min(OBSERVE_TICK_SECONDS, remaining))
    return pipeline.StageResult(detail=f"{seconds}s window clean; {_run_smoke(run, 'observe')}")


def _stage_complete(run: Run) -> pipeline.StageResult:
    execution = _execution(run.promotion_id)
    canary_id = execution.get("canary_id")
    if execution.get("use_canary") and canary_id:
        if CANARY.state(canary_id)["status"] == "running":
            CANARY.complete(canary_id, run.log)
        try:
            CANARY.cleanup(canary_id, run.log)
        except Exception as exc:  # the release is already live; cleanup is retryable
            run.log(f"canary cleanup incomplete: {type(exc).__name__}: {exc}", level="warn")
    db = SessionLocal()
    try:
        agent = db.get(Agent, execution["target_agent_id"])
        if agent is None:
            raise StageFailed("the target agent disappeared before completion")
        agent.status = "active"
        agent.error = None
        db.commit()
    finally:
        db.close()
    return pipeline.StageResult(detail="release complete")


# ── the worker ───────────────────────────────────────────────────────────────────


def _abort_canary(run: Run) -> None:
    """A canary that never completed must not leave anything behind.

    Two steps, mirroring the success path in `_stage_complete` (complete → cleanup):
    roll back so the previous version serves all traffic again, then clean up the
    canary's own AWS resources — its experiment gateway, A/B test, online-evaluation
    config and the candidate's runtime endpoint. Rolling back alone (the original
    behaviour) left all of those in place, which leaked billable resources and made the
    agent undeletable (`DeleteAgentRuntime` refuses while an endpoint exists); found by
    the cross-region release e2e when a canary correctly blocked a worse candidate.

    Each step is best-effort and logged: a failed cleanup is retryable from the canary
    console, but a failed rollback is the one that matters, so it is attempted first.
    """
    canary_id = _execution(run.promotion_id).get("canary_id")
    if not canary_id:
        return
    try:
        if CANARY.state(canary_id)["status"] == "running":
            run.stage = "canary"
            run.log("rolling the canary back so the previous version serves again")
            CANARY.rollback(canary_id, run.log)
    except Exception as exc:
        run.log(f"canary rollback failed: {type(exc).__name__}: {exc}", level="error")
        return  # never tear down a canary that may still be routing traffic
    try:
        run.log("cleaning up the canary's experiment resources")
        CANARY.cleanup(canary_id, run.log)
    except Exception as exc:
        run.log(
            f"canary cleanup incomplete: {type(exc).__name__}: {exc} — retry it from the "
            "canary console",
            level="warn",
        )


# Stages after which a direct (non-canary) release is already serving traffic.
_LIVE_AFTER = ("deploy", "smoke", "canary", "observe", "complete")


def _revert_direct(run: Run, failed_stage: str) -> None:
    """Put a direct release's previous spec back after a failure past the publish.

    A non-canary update is live as soon as its deploy job finishes, so a smoke or
    observe failure used to leave the failing version serving. Re-publishing the spec
    the target ran before restores it. Never raises: the run is already failing, and a
    revert that cannot complete is logged for the operator rather than masking the
    original error.
    """
    execution = _execution(run.promotion_id)
    previous = execution.get("previous_spec")
    if (
        run.action != ACTION_EXECUTE
        or execution.get("use_canary")
        or execution.get("deploy_mode") != "update"
        or not previous
        or failed_stage not in _LIVE_AFTER
        or execution.get("reverted")
    ):
        return
    db = SessionLocal()
    try:
        agent = db.get(Agent, execution["target_agent_id"])
        if agent is None:
            return
        if agent.status == "deploying":
            # the failed stage was the deploy itself and it never went active: nothing
            # new is serving, so there is nothing to put back
            return
        restored = copy.deepcopy(previous)
        if agent.method in RUNTIME_CONFIG_METHODS and restored.get("runtime_config") is None:
            captured = execution.get("previous_runtime_config")
            if captured is None:
                raise RuntimeError(
                    "original Runtime settings were not captured before publish; "
                    "recovery requires review"
                )
            restored["runtime_config"] = RuntimeConfig.model_validate(captured).model_dump()
        agent.spec = restored
        agent.status = "deploying"
        agent.error = None
        db.flush()
        _, job = pipeline.create_deployment(
            db,
            agent,
            mode="update",
            skip_register=bool(agent.registry_record_id),
            actor="promotion-executor",
            note=f"revert after failed promotion {run.promotion_id}",
            commit=False,
        )
        db.commit()
        job_id = job.id
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run.log(f"revert could not start: {type(exc).__name__}: {exc}", level="warn")
        return
    finally:
        db.close()
    _patch_execution(run.promotion_id, reverted=True, revert_job_id=job_id)
    run.log(f"reverting the target to the spec it ran before (deploy job {job_id})")
    try:
        _wait_for_job(run, job_id)
        run.log("reverted: the previous spec is serving again")
    except Exception as exc:  # noqa: BLE001
        run.log(
            f"revert did not finish: {exc} — re-publish the previous snapshot manually",
            level="warn",
        )


def _finish(run: Run, error: str | None, failed_stage: str | None = None) -> None:
    ok = error is None

    def apply(row: Promotion) -> None:
        if ok:
            row.status = STATUS_ROLLED_BACK if run.action == ACTION_ROLLBACK else STATUS_SUCCEEDED
            row.error = None
        else:
            row.status = STATUS_FAILED
            prefix = "rollback failed at " if run.action == ACTION_ROLLBACK else ""
            row.error = f"{prefix}{failed_stage}: {error}" if failed_stage else error
        row.finished_at = _now()

    _mutate(run.promotion_id, apply)
    db = SessionLocal()
    try:
        job = db.get(Job, run.job_id)
        if job is not None:
            job.status = "succeeded" if ok else "failed"
            job.error = None if ok else error
            db.commit()
    finally:
        db.close()
    try:
        record_audit_event(
            workspace_id=run.target_id,
            actor="promotion-executor",
            action=f"promotion.{run.action}.{'ok' if ok else 'failed'}",
            target=run.promotion_id,
        )
    except Exception:  # the journal must not turn a finished run into a crash
        logger.warning("promotion %s: audit journal write failed", run.promotion_id)


def execute_job(job_id: str, *, resume: bool = True) -> None:
    """Run (or resume) one promotion job to completion. Never raises.

    The claim mirrors `pipeline.execute_deploy_job`: a fresh launch takes `queued ->
    running` with one conditional UPDATE; only the startup resume (and a direct call)
    adopts a job a dead process left `running`. A terminal job is inert.
    """
    db = SessionLocal()
    try:
        eligible = ["queued", "running"] if resume else ["queued"]
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.type == JOB_TYPE, Job.status.in_(eligible))
            .values(status="running", updated_at=_now())
        ).rowcount
        db.commit()
        if claimed != 1:
            return
        job = db.get(Job, job_id)
        promotion = db.get(Promotion, (job.payload or {})["promotion_id"])
        bundle = db.get(
            ReleaseBundle, (job.payload or {}).get("bundle_id") or promotion.bundle_id
        )
        run = Run(job, promotion, bundle)
        done = {
            s["name"] for s in (promotion.stages or []) if s["status"] in ("succeeded", "skipped")
        }
    except Exception as exc:
        db.rollback()
        _fail_outside_stage(job_id, exc)
        return
    finally:
        db.close()

    # a worker thread starts with no project: every stage reads the target's ledger and
    # stamps what it creates (the agent, its deploy job, the canary) as the release's own
    with project_context(run.project_id):
        _run_stages(run, done)


def _run_stages(run: Run, done: set[str]) -> None:
    for name in STAGE_ORDER:
        if name in done:
            continue
        run.stage = name
        _set_stage(run.promotion_id, name, "running")
        run.log("stage started")
        try:
            result = globals()[f"_stage_{name}"](run)
        except Exception as exc:
            detail = (
                str(exc) if isinstance(exc, StageFailed) else f"{type(exc).__name__}: {exc}"
            )
            logger.warning("promotion %s failed at %s: %s", run.promotion_id, name, detail)
            _set_stage(run.promotion_id, name, "failed", detail)
            run.log(detail, level="error")
            run.log(traceback.format_exc(limit=3), level="debug")
            _abort_canary(run)
            _revert_direct(run, name)
            _finish(run, detail, failed_stage=name)
            return
        status = "skipped" if result.skipped else "succeeded"
        _set_stage(run.promotion_id, name, status, result.detail)
        run.log(result.detail or status)
    run.stage = "complete"
    _finish(run, None)


def _fail_outside_stage(job_id: str, exc: BaseException) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    logger.exception("promotion job %s failed outside a stage: %s", job_id, detail)
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error = detail
        promotion = db.get(Promotion, (job.payload or {}).get("promotion_id", ""))
        if promotion is not None:
            promotion.status = STATUS_FAILED
            promotion.error = detail
            promotion.finished_at = _now()
        db.commit()
        pipeline._append_log(db, job_id, "job", detail, "error")
    finally:
        db.close()


_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()


def start_async(job_id: str, *, resume: bool = False) -> threading.Thread:
    with _LIVE_LOCK:
        existing = _LIVE.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def work() -> None:
            try:
                execute_job(job_id, resume=resume)
            finally:
                with _LIVE_LOCK:
                    if _LIVE.get(job_id) is threading.current_thread():
                        del _LIVE[job_id]

        thread = threading.Thread(target=work, daemon=True, name=f"promotion-{job_id[:8]}")
        _LIVE[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE.pop(job_id, None)
            raise
        return thread


def start_resume(job_id: str) -> threading.Thread:
    """Startup resume: the only launch allowed to adopt a job a dead process left running."""
    return start_async(job_id, resume=True)


# ── admission (called by the routes) ─────────────────────────────────────────────


def previous_bundle_id(db, promotion: Promotion, bundle: ReleaseBundle) -> str | None:
    """The bundle the target last ran for this agent: what a rollback re-deploys."""
    rows = db.execute(
        select(Promotion, ReleaseBundle)
        .join(ReleaseBundle, ReleaseBundle.id == Promotion.bundle_id)
        .where(
            Promotion.target_workspace_id == promotion.target_workspace_id,
            Promotion.status == STATUS_SUCCEEDED,
            Promotion.id != promotion.id,
            ReleaseBundle.agent_name == bundle.agent_name,
        )
        .order_by(Promotion.finished_at.desc(), Promotion.created_at.desc())
    ).all()
    for _, candidate in rows:
        if candidate.id != bundle.id:
            return candidate.id
    return None


def assert_target_free(db, promotion: Promotion, bundle: ReleaseBundle) -> None:
    # the target agent name is a workspace-wide resource: a release of another project
    # into the same name is just as busy, though only one of this project is named
    with project_context(None):
        others = db.execute(
            select(Promotion)
            .join(ReleaseBundle, ReleaseBundle.id == Promotion.bundle_id)
            .where(
                Promotion.target_workspace_id == promotion.target_workspace_id,
                Promotion.status == STATUS_EXECUTING,
                Promotion.id != promotion.id,
                ReleaseBundle.agent_name == bundle.agent_name,
            )
        ).scalars().first()
    if others is not None:
        mine = others.project_id == promotion.project_id
        raise AppError(
            "promotion.target_busy",
            (f"promotion {others.id}" if mine else "another project's release")
            + f" is already executing '{bundle.agent_name}' in "
            f"{promotion.target_workspace_id}",
            {"promotion_id": others.id if mine else None},
            status_code=409,
        )


def _target_holds(db, target_workspace_id: str, bundle: ReleaseBundle) -> bool:
    """The target workspace already runs a live agent of the bundle's name."""
    with project_context(None):  # names are workspace-wide
        return db.scalars(
            select(Agent.id).where(
                Agent.workspace_id == target_workspace_id,
                Agent.name == bundle.agent_name,
                Agent.status != "deleted",
            )
        ).first() is not None


def admit(
    db,
    promotion: Promotion,
    *,
    action: str,
    bundle_id: str,
    previous_id: str | None,
    gates: dict[str, Any] | None = None,
) -> Job:
    """Flip the promotion to `executing`, mint the job, and hand back the queued row.

    The caller commits and then starts the worker, so a crash in between leaves a queued
    job the startup resume picks up rather than a promotion stuck `executing` with none.
    A bundle whose agent runs in a network profile with no mapping into the target is
    refused here (409 `network_profile.unmapped`) — the rollback route runs no gates.
    """
    from app.services import resource_mapping

    bundle = db.get(ReleaseBundle, bundle_id)
    if bundle is not None and not _target_holds(db, promotion.target_workspace_id, bundle):
        # the run would create its target agent: refuse before any job (422), the same
        # agent-project rule every other creation path obeys
        from app.services import agent_project

        agent_project.check_agent_project(
            db, promotion.project_id, promotion.target_workspace_id,
        )
    if bundle is not None:
        resource_mapping.assert_network_mapped(
            db, bundle.spec or {}, source_workspace_id=bundle.workspace_id,
            target_workspace_id=promotion.target_workspace_id,
            project_id=promotion.project_id,
        )
    job = Job(
        workspace_id=promotion.target_workspace_id,
        type=JOB_TYPE,
        payload={"promotion_id": promotion.id, "action": action, "bundle_id": bundle_id},
    )
    db.add(job)
    db.flush()
    promotion.status = STATUS_EXECUTING
    promotion.job_id = job.id
    promotion.error = None
    promotion.stages = initial_stages()
    promotion.started_at = _now()
    promotion.finished_at = None
    promotion.previous_bundle_id = previous_id
    promotion.execution = {
        "action": action,
        "bundle_id": bundle_id,
        **({"gates": gates} if gates else {}),
    }
    db.flush()
    return job


def job_log(db, job_id: str | None, limit: int = 400) -> list[dict[str, Any]]:
    if not job_id:
        return []
    job = db.get(Job, job_id)
    if job is None or not job.log:
        return []
    lines = []
    for raw in job.log.splitlines()[-limit:]:
        try:
            lines.append(json.loads(raw))
        except ValueError:
            lines.append({"ts": None, "stage": "job", "level": "info", "msg": raw})
    return lines
