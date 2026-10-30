"""Independent Runtime canary orchestration (Model 1: one agent, two immutable
versions behind two named endpoints).

CN (N12 W-E3, plan §B.8): the per-canary Gateway, its targets and the AgentCore
target-routing A/B test are gone (none exist in ``aws-cn``). What stays is exactly the
part the cn control plane offers — Runtime **versions** (``mint_candidate_version`` →
``UpdateAgentRuntime``) and **named endpoints** ``stable<id6>`` → ``v_current`` /
``treat<id6>`` → ``v_candidate`` — and the split moves into Launchpad's own invoke
chain: the local A/B row ``artifacts.abtest`` (``target`` variants = endpoint names,
``RAMP_WEIGHTS``) is what ``optimization.routing`` reads to pick a session's
``qualifier``; replay traffic is one pre-assigned ``batch_eval`` job on the evaluation
worker (``optimization.traffic``); the verdict is computed from ``eval_results`` ×
``experiment_assignments`` (``routing.ab_results``). Each arm's live sessions are
scored by a local online evaluation config pointed at that endpoint's log group.

Safety invariant kept from the baseline: ``UpdateAgentRuntime`` auto-rolls DEFAULT to
the untested candidate, so from the moment the stable endpoint exists (the PARTIAL
setup written before the mint) until the A/B row is live, routing sends **every**
session to ``stable<id6>`` — production never lands on the candidate by accident.
The managed-Harness canary does not exist in CN (C5).
"""

import copy
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer.pipeline import StageContext, refuse_deploy_in_progress
from app.evaluation import executor as eval_executor
from app.evaluation.online_evaluators import ONLINE_EVAL_DEFAULT
from app.models.ledger import Agent
from app.optimization import canary_infra, routing, traffic
from app.optimization.capability import canary_capability
from app.optimization.models import RuntimeCanary
from app.optimization.progress_steps import detail_of, step
from app.optimization.verdict import (
    VERDICT_POLL_S,
    VERDICT_WAIT_S,
    compute_verdict,
    held_out_block,
    metric_evaluator_id,
    reason_of,
)
from app.schemas.agent import AgentSpec
from app.services import creators, eval_tokens, policy_enforcement, snapshots
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import control_client
from app.services.policy_enforcement import DeployPolicy
from app.services.workspace import WorkspaceContext, context_for_workspace

Progress = Callable[[str], None]
RAMP_WEIGHTS = ((90, 10), (50, 50), (1, 99))
ASYNC_ACTIONS = frozenset(
    {"setup", "traffic", "verdict", "advance", "complete", "rollback", "cleanup"}
)
# Evaluators the per-arm online configs (and the replay job) score a canary with when the
# row carries no choice of its own (a row created before N12-obs46, or an empty list).
CANARY_EVALUATORS: tuple[str, ...] = tuple(ONLINE_EVAL_DEFAULT)
# Routing substrate label the console renders where the Global build showed a Gateway id.
ROUTING_MODE = "invoke-chain"


_sleep = time.sleep  # injectable


def _spawn(target: Callable[[], None]) -> None:  # injectable for tests
    threading.Thread(target=target, daemon=True).start()


def _online():
    """W-E2's online-evaluation module (frozen seam: ``create_for_owner`` /
    ``delete_for_owner``); resolved lazily so the two groups' imports never cycle."""
    from app.evaluation import online

    return online


def rt_name(control: Any, runtime_id: str) -> str:
    return control.get_agent_runtime(agentRuntimeId=runtime_id)["agentRuntimeName"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(canary_id: str) -> RuntimeCanary:
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        if row is None:
            raise RuntimeError(f"runtime canary {canary_id} no longer exists")
        return row
    finally:
        db.close()


def _update(canary_id: str, *, disable_online: Any = None, **fields: Any) -> None:
    """Write ``fields`` (``artifact`` merged into the artifacts); with ``disable_online`` (the
    canary's workspace) its per-arm online configs go DISABLED in the same commit — a
    terminal transition (N17-obs442)."""
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        if row is None:
            raise RuntimeError(f"runtime canary {canary_id} no longer exists")
        artifact = fields.pop("artifact", None)
        if artifact is not None:
            merged = dict(row.artifacts)
            merged.update(artifact)
            row.artifacts = merged
        for key, value in fields.items():
            setattr(row, key, value)
        if disable_online is not None:
            _online().disable_for_owner(disable_online, canary_id, db=db)
        db.commit()
    finally:
        db.close()


def run_action(
    canary_id: str,
    action: str,
    fn: Callable[[Progress], Any],
) -> None:
    """Run one canary action with progress persisted on its own ledger row."""

    def progress(message: str) -> None:
        # N17-obs447: a coded line also stores its code + params; plain text clears them
        _update(canary_id, progress=message[:300], progress_detail=detail_of(message))

    _refuse_system_subject(_get(canary_id), action)  # before any write or thread

    def runner() -> None:
        try:
            fn(progress)
            _update(canary_id, running_action=None, progress=None, progress_detail=None)
        except Exception as exc:
            _update(
                canary_id,
                running_action=None,
                progress=None,
                progress_detail=None,
                error=f"{action}: {type(exc).__name__}: {exc}"[:500],
            )

    _update(canary_id, running_action=action, error=None)
    _spawn(runner)


def clear_stale_running_actions() -> list[str]:
    """Clear daemon-thread flags left by a backend restart.

    Two guards keep it from fighting a resumed architect clear (``assistant.purge``,
    N15-obs10): a canary whose Agent's conversation has a live purge job is skipped
    whatever its ``progress`` says (the job re-adopts and rewrites the claim), and every
    clear is a compare-and-set on the ``running_action`` value this sweep read — a claim
    the job released between the read and the write stays released, without the error."""
    from app.assistant import purge as assistant_purge

    db = SessionLocal()
    try:
        rows = (
            db.query(RuntimeCanary)
            .filter(RuntimeCanary.running_action.isnot(None))
            .all()
        )
        cleared: list[str] = []
        for row in rows:
            if assistant_purge.canary_under_live_clear(db, row):
                continue
            seen = row.running_action
            won = db.execute(
                update(RuntimeCanary)
                .where(RuntimeCanary.id == row.id, RuntimeCanary.running_action == seen)
                .values(
                    running_action=None,
                    progress=None,
                    error=f"{seen}: interrupted by a backend restart — retry the action",
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if won:
                cleared.append(row.id)
        db.commit()
        return cleared
    finally:
        db.close()


def _agent_meta(agent: Any, control: Any) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "arn": agent.arn,
        "resource_id": agent.resource_id,
        "runtime_name": rt_name(control, agent.resource_id),
        "canary_capability": canary_capability(agent),
    }


# ─── the operator's evaluator choice (N12-obs46, upstream b5eb3dc8 parity) ───────────
def _evaluator_artifacts(
    online_evaluators: list[str] | None, primary_evaluator: str | None,
) -> dict[str, Any]:
    """The operator's evaluator choice as top-level artifact keys (absent when not given,
    so a row created without it reads exactly like a row from before the choice)."""
    out: dict[str, Any] = {}
    if online_evaluators:
        out["online_evaluators"] = list(online_evaluators)
    if primary_evaluator:
        out["primary_evaluator"] = primary_evaluator
    return out


def configured_evaluators(row: RuntimeCanary) -> list[str] | None:
    """The evaluator ids both arms' online evaluations (and the replay job) were created
    with, or None for a row created before the choice existed (its configs carry the
    default pair, :data:`CANARY_EVALUATORS`)."""
    artifacts = row.artifacts or {}
    chosen = artifacts.get("online_evaluators") or (artifacts.get("setup") or {}).get(
        "online_evaluators")
    return list(chosen) if chosen else None


def primary_evaluator(row: RuntimeCanary) -> str | None:
    """The evaluator that alone decides the verdict, or None (legacy aggregate)."""
    artifacts = row.artifacts or {}
    return artifacts.get("primary_evaluator") or (artifacts.get("setup") or {}).get(
        "primary_evaluator") or None


def scoring_evaluators(row: RuntimeCanary) -> list[str]:
    """What this canary's arms are scored with: the configured list or the default pair."""
    return configured_evaluators(row) or list(CANARY_EVALUATORS)


def _setup_evaluator_keys(row: RuntimeCanary) -> dict[str, Any]:
    """What setup copies into ``artifacts.setup`` so the detail page reads one place."""
    out: dict[str, Any] = {"online_evaluators": scoring_evaluators(row)}
    primary = primary_evaluator(row)
    if primary:
        out["primary_evaluator"] = primary
    return out


def start_canary(
    agent: Any,
    edited_spec: AgentSpec,
    workspace: WorkspaceContext,
    *,
    online_evaluators: list[str] | None = None,
    primary_evaluator: str | None = None,
    design: dict[str, Any] | None = None,
    creator: creators.Creator | None = None,
) -> RuntimeCanary:
    """Create only the ledger row; setup mints the candidate version + endpoints.

    Model 1 collapses champion/challenger onto ONE agent: both columns carry the
    single agent id/name so ``_out`` and existing consumers keep working. The
    candidate is described by ``edited_spec`` (stored as a dump) and materialized
    at setup, never here. ``online_evaluators`` / ``primary_evaluator`` (validated by
    the router) are stored as top-level artifact keys; absent → the default pair and the
    aggregate verdict, exactly like a row from before the choice existed.
    """
    control = control_client(workspace)
    row = RuntimeCanary(
        workspace_id=agent.workspace_id,
        name=f"CANARY-{agent.name[:32]}",
        champion_agent_id=agent.id,
        champion_agent_name=agent.name,
        challenger_agent_id=agent.id,
        challenger_agent_name=agent.name,
        artifacts={
            "agent_meta": _agent_meta(agent, control),
            "edited_spec": edited_spec.model_dump(),
            "rounds": [],
            **_evaluator_artifacts(online_evaluators, primary_evaluator),
            # R3-d: the fixed-n design (power calculation) the verdict honours
            **({"design": dict(design)} if design else {}),
        },
    )
    creators.stamp(row, creator)
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        row_id = row.id
    finally:
        db.close()
    return _get(row_id)


def _test_name(canary_id: str) -> str:
    return f"can_{canary_id[:8]}_target"


def assert_setup_available(canary_id: str) -> None:
    """Setup must not start while another canary is still routing this agent — one
    owner per agent (``routing.active_owner``)."""
    row = _get(canary_id)
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, row.champion_agent_id)
    finally:
        db.close()
    if owner is not None and owner.id != canary_id:
        raise AppError(
            "canary.agent_busy",
            f"a running {owner.kind} is already routing this agent's traffic — finish "
            "or clean it up first",
            {"owner_kind": owner.kind, "owner_id": owner.id},
            status_code=409,
        )


def _current_round(
    row: RuntimeCanary,
    *,
    create: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    setup = row.artifacts.get("setup") or {}
    ramp_stage = int(setup.get("ramp_stage", 0))
    rounds = copy.deepcopy(row.artifacts.get("rounds") or [])
    current = next(
        (entry for entry in rounds if entry.get("ramp_stage") == ramp_stage),
        None,
    )
    if current is None and create:
        current = {
            "ramp_stage": ramp_stage,
            "weights": dict(setup.get("weights") or {}),
            "traffic_attempts": [],
        }
        rounds.append(current)
    return rounds, current


def metric_sample_count(metrics: list[dict[str, Any]]) -> int:
    """Aggregate sample count used only as a monotonic fresh-evidence marker."""
    total = 0
    for metric in metrics:
        total += int((metric.get("control") or {}).get("sampleSize") or 0)
        total += sum(
            int(variant.get("sampleSize") or 0)
            for variant in metric.get("variants", [])
        )
    return total


def evaluator_sample_count(metrics: list[dict[str, Any]], evaluator: str) -> int:
    """:func:`metric_sample_count` of the one metric that belongs to ``evaluator``."""
    return metric_sample_count([
        m for m in metrics if metric_evaluator_id(m) == evaluator])


def stage_not_ready_reason(row: RuntimeCanary, action: str) -> str | None:
    setup = row.artifacts.get("setup")
    if action == "cleanup":
        return None
    if row.status != "running":
        return f"canary is already {row.status}"
    if action == "setup":
        return "canary setup is already complete" if setup else None
    # rollback is the safety valve — allowed for ANY running canary, even one whose
    # setup only partially completed (act_rollback tolerates a partial artifact and
    # always rolls production forward off any minted candidate).
    if action == "rollback":
        return None
    if not setup:
        return "run canary setup first"

    _, current = _current_round(row)
    attempts = (current or {}).get("traffic_attempts") or []
    if action == "traffic":
        return None
    if action == "verdict":
        return None if attempts else "send traffic at the current weights first"
    if action in {"advance", "complete"}:
        if current is None or current.get("verdict") is None:
            return "record a verdict at the current weights first"
        ramp_stage = int(setup.get("ramp_stage", 0))
        if action == "advance" and ramp_stage >= len(RAMP_WEIGHTS) - 1:
            return "the final 1/99 stage must be completed, not advanced"
        if action == "complete" and ramp_stage != len(RAMP_WEIGHTS) - 1:
            return "reach the final 1/99 stage first"
    return None


def assert_verdict_allows(
    row: RuntimeCanary,
    *,
    allow_non_significant: bool,
    terminal: bool = False,
) -> None:
    """Enforce the reviewed treatment/tie/control ramp policy.

    A **significant** ``control-wins`` is terminal — and since N-SCN-obs14
    ``compute_verdict`` only emits ``control-wins`` when BOTH arms carry ``min_arm_n``
    scored records, so a 32-vs-4 round can no longer end the ramp. ``insufficient-data``
    (no evidence at all) stays blocked. ``insufficient-n`` (evidence, but an arm below the
    bar) is weak evidence like a tie: the operator may ``advance`` through it with the
    explicit override — the next stage gives the starved arm the samples this one could
    not. N-SCN-obs15 (user ruling 2026-10-14): a **non-significant** ``control-wins``
    (``significant: false`` — statistically indistinguishable from a tie) is the same weak
    evidence and may ``advance`` with ``allow_non_significant: true``, symmetric with a
    non-significant ``treatment-wins``; an older payload without ``significant`` stays
    terminal (conservative).

    ``terminal=True`` is the ``complete`` policy: promoting to 100 % must not rest on an
    arm the bar declares unusable, so ``insufficient-n`` is blocked there even with
    ``allow_non_significant`` — the 409 tells the operator which arm is short and to send
    a ``traffic`` round with ``sessions_per_arm`` at the bar — and **any** ``control-wins``
    (significant or not) is blocked: the current version wins, there is nothing to
    promote. A canary ``complete`` under override additionally requires both arms at
    the bar (the retired configuration experiment's ``promote --force`` did not).
    """
    _, current = _current_round(row)
    verdict = (current or {}).get("verdict") or {}
    outcome = verdict.get("verdict")
    significant = verdict.get("significant")
    if outcome == "judge-mismatch":  # P3.2: the arms were judged by different models
        raise AppError(
            "canary.verdict_blocked",
            f"{outcome} cannot advance: {verdict.get('reason') or 'mixed judges'}",
            {"verdict": verdict},
            status_code=409,
        )
    if outcome == "insufficient-data":
        raise AppError(
            "canary.verdict_blocked",
            f"{outcome} cannot advance; send more traffic or roll back",
            {"verdict": verdict},
            status_code=409,
        )
    if outcome == "control-wins" and terminal:
        raise AppError(
            "canary.verdict_blocked",
            "control-wins cannot complete: the current version wins, there is nothing to "
            "promote — roll back",
            {"verdict": verdict, "terminal": True},
            status_code=409,
        )
    if outcome == "control-wins" and significant is not False:
        raise AppError(
            "canary.verdict_blocked",
            f"{outcome} cannot advance; send more traffic or roll back",
            {"verdict": verdict},
            status_code=409,
        )
    if terminal and outcome == "insufficient-n":
        arms = verdict.get("arms_n") or {}
        min_arm_n = verdict.get("min_arm_n") or get_settings().verdict_min_arm_n
        raise AppError(
            "canary.verdict_blocked",
            "insufficient-n cannot complete: promoting to production needs both arms at "
            f"the per-arm bar — send a traffic round with sessions_per_arm ≥ {min_arm_n} "
            f"(control has {arms.get('control', '?')}, "
            f"treatment has {arms.get('treatment', '?')})",
            {"verdict": verdict, "terminal": True},
            status_code=409,
        )
    # a non-significant control-wins reaches here only for `advance` (N-SCN-obs15)
    needs_override = outcome in {"tie", "insufficient-n"} or significant is False
    if outcome != "treatment-wins" and not needs_override:
        raise AppError(
            "canary.verdict_blocked",
            "the current verdict does not allow advancing",
            {"verdict": verdict},
            status_code=409,
        )
    if needs_override and not allow_non_significant:
        raise AppError(
            "canary.override_required",
            "tie, insufficient-n or non-significant evidence (including a non-significant "
            "control-wins) requires explicit operator override",
            {"verdict": verdict},
            status_code=409,
        )


CLEANUP_DECISION_CODE = "canary.cleanup_requires_decision"


def refuse_cleanup_before_decision(row: RuntimeCanary) -> None:
    """N17-obs452: 409 ``canary.cleanup_requires_decision`` for the cleanup of a RUNNING
    canary whose setup may have rolled DEFAULT — production — to the untested candidate.
    Setup's mint (``UpdateAgentRuntime``) auto-rolls DEFAULT; only the A/B pinning keeps live
    traffic on the stable endpoint, and cleanup rolls nothing forward, so cleaning up before
    a ``complete`` / ``rollback`` would silently leave production on the candidate.

    * no setup artifact (setup never reached the mint) → allowed;
    * ``v_candidate`` recorded (the mint ran) → refused;
    * a partial setup without ``v_candidate`` (setup failed — the mint may or may not have
      run): the runtime's LIVE version is read; equal to ``setup.v_current`` (DEFAULT never
      moved) → allowed; different, or unreadable → refused.

    Terminal canaries always clean up. Rollback stays available on every running canary.
    """
    if row.status != "running":
        return
    setup = (row.artifacts or {}).get("setup") or {}
    if not setup:
        return
    if setup.get("v_candidate"):
        raise AppError(
            CLEANUP_DECISION_CODE,
            "this canary is still running: complete or roll it back before cleanup — setup "
            "already moved production's DEFAULT endpoint to the candidate, and cleanup would "
            "leave production on it",
            {"canary_id": row.id, "state": "live", "v_current": setup.get("v_current"),
             "v_candidate": setup.get("v_candidate")},
            status_code=409,
        )
    live: str | None = None
    try:
        live = canary_infra.current_version(
            control_client(context_for_workspace(row.workspace_id)), setup.get("runtime_id"))
    except Exception:  # noqa: BLE001 — unknown is not "unmoved"
        live = None
    if live is not None and str(live) == str(setup.get("v_current")):
        return  # the mint never ran: DEFAULT still serves v_current
    raise AppError(
        CLEANUP_DECISION_CODE,
        "this canary's setup did not finish and production's DEFAULT endpoint may serve the "
        "candidate (live version "
        f"{live or 'unknown'}, v_current {setup.get('v_current')}): roll it back before cleanup",
        {"canary_id": row.id, "state": "partial_setup", "v_current": setup.get("v_current"),
         "live_version": live},
        status_code=409,
    )


def locked_verdict(row: RuntimeCanary) -> dict[str, Any]:
    """N17-obs451: the verdict a ``complete`` acts on — the current round's ``verdict`` /
    ``significant`` and whether it needed the operator override (the
    :func:`assert_verdict_allows` rule: a tie, an ``insufficient-n`` or a non-significant
    result). Recorded on ``artifacts.complete`` so the console words the promotion by it."""
    _, current = _current_round(row)
    verdict = (current or {}).get("verdict") or {}
    outcome = verdict.get("verdict")
    significant = verdict.get("significant")
    return {"verdict": outcome, "significant": significant,
            "override": outcome in {"tie", "insufficient-n"} or significant is False}


def _create_variant_eval(
    *,
    workspace: WorkspaceContext,
    agent: Agent,
    eval_name: str,
    resource_id: str,
    runtime_name: str,
    endpoint: str,
    progress: Progress,
    role: str,
    evaluators: list[str] | None = None,
) -> dict[str, Any]:
    """Per-variant local online eval scoped to the variant's named-endpoint telemetry
    (W-E2's ``online.create_for_owner``; sampling 100 %, 2-minute sessions). ``evaluators``
    is the canary's configured list; None → the default pair."""
    progress(step("creating_online_config", f"creating {role} online evaluation config…",
                  role=role))
    config = _online().create_for_owner(
        workspace, agent,
        name=eval_name,
        evaluators=list(evaluators or CANARY_EVALUATORS),
        log_group=canary_infra.endpoint_log_group(resource_id, endpoint),
        service_name=canary_infra.endpoint_service_name(runtime_name, endpoint),
        sampling=100.0,
        timeout=2,
    )
    return {
        "online_eval_id": getattr(config, "config_id", None),
        "online_eval_arn": getattr(config, "config_arn", None),
    }


#: The canary actions that publish or change the champion's LIVE version — `complete`
#: (production = candidate) and `rollback` (a roll-forward mint) — refused with the shared
#: `409 agent.deploy_in_progress` while a deploy job owns the agent (agents.md §1.2,
#: N17-obs13). `setup` / `traffic` / `verdict` / `advance` / `cleanup` act on the split only.
PUBLISH_ACTIONS = frozenset({"complete", "rollback"})


def refuse_publish_during_deploy(row: RuntimeCanary, action: str) -> None:
    """N17-obs13: a canary `complete` / `rollback` is a publish of the champion's live
    version like a redeploy — while the agent's deploy job is `queued` / `running` it is
    the shared `409 agent.deploy_in_progress {job_id, stage, job_status}` and writes
    nothing. Called by the action route (a plain 409 before the 202) and again inside the
    action itself (the thread's own belt against a job that landed in between)."""
    if action not in PUBLISH_ACTIONS:
        return
    db = SessionLocal()
    try:
        refuse_deploy_in_progress(db, row.champion_agent_id, then=f"{action} the canary")
    finally:
        db.close()


def stale_base(row: RuntimeCanary, agent: Agent) -> tuple[str | None, str | None] | None:
    """``(setup_version, current_version)`` when the champion's ledger version moved since
    this canary's setup (a redeploy that finished in between — refused on the route since
    N17-obs16, but a finished job, an out-of-band publish or a pre-guard canary can still
    leave it), else ``None``. A setup that recorded no ledger version (a canary set up
    before the key existed) is never stale by this rule."""
    setup = (row.artifacts or {}).get("setup") or {}
    if "agent_version" not in setup:
        return None
    recorded, current = setup.get("agent_version"), agent.version
    if (recorded is None and current is None) or str(recorded) == str(current):
        return None
    return (str(recorded) if recorded is not None else None,
            str(current) if current is not None else None)


def refuse_stale_base(row: RuntimeCanary, action: str) -> None:
    """N17-obs16 belt: `complete` would set the agent's spec / version to a candidate built
    on a base that is no longer production, `rollback` would mint off a spec the canary
    never saw — both are `409 canary.stale_base {setup_version, current_version}` once the
    champion's version moved since setup. Nothing written; the operator settles the canary
    with `cleanup` and starts a new one on the current version. Route pre-flight and the
    action thread both call it."""
    if action not in PUBLISH_ACTIONS:
        return
    db = SessionLocal()
    try:
        agent = db.get(Agent, row.champion_agent_id)
        if agent is None:
            return
        moved = stale_base(row, agent)
    finally:
        db.close()
    if moved is None:
        return
    setup_version, current_version = moved
    raise AppError(
        "canary.stale_base",
        f"the agent was re-published since this canary was set up (version "
        f"{setup_version} → {current_version}) — the candidate no longer applies to "
        f"production; clean this canary up and start a new one",
        {"setup_version": setup_version, "current_version": current_version,
         "action": action},
        status_code=409,
    )


def _refuse_system_subject(row: RuntimeCanary, action: str) -> None:
    """A canary whose champion or challenger is a system-managed preset never
    mints, promotes, rolls back or cleans up — refused before AWS is touched."""
    from app.system_agents.service import refuse_system_agent_id

    refuse_system_agent_id(row.champion_agent_id, action)
    refuse_system_agent_id(row.challenger_agent_id, action)
    meta_id = (row.artifacts or {}).get("agent_meta", {}).get("id")
    if meta_id:
        refuse_system_agent_id(meta_id, action)


# ─── the local A/B row ───────────────────────────────────────────────────────
def set_ab_execution_status(canary_id: str, status: str) -> dict[str, Any]:
    """Flip the canary's local A/B row (RUNNING | PAUSED | STOPPED); returns the row.
    A canary without a row (partial setup) answers ``{}`` — nothing to stop."""
    row = _get(canary_id)
    abtest = dict(row.artifacts.get("abtest") or {})
    if not abtest:
        return {}
    if abtest.get("execution_status") != status:
        abtest["execution_status"] = status
        abtest["status_changed_at"] = _now()
        _update(canary_id, artifact={"abtest": abtest})
    return abtest


def _stop_ab_test(canary_id: str, progress: Progress) -> dict[str, Any]:
    row = _get(canary_id)
    abtest = row.artifacts.get("abtest") or {}
    if abtest and abtest.get("execution_status") != routing.STOPPED:
        progress(step("stopping_ab_test", "stopping the Runtime canary A/B test…"))
    abtest = set_ab_execution_status(canary_id, routing.STOPPED)
    if not abtest:
        return {}
    return {**abtest, "executionStatus": abtest.get("execution_status")}


def _owner(row: RuntimeCanary) -> routing.OwnerRef:
    owner = routing.owner_from_canary(row)
    if owner is None or owner.execution_status != routing.RUNNING:
        raise RuntimeError("the canary's A/B test is not running — run setup first")
    return owner


def ab_metrics(row: RuntimeCanary, sessions: set[str] | None = None,
               ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(GetABTest-shaped result, normalized metrics)`` from ``eval_results`` ×
    assignments — the local twin of ``get_ab_test`` + ``normalize_ab_results``;
    ``sessions`` restricts it to a subset (R3-c: the held-out scenarios)."""
    owner = routing.owner_from_canary(row)
    if owner is None:
        return {"executionStatus": None, "results": {"evaluatorMetrics": []}}, []
    db = SessionLocal()
    try:
        result = routing.ab_results(db, owner, sessions=sessions)
    finally:
        db.close()
    return result, routing.normalize_ab_results(result)


# ─── actions ─────────────────────────────────────────────────────────────────
def _mint_stage_ctx(
    agent: Agent, canary_id: str, workspace: WorkspaceContext, progress: Progress,
) -> StageContext:
    """The deploy-stage context a mint computes its deploy-time policy context in (N12-obs45);
    it is left on ``scratch["deploy_policy"]`` the way the deploy stages share it."""
    return StageContext(
        agent_id=agent.id, deployment_id="", job_id=canary_id, workspace=workspace,
        log=progress,
    )


def _minted_narrowing(stage_ctx: StageContext) -> dict[str, Any] | None:
    """The ``as_dict`` snapshot of the deploy-time policy context a mint computed (None when
    the mint did not compute one — a stubbed mint, or a pre-N12-obs45 canary row)."""
    policy = stage_ctx.scratch.get("deploy_policy")
    if not isinstance(policy, DeployPolicy):
        return None
    out = policy.as_dict()
    # stream A (review nit): whether the minted version renders the principal carrier — the
    # mint's own `job_carrier_decision` — so a promote / rollback stamps what the LIVE version
    # actually renders (a rollback to a pre-carrier version keeps the agent `policy_stale`)
    decision = stage_ctx.scratch.get("principal_carrier_decision")
    if isinstance(decision, tuple) and len(decision) == 2:
        out["carrier"] = bool(decision[0])
    return out


def _mark_minted_version_applied(
    db: Any, agent: Agent, snapshot: dict[str, Any] | None, *, canary_id: str,
) -> None:
    """Production now serves a version this canary minted (``complete`` → the candidate,
    ``rollback`` → the restore): snapshot ITS narrowing onto the row exactly as the deploy
    stage does (``mark_applied`` — `policy_applied`, the `policy_stale` comparison basis,
    the `bundle.apply` journal row). Without a snapshot the row is left untouched — the
    caller commits its own writes."""
    if snapshot is None:
        db.commit()
        return
    carrier = snapshot.get("carrier")  # absent on a pre-A6 snapshot: the module rule decides
    policy_enforcement.mark_applied(
        db, agent, DeployPolicy.from_snapshot(snapshot), job_id=canary_id, operator="canary",
        carrier=bool(carrier) if isinstance(carrier, bool) else None,
    )


def act_setup(canary_id: str, progress: Progress) -> dict[str, Any]:
    """Mint the candidate version, stand up the stable/treatment named endpoints and
    their per-arm online evals, and open the local 90/10 A/B row."""
    row = _get(canary_id)
    _refuse_system_subject(row, "setup")
    meta = row.artifacts["agent_meta"]
    spec = AgentSpec(**row.artifacts["edited_spec"])
    db = SessionLocal()
    try:
        agent = db.get(Agent, meta["id"])
        if agent is None or agent.status == "deleted":
            raise RuntimeError("the agent behind this canary no longer exists")
    finally:
        db.close()

    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    runtime_id = meta["resource_id"]
    stable = f"stable{canary_id[:6]}"
    treatment = f"treat{canary_id[:6]}"

    # (1) Read the LIVE production version BEFORE the mint — UpdateAgentRuntime
    # auto-rolls DEFAULT to the candidate, so v_current must be captured first.
    progress(step("reading_current_version", "reading current production runtime version…"))
    v_current = canary_infra.current_version(control, runtime_id)

    # (2) Stable endpoint pinned to v_current, READY — created BEFORE the mint so the
    # invoke chain can serve v_current via this endpoint the moment DEFAULT rolls to
    # the (untested) candidate.
    canary_infra.ensure_endpoint_ready(
        control, runtime_id=runtime_id, endpoint_name=stable,
        version=v_current, log=progress,
    )

    # (3) Persist a PARTIAL setup NOW. routing.owner_from_canary's "provisioning form"
    # (stable endpoint present, no A/B row yet) pins every session to v_current
    # during the setup window and on any partial failure below — production never
    # lands on the untested candidate, and rollback stays available.
    partial = {
        "runtime_id": runtime_id,
        "stable_endpoint": stable,
        "v_current": v_current,
        # N17-obs16: the ledger version the candidate was built on — `complete` / `rollback`
        # refuse once the champion moved past it (`refuse_stale_base`)
        "agent_version": agent.version,
        "gateway_id": ROUTING_MODE,
    }
    _update(canary_id, stage="setup", artifact={"setup": partial})

    # (4) Mint the candidate (DEFAULT auto-rolls to it; routing keeps live traffic on
    # the stable endpoint until the A/B row goes live).
    progress(step("minting_candidate", "minting candidate runtime version…"))
    mint_ctx = _mint_stage_ctx(agent, canary_id, workspace, progress)
    _, v_candidate, candidate_key = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=spec, control_client=control, workspace=workspace,
        log=progress, canary_id=canary_id, role=canary_infra.CANDIDATE_ROLE,
        stage_ctx=mint_ctx,
    )

    # (5) Treatment endpoint pinned to the candidate, READY.
    canary_infra.ensure_endpoint_ready(
        control, runtime_id=runtime_id, endpoint_name=treatment,
        version=v_candidate, log=progress,
    )

    evaluators = scoring_evaluators(row)
    control_eval = _create_variant_eval(
        workspace=workspace, agent=agent, eval_name=f"can_{canary_id[:8]}_oec",
        resource_id=meta["resource_id"], runtime_name=meta["runtime_name"],
        endpoint=stable, progress=progress, role="control", evaluators=evaluators,
    )
    treatment_eval = _create_variant_eval(
        workspace=workspace, agent=agent, eval_name=f"can_{canary_id[:8]}_oet",
        resource_id=meta["resource_id"], runtime_name=meta["runtime_name"],
        endpoint=treatment, progress=progress, role="treatment", evaluators=evaluators,
    )

    test_name = _test_name(canary_id)
    control_weight, treatment_weight = RAMP_WEIGHTS[0]
    variants = routing.target_variants(stable, treatment, control_weight, treatment_weight)
    progress(step("opening_ab_test",
                  f"opening the local A/B test at {control_weight}/{treatment_weight}…",
                  control=control_weight, treatment=treatment_weight))
    abtest = {
        "ab_test_id": f"local_{canary_id}",
        "name": test_name,
        "variants": variants,
        "execution_status": routing.RUNNING,
        "created_at": _now(),
    }

    # Finalize by merging the live keys onto the partial setup persisted at (3), so
    # the "provisioning" route (stable endpoint only) becomes the live split
    # atomically. ``champion``/``challenger`` here mean the control/treatment
    # ENDPOINTS — the key names are preserved so traffic/verdict/advance/cleanup and
    # the console read them unchanged (``target_name`` = endpoint name).
    result = {
        **partial,
        "test_name": test_name,
        "ab_test_id": abtest["ab_test_id"],
        "champion": {"target_name": stable, **control_eval},
        "challenger": {"target_name": treatment, **treatment_eval},
        "ramp_stage": 0,
        "weights": routing.weights_of(variants),
        "v_candidate": v_candidate,
        # the zip behind v_candidate — cleanup owns it (unless it is still live)
        "candidate_s3_key": candidate_key,
        "treatment_endpoint": treatment,
        # N12-obs45: the deploy-time policy context the candidate was minted with (the
        # principal carrier) — `complete` snapshots it onto the agent as `policy_applied`
        "deploy_policy": _minted_narrowing(mint_ctx),
        # N12-obs46: the evaluators both arms are scored with (+ the primary), copied here
        # so the detail page reads one place (the default pair on a row without a choice)
        **_setup_evaluator_keys(row),
    }
    _update(canary_id, stage="setup", artifact={"setup": result, "abtest": abtest,
                                               "rounds": []})
    return result


def held_out_sessions(sessions: list[dict[str, Any]], held_out: set[str]) -> list[str]:
    """R3-c: the job's sessions that replay a held-out scenario (a cycled pass ``label#2``
    keeps its scenario's split)."""
    if not held_out:
        return []
    return [str(s["session_id"]) for s in sessions
            if str(s.get("scenario_id") or "").split("#", 1)[0] in held_out]


def act_traffic(
    canary_id: str,
    prompts: list[str | list[str]],
    dataset_info: dict[str, str],
    progress: Progress,
    scenario_ids: list[str] | None = None,
    sessions_per_arm: int | None = None,
    held_out_scenarios: set[str] | None = None,
) -> dict[str, Any]:
    """Replay the dataset as ONE pre-assigned ``batch_eval`` job: every session gets its
    arm (and therefore its endpoint ``qualifier``) here, the worker only executes.

    N-SCN-obs14 — ``sessions_per_arm`` (optional) keeps cycling the dataset inside this
    one job until every arm holds at least that many sessions, capped at
    ``SESSIONS_PER_ARM_CAP_FACTOR`` × the dataset (a 90/10 split simply cannot feed the
    treatment ten sessions from nine prompts; the cap makes that visible instead of
    looping). The dataset is always replayed at least once in full.
    """
    row = _get(canary_id)
    workspace = context_for_workspace(row.workspace_id)
    owner = _owner(row)
    _, metrics = ab_metrics(row)
    baseline_n = metric_sample_count(metrics)
    primary = primary_evaluator(row)
    primary_baseline_n = evaluator_sample_count(metrics, primary) if primary else None
    labels = list(scenario_ids or [])
    items = [
        (labels[index] if index < len(labels) else f"item_{index + 1}", prompt)
        for index, prompt in enumerate(prompts)
    ]
    planned_items = _cycle_items(items, sessions_per_arm)
    executor = eval_executor.select_executor(get_settings(), job_type="batch_eval")
    eval_executor.assert_executor_ready(executor, workspace)  # N12-bug6: worker probed now
    # the replay job scores with the canary's configured evaluators — in CN the replay is
    # what feeds the verdict, so the choice has to reach it, not only the online configs
    job, assigned = traffic.plan(
        workspace, row.artifacts.get("agent_meta") or {}, planned_items, owner,
        evaluators=scoring_evaluators(row),
    )
    if sessions_per_arm:
        assigned = _trim_to_arm_target(owner, job, assigned, len(items), sessions_per_arm)
        progress(step(
            "replaying_sessions",
            f"replaying {len(job.spec['sessions'])} sessions (target ≥{sessions_per_arm} "
            f"per arm, cap {len(planned_items)})…",
            sessions=len(job.spec["sessions"]), per_arm=sessions_per_arm,
            cap=len(planned_items)))
    extra = {"sessions_per_arm": sessions_per_arm,
             "planned_sessions": len(job.spec["sessions"])} if sessions_per_arm else {}
    # R3-c: ALL scenarios are replayed; the held-out ones are remembered for the report
    held = held_out_sessions(list((getattr(job, "spec", None) or {}).get("sessions") or []),
                             held_out_scenarios or set())
    if held:
        extra["held_out_sessions"] = held
    _prepare_canary_tokens(workspace, row, job, canary_id)
    try:
        result = traffic.run(workspace, owner, job, assigned, executor=executor,
                             progress=progress)
    except traffic.ReplayJobFailed as exc:
        # N-SCN-bug3: the sessions that DID complete are evidence for this stage — record
        # the attempt (flagged with the worker's error) before surfacing the failure; a job
        # with nothing completed leaves the round untouched.
        if int(exc.result.get("sent") or 0) > 0:
            _record_traffic_attempt(canary_id, row, {**exc.result, **extra}, dataset_info,
                                    baseline_n, error=str(exc)[:500],
                                    primary_baseline_n=primary_baseline_n)
        raise
    return _record_traffic_attempt(canary_id, row, {**result, **extra}, dataset_info,
                                   baseline_n, primary_baseline_n=primary_baseline_n)


def _prepare_canary_tokens(workspace: WorkspaceContext, row: RuntimeCanary, job: Any,
                           canary_id: str) -> None:
    """D5 (I9): canary traffic to a governed agent acts as ``System::"canary"`` — the job's
    sessions get their tokens before it is dispatched. Inert with the switch off."""
    if not get_settings().policy_gateway_interceptor:
        return
    from app.services.policy_entities import Principal

    db = SessionLocal()
    try:
        agent = db.get(Agent, row.champion_agent_id)
    finally:
        db.close()
    if agent is not None:
        eval_tokens.prepare(workspace, agent, job, principal=Principal.system("canary"),
                            channel="canary", owner_kind=eval_tokens.OWNER_CANARY,
                            owner_id=canary_id)


# Hard cap on how far ``sessions_per_arm`` may cycle one dataset within a single round.
SESSIONS_PER_ARM_CAP_FACTOR = 3


def _cycle_items(
    items: list[tuple[str, str | list[str]]], sessions_per_arm: int | None,
) -> list[tuple[str, str]]:
    """The dataset once, then repeated (labels suffixed ``#2``, ``#3``…) up to the cap
    when a per-arm target is set — enough candidates for :func:`_trim_to_arm_target`."""
    if not sessions_per_arm or not items:
        return list(items)
    cap = len(items) * SESSIONS_PER_ARM_CAP_FACTOR
    out = list(items)
    pass_no = 2
    while len(out) < cap:
        out.extend((f"{label}#{pass_no}", prompt) for label, prompt in items)
        pass_no += 1
    return out[:cap]


def _trim_to_arm_target(
    owner: routing.OwnerRef, job: Any, assigned: dict[str, Any], dataset_size: int,
    sessions_per_arm: int,
) -> dict[str, Any]:
    """Keep the first full dataset pass plus, in order, as many extra pre-assigned
    sessions as it takes for every arm to reach ``sessions_per_arm`` (or the cap); the
    surplus assignment rows are dropped the way the worker's ``reconcile`` drops
    never-invoked sessions."""
    sessions = list(job.spec.get("sessions") or [])
    counts: dict[str, int] = {}
    keep: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        arm = str(session.get("arm") or "")
        if index >= dataset_size and all(
            counts.get(a.name, 0) >= sessions_per_arm for a in owner.arms
        ):
            break
        keep.append(session)
        counts[arm] = counts.get(arm, 0) + 1
    kept_ids = {s["session_id"] for s in keep}
    surplus = [s["session_id"] for s in sessions if s["session_id"] not in kept_ids]
    if surplus:
        routing.reconcile(owner, [], planned=surplus)
    job.spec["sessions"] = keep
    return {sid: a for sid, a in assigned.items() if sid in kept_ids}


def _record_traffic_attempt(
    canary_id: str, row: RuntimeCanary, result: dict[str, Any], dataset_info: dict[str, str],
    baseline_n: int, *, error: str | None = None, primary_baseline_n: int | None = None,
) -> dict[str, Any]:
    primary = primary_evaluator(row)
    attempt = {
        **result,
        **dataset_info,
        "baseline_n": baseline_n,
        # the primary's own fresh-evidence marker: the verdict waits for IT to score this
        # stage's traffic, not just for any evaluator to (None when the row has no primary)
        **({"primary_baseline_n": primary_baseline_n} if primary else {}),
        "completed_at": _now(),
    }
    if error:
        attempt["error"] = error
    rounds, current = _current_round(row, create=True)
    assert current is not None
    current.setdefault("traffic_attempts", []).append(attempt)
    current.pop("verdict", None)
    _update(canary_id, stage="traffic", artifact={"rounds": rounds})
    return attempt


def _scoring_keys(
    configured: list[str] | None, metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    """``evaluators`` (what the verdict was configured to read) and ``unscored`` (the
    configured ones with no comparable score on both arms) — absent on legacy rows."""
    if not configured:
        return {}
    scored = {
        metric_evaluator_id(m) for m in metrics
        if (m.get("control") or {}).get("mean") is not None
        and any(v.get("mean") is not None for v in m.get("variants") or [])
    }
    return {
        "evaluators": list(configured),
        "unscored": [e for e in configured if e not in scored],
    }


def act_verdict(canary_id: str, progress: Progress) -> dict[str, Any]:
    """Wait for fresh current-stage evidence, then decide.

    N12-obs46: with a primary evaluator the wait also needs the PRIMARY's own sample count
    to have grown past the marker the traffic attempt recorded (``primary_baseline_n``) —
    another evaluator scoring this stage's traffic is not evidence the primary can decide
    on; an attempt recorded before the marker existed falls back to the aggregate marker.
    The verdict itself comes from :func:`verdict.compute_verdict` with the
    primary, whose per-arm bar (``verdict_min_arm_n``) then applies to the primary's arms."""
    row = _get(canary_id)
    rounds, current = _current_round(row)
    if current is None or not current.get("traffic_attempts"):
        raise RuntimeError("current ramp stage has no traffic attempt")
    attempt = current["traffic_attempts"][-1]
    baseline_n = int(attempt.get("baseline_n", 0))
    primary = primary_evaluator(row)
    design = (row.artifacts or {}).get("design")
    if design and design.get("decision"):
        # R3-d: the fixed-n test ran once; later rounds read the decision, never re-test
        stored = {**design["decision"], "frozen": True, "recorded_at": _now()}
        current["verdict"] = stored
        _update(canary_id, stage="verdict", artifact={"rounds": rounds})
        return stored
    primary_baseline = attempt.get("primary_baseline_n") if primary else None
    deadline = datetime.now(UTC).timestamp() + VERDICT_WAIT_S
    metrics: list[dict[str, Any]] = []
    sample_n = 0
    primary_fresh = True
    result: dict[str, Any] = {}
    while True:
        result, metrics = ab_metrics(row)
        sample_n = metric_sample_count(metrics)
        primary_fresh = primary_baseline is None or (
            evaluator_sample_count(metrics, primary or "") > int(primary_baseline))
        if sample_n > baseline_n and primary_fresh:
            break
        if datetime.now(UTC).timestamp() >= deadline:
            break
        progress(step(
            "aggregating_evidence",
            f"aggregating current-stage evidence · n {sample_n}/{baseline_n + 1} "
            f"· status {result.get('executionStatus', '?')}",
            n=sample_n, needed=baseline_n + 1,
            status=str(result.get("executionStatus", "?"))))
        _sleep(VERDICT_POLL_S)

    verdict = compute_verdict(metrics, primary=primary, design=design)
    if sample_n <= baseline_n:
        verdict = {
            "verdict": "insufficient-data",
            **reason_of(
                "no_new_samples", "no new evaluator samples arrived after current-stage traffic"),
            "n": sample_n,
            **({"primary": primary} if primary else {}),
        }
    elif not primary_fresh and verdict.get("verdict") != "insufficient-data":
        # the primary would still decide on the previous stage's evidence only
        verdict = {
            "verdict": "insufficient-data",
            **reason_of(
                "no_new_primary_samples",
                "no new primary evaluator samples arrived after current-stage traffic",
                primary=primary),
            "n": evaluator_sample_count(metrics, primary or ""),
            "primary": primary,
        }
    stored = {
        "metrics": metrics,
        **verdict,
        **_scoring_keys(configured_evaluators(row), metrics),
        "baseline_n": baseline_n,
        "recorded_at": _now(),
    }
    held = {sid for rnd in rounds for attempt in rnd.get("traffic_attempts") or []
            for sid in attempt.get("held_out_sessions") or []}
    if held:
        # R3-c: the held-out scenarios' result, reported next to the verdict (not deciding)
        _, held_metrics = ab_metrics(row, sessions=held)
        block = held_out_block(held_metrics, primary)
        if block is not None:
            stored["held_out"] = block
    current["verdict"] = stored
    artifact: dict[str, Any] = {"rounds": rounds}
    if design and stored.get("decided_at_n"):
        # R3-d: freeze the fixed-n decision (without the bulky metrics)
        artifact["design"] = {**design, "decision": {
            k: v for k, v in stored.items() if k not in ("metrics", "recorded_at")}}
    _update(canary_id, stage="verdict", artifact=artifact)
    return stored


def act_advance(
    canary_id: str,
    progress: Progress,
    *,
    allow_non_significant: bool,
) -> dict[str, Any]:
    row = _get(canary_id)
    assert_verdict_allows(row, allow_non_significant=allow_non_significant)
    setup = copy.deepcopy(row.artifacts["setup"])
    abtest = dict(row.artifacts.get("abtest") or {})
    next_stage = int(setup["ramp_stage"]) + 1
    control_weight, treatment_weight = RAMP_WEIGHTS[next_stage]
    variants = routing.reweight(
        abtest.get("variants") or routing.target_variants(
            setup["champion"]["target_name"], setup["challenger"]["target_name"]),
        control_weight=control_weight, treatment_weight=treatment_weight,
    )
    progress(step("shifting_traffic",
                  f"shifting live traffic to {control_weight}/{treatment_weight}…",
                  control=control_weight, treatment=treatment_weight))
    abtest["variants"] = variants
    setup.update(ramp_stage=next_stage, weights=routing.weights_of(variants))
    _update(canary_id, stage="ramp", artifact={"setup": setup, "abtest": abtest})
    return {"ramp_stage": next_stage, "weights": setup["weights"]}


def act_complete(
    canary_id: str,
    progress: Progress,
    *,
    allow_non_significant: bool,
) -> dict[str, Any]:
    """Promote (Option B — DEFAULT is production truth): stop the split and record
    production = candidate in the ledger.

    Setup's mint already auto-rolled the runtime's DEFAULT endpoint to the candidate
    version, so once routing stops pinning sessions to the stable endpoint production
    (invoked direct-ARN via DEFAULT) serves the candidate. Promotion only needs to (1)
    stop the A/B row and (2) make the ledger reflect that the candidate is now
    production — no stable endpoint repoint. Endpoint teardown happens in cleanup.
    """
    row = _get(canary_id)
    _refuse_system_subject(row, "complete")
    refuse_publish_during_deploy(row, "complete")  # N17-obs13: before the A/B stop
    refuse_stale_base(row, "complete")  # N17-obs16: the base must still be production
    assert_verdict_allows(
        row, allow_non_significant=allow_non_significant, terminal=True
    )
    decided = locked_verdict(row)  # N17-obs451: before the A/B stop changes nothing
    setup = row.artifacts["setup"]
    meta = row.artifacts["agent_meta"]
    stopped = _stop_ab_test(canary_id, progress)
    progress(step("recording_promotion",
                  "recording promotion in the ledger (production = candidate)…"))
    db = SessionLocal()
    try:
        agent = db.get(Agent, meta["id"])
        if agent is None or agent.status == "deleted":
            raise RuntimeError("the agent behind this canary no longer exists")
        agent.spec = copy.deepcopy(row.artifacts["edited_spec"])
        agent.version = setup["v_candidate"]
        # a canary win is a publish like any other (no deployment row, though): the
        # promoted spec joins the snapshot history, stamped with the version it made
        # live (upstream #277); `_mark_minted_version_applied` commits it below
        snap = snapshots.record_snapshot(
            db, agent, actor=_canary_actor(row), note="canary promoted"
        )
        snap.aws_version = str(setup["v_candidate"])
        # N12-obs45: the live version is the candidate — its policy context (the carrier) is
        # what `policy_applied` / `policy_stale` must describe from now on (commits)
        _mark_minted_version_applied(  # `policy_narrowing`: a pre-parity canary row's key
            db, agent, setup.get("deploy_policy") or setup.get("policy_narrowing"),
            canary_id=canary_id,
        )
    finally:
        db.close()
    result = {
        "winner": "challenger",
        "promoted_version": setup["v_candidate"],
        "ab_test_status": stopped.get("executionStatus"),
        "completed_at": _now(),
        "completed_at_stage": int(setup.get("ramp_stage", 0)),
        # N17-obs451 (additive): the verdict promoted on and whether the override carried it
        **decided,
    }
    _update(
        canary_id,
        status="completed",
        stage="complete",
        artifact={"complete": result},
        # N17-obs442: the experiment is over — its arm configs stop judging traffic
        disable_online=context_for_workspace(row.workspace_id),
    )
    return result


def _canary_actor(row: RuntimeCanary) -> creators.Creator | None:
    """Who a canary's ledger publish is attributed to: the canary's creator (the
    action thread carries no request identity); None falls back to the agent's."""
    if row.created_by is None and row.created_by_name is None:
        return None
    return creators.Creator(user_id=row.created_by, name=row.created_by_name)


def act_rollback(canary_id: str, progress: Progress) -> dict[str, Any]:
    """Rollback (roll-forward — Option B): stop the split, then re-publish the agent's
    CURRENT (unchanged) spec so DEFAULT rolls forward off the rejected candidate back
    to v_current behavior.

    Setup's mint auto-rolled DEFAULT to the rejected candidate, so a rollback that
    only stopped the split would leave DEFAULT (production truth) serving the
    untested candidate once routing stops pinning the stable endpoint. Re-publishing
    the unchanged ledger spec mints a new version whose behavior == v_current, so
    DEFAULT — and ``current_version`` — is production truth again.
    """
    row = _get(canary_id)
    _refuse_system_subject(row, "rollback")
    refuse_publish_during_deploy(row, "rollback")  # N17-obs13: before the A/B stop / mint
    refuse_stale_base(row, "rollback")  # N17-obs16: the base must still be production
    setup = row.artifacts.get("setup") or {}
    meta = row.artifacts["agent_meta"]
    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    # A partial setup (failed mid-way) has no A/B row yet — only stop one when it
    # exists; the roll-forward below runs unconditionally so DEFAULT is restored to
    # v_current whether or not the candidate was ever minted.
    stopped: dict[str, Any] = {}
    if setup.get("ab_test_id"):
        stopped = _stop_ab_test(canary_id, progress)
    db = SessionLocal()
    try:
        agent = db.get(Agent, meta["id"])
        if agent is None or agent.status == "deleted":
            raise RuntimeError("the agent behind this canary no longer exists")
        current_spec = AgentSpec(**agent.spec)
    finally:
        db.close()

    # mint_candidate_version doubles as a generic "build this spec into a new
    # runtime version" helper — re-publishing v_current's unchanged spec rolls
    # DEFAULT off the rejected candidate back to current behavior.
    progress(step("rolling_forward", "rolling production forward off the rejected candidate…"))
    mint_ctx = _mint_stage_ctx(agent, canary_id, workspace, progress)
    _, v_restored, restored_key = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=current_spec, control_client=control,
        workspace=workspace, log=progress, canary_id=canary_id,
        role=canary_infra.RESTORE_ROLE, stage_ctx=mint_ctx,
    )
    db = SessionLocal()
    try:
        fresh = db.get(Agent, meta["id"])
        if fresh is not None:
            fresh.version = v_restored
            # N17-obs2 (deploy-ops-13 live T77): the roll-forward is a publish that changes
            # the live version — it records its own snapshot row like the promote does
            # (architecture.md "Spec snapshots"), stamped with the version it made live;
            # `_mark_minted_version_applied` commits it below
            snap = snapshots.record_snapshot(
                db, fresh, actor=_canary_actor(row), note="canary rolled back"
            )
            snap.aws_version = str(v_restored)
            # N12-obs45: the restored version carries the policy context it was minted with
            _mark_minted_version_applied(
                db, fresh, _minted_narrowing(mint_ctx), canary_id=canary_id,
            )
    finally:
        db.close()
    result = {
        "winner": "champion",
        "restored_version": v_restored,
        "restored_s3_key": restored_key,
        "ab_test_status": stopped.get("executionStatus"),
        "rolled_back_at": _now(),
    }
    _update(
        canary_id,
        status="rolled_back",
        stage="rollback",
        artifact={"rollback": result},
        disable_online=workspace,  # N17-obs442
    )
    return result


def _owned_resources(row: RuntimeCanary) -> tuple[list[str], list[str]]:
    """``(endpoint names, online-eval config ids)`` this canary stands up."""
    setup = row.artifacts.get("setup") or {}
    endpoints = [
        name for name in (setup.get("stable_endpoint"), setup.get("treatment_endpoint"))
        if name
    ]
    online_eval_ids = sorted({
        target["online_eval_id"]
        for target in (setup.get("champion") or {}, setup.get("challenger") or {})
        if target.get("online_eval_id")
    })
    return endpoints, online_eval_ids


def _cleanup_candidate_objects(
    row: RuntimeCanary,
    control: Any,
    workspace: WorkspaceContext,
    progress: Progress,
) -> list[dict[str, str]]:
    """Delete the zips this canary uploaded, except the one behind the version
    production is running.

    Both mints (candidate at setup, restore at rollback) leave a ~37MB object in
    the artifacts bucket; nothing used to own them (ISSUE-011). AWS does not
    document whether a published runtime version keeps reading its S3 artifact,
    so the live version's object is always kept — deleting only superseded
    objects is safe either way. Rows written before the keys were recorded have
    nothing to delete and are reported as such.
    """
    setup = row.artifacts.get("setup") or {}
    rollback = row.artifacts.get("rollback") or {}
    owned = [
        (setup.get("candidate_s3_key"), str(setup.get("v_candidate") or "")),
        (rollback.get("restored_s3_key"), str(rollback.get("restored_version") or "")),
    ]
    owned = [(key, version) for key, version in owned if key]
    if not owned:
        return []
    runtime_id = setup.get("runtime_id")
    live_version = ""
    try:
        if runtime_id:
            live_version = canary_infra.current_version(control, runtime_id)
    except Exception as exc:  # unknown live version → keep every object
        return [{
            "category": f"s3:{key}",
            "status": "skipped",
            "detail": f"live version unknown: {type(exc).__name__}: {exc}",
        } for key, _ in owned]

    results: list[dict[str, str]] = []
    for key, version in owned:
        if version and version == live_version:
            results.append({
                "category": f"s3:{key}",
                "status": "skipped",
                "detail": f"artifact of the live version {version}",
            })
            continue
        try:
            progress(step("deleting_artifact", f"deleting candidate artifact s3://…/{key}",
                          key=key))
            canary_infra.delete_object_quiet(key, workspace)
            results.append({"category": f"s3:{key}", "status": "deleted", "detail": ""})
        except Exception as exc:
            results.append({
                "category": f"s3:{key}",
                "status": "skipped",
                "detail": f"{type(exc).__name__}: {exc}",
            })
    return results


def act_cleanup(
    canary_id: str,
    progress: Progress,
) -> list[dict[str, str]]:
    """Tear down this canary's split: stop the A/B row, delete BOTH named endpoints
    (and wait, bounded, until they stop being listed — N-SCN-obs3), the per-arm
    online-eval configs, the session assignments and the superseded zips. Under Option B
    production is invoked via the runtime's DEFAULT endpoint, so neither the stable nor
    the treatment endpoint is needed post-canary."""
    row = _get(canary_id)
    setup = row.artifacts.get("setup") or {}
    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    endpoint_names, online_eval_ids = _owned_resources(row)
    progress(step("tearing_down",
                  "tearing down the canary's A/B test, online evals and endpoints…"))
    results: list[dict[str, str]] = []
    abtest = row.artifacts.get("abtest") or {}
    if abtest.get("ab_test_id"):
        set_ab_execution_status(canary_id, routing.STOPPED)
        results.append({"category": f"abtest:{abtest['ab_test_id']}", "status": "deleted",
                        "detail": "local A/B row STOPPED"})
    if online_eval_ids:
        try:
            _online().delete_for_owner(workspace, canary_id)
            results.extend({"category": f"online-eval:{config_id}", "status": "deleted",
                            "detail": ""} for config_id in online_eval_ids)
        except Exception as exc:  # noqa: BLE001 — per-category tolerance
            # N17-obs442: a config it could not delete is DISABLED by the `cleaned` write
            results.extend({"category": f"online-eval:{config_id}", "status": "skipped",
                            "detail": f"{type(exc).__name__}: {exc} (kept, DISABLED)"}
                           for config_id in online_eval_ids)
    runtime_id = setup.get("runtime_id")
    deleted_endpoints: list[str] = []
    for endpoint_name in endpoint_names:
        if not runtime_id:
            continue
        try:
            canary_infra.delete_endpoint_quiet(
                control,
                runtime_id=runtime_id,
                endpoint_name=endpoint_name,
                log=progress,
            )
            results.append(
                {"category": f"endpoint:{endpoint_name}", "status": "deleted", "detail": ""}
            )
            deleted_endpoints.append(endpoint_name)
        except Exception as exc:
            results.append({
                "category": f"endpoint:{endpoint_name}",
                "status": "skipped",
                "detail": f"{type(exc).__name__}: {exc}",
            })
    # N-SCN-obs3: the deletes above are accepted, not finished — wait (bounded; this runs
    # on the action's background thread) until the names stop being listed, so `cleaned`
    # means gone. Whatever is still listed at the deadline is recorded, not hidden.
    endpoints_pending: list[str] = []
    if runtime_id and deleted_endpoints:
        timeout_s = get_settings().canary_cleanup_endpoints_gone_timeout_s
        progress(step(
            "waiting_endpoints_gone",
            f"waiting up to {timeout_s}s for {', '.join(deleted_endpoints)} to disappear…",
            timeout_s=timeout_s, endpoints=list(deleted_endpoints)))
        endpoints_pending = canary_infra.wait_endpoints_gone(
            control,
            runtime_id=runtime_id,
            endpoint_names=deleted_endpoints,
            timeout_s=timeout_s,
            log=progress,
        )
        gone = [name for name in deleted_endpoints if name not in endpoints_pending]
        detail = f"{', '.join(gone)} gone" if gone else ""
        if endpoints_pending:
            detail += (f"{'; ' if detail else ''}still listed after {timeout_s}s: "
                       f"{', '.join(endpoints_pending)} (DELETING is eventually consistent)")
        results.append({"category": "endpoints-gone",
                        "status": "skipped" if endpoints_pending else "deleted",
                        "detail": detail})
    dropped = routing.delete_assignments(canary_id)
    results.append({"category": "assignments", "status": "deleted",
                    "detail": f"{dropped} session assignment(s)"})
    results.extend(_cleanup_candidate_objects(row, control, workspace, progress))
    artifact: dict[str, Any] = {"cleanup": results}
    if endpoints_pending:
        artifact["endpoints_pending"] = endpoints_pending
    _update(
        canary_id,
        status="cleaned",
        stage="cleanup",
        artifact=artifact,
        disable_online=workspace,  # N17-obs442: whatever the teardown left stops judging
    )
    return results


# ─── what the architect's conversation purge reads (``assistant.purge``) ─────────
def cleanup_blockers(results: list[dict[str, str]]) -> list[dict[str, str]]:
    """The cleanup results that leave a canary-owned resource behind. ``skipped`` is
    never success; the exceptions are a candidate zip kept because it backs the live
    version (``s3:`` — not a dependency of anything, removed once the agent is gone) and
    the ``endpoints-gone`` wait marker (the purge's own AWS readback settles that)."""
    return [r for r in results
            if r.get("status") == "skipped"
            and not str(r.get("category", "")).startswith("s3:")
            and r.get("category") != "endpoints-gone"]


def retained_artifact_keys(row: RuntimeCanary) -> list[str]:
    """The S3 keys this canary uploaded (candidate at setup, restore at rollback)."""
    artifacts = row.artifacts or {}
    keys = [(artifacts.get("setup") or {}).get("candidate_s3_key"),
            (artifacts.get("rollback") or {}).get("restored_s3_key")]
    return [k for k in keys if k]


def remaining_resources(row: RuntimeCanary, workspace: WorkspaceContext) -> list[dict[str, str]]:
    """Readback of every resource this canary owns that still exists — empty only when
    the per-arm online-evaluation configs (ledger rows the scheduler serves) and BOTH
    named endpoints (AWS) are verifiably gone. ``act_cleanup`` returning is not that
    evidence: ``DeleteAgentRuntimeEndpoint`` is accepted at once and the endpoint stays
    ``DELETING`` for minutes (N-SCN-obs3). The local A/B row and the session assignments
    are ledger state the cleanup flips synchronously and are not listed. Every entry is
    ``{category, status}``; a lookup error propagates (unknown ≠ gone)."""
    setup = (row.artifacts or {}).get("setup") or {}
    out: list[dict[str, str]] = []
    for config_id in _online().config_ids_for_owner(workspace, row.id):
        out.append({"category": f"online-eval:{config_id}", "status": "ACTIVE"})
    runtime_id = setup.get("runtime_id")
    if not runtime_id:
        return out
    control = control_client(workspace)
    for name in (setup.get("stable_endpoint"), setup.get("treatment_endpoint")):
        if not name:
            continue
        try:
            endpoint = rt.get_runtime_endpoint(control, runtime_id=runtime_id, endpoint_name=name)
        except Exception as exc:
            if type(exc).__name__ in canary_infra._NOT_FOUND:
                continue  # gone — or the runtime itself is, and its endpoints with it
            raise
        out.append({"category": f"endpoint:{name}", "status": str(endpoint.get("status") or "")})
    return out
