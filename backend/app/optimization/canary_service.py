"""Independent Runtime target-canary orchestration (Model 1: one agent, two
immutable versions fronted by a dedicated per-canary Gateway)."""

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import agentcore_eval as ac
from app.models.ledger import Agent
from app.optimization import canary_harness, canary_infra
from app.optimization import service as experiment_service
from app.optimization.models import RuntimeCanary
from app.schemas.agent import AgentSpec
from app.services.agentcore import harness as hc
from app.services.agentcore.client import control_client, data_client
from app.services.memory import scoped_actor
from app.services.workspace import WorkspaceContext, context_for_workspace

Progress = Callable[[str], None]
RAMP_WEIGHTS = ((90, 10), (50, 50), (1, 99))
ASYNC_ACTIONS = frozenset(
    {"setup", "traffic", "verdict", "advance", "complete", "rollback", "cleanup"}
)


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


def _update(canary_id: str, **fields: Any) -> None:
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
        _update(canary_id, progress=message[:300])

    _refuse_system_subject(_get(canary_id), action)  # before any write or thread

    def runner() -> None:
        try:
            fn(progress)
            _update(canary_id, running_action=None, progress=None)
        except Exception as exc:
            _update(
                canary_id,
                running_action=None,
                progress=None,
                error=f"{action}: {type(exc).__name__}: {exc}"[:500],
            )

    _update(canary_id, running_action=action, error=None)
    experiment_service._spawn(runner)


def active_canary_route(agent_id: str) -> dict[str, Any] | None:
    """Cheap invoke-hot-path lookup: is there an active canary fronting this agent?

    Returns ``None`` only when no ``running`` canary has at least a stable
    endpoint provisioned (``stable_endpoint`` + ``runtime_id`` + agent arn). Two
    live forms otherwise, so a mid-setup canary never leaks DEFAULT (the untested
    candidate) into production:

    - **provisioning** — stable endpoint stood up but the gateway A/B is not live
      yet: ``{runtime_id, arn, stable_endpoint, v_current}``. invoke serves
      v_current directly via the stable endpoint.
    - **live gateway** — the above PLUS ``gateway_url`` + ``control_target`` once
      the A/B test exists. invoke routes real traffic through the gateway.

    Every key is read with ``.get()`` so a PARTIAL setup artifact (from an
    in-flight or failed setup) can never raise. ``champion_agent_id`` is indexed,
    so this is one indexed SELECT and no AWS call — safe on every invocation.
    """
    db = SessionLocal()
    try:
        row = (
            db.query(RuntimeCanary)
            .filter(
                RuntimeCanary.champion_agent_id == agent_id,
                RuntimeCanary.status == "running",
            )
            .order_by(RuntimeCanary.created_at.desc())
            .first()
        )
        if row is None:
            return None
        artifacts = row.artifacts or {}
        setup = artifacts.get("setup") or {}
        arn = (artifacts.get("agent_meta") or {}).get("arn")
        stable_endpoint = setup.get("stable_endpoint")
        runtime_id = setup.get("runtime_id")
        if not (stable_endpoint and runtime_id and arn):
            return None
        route: dict[str, Any] = {
            "kind": artifacts.get("kind") or "runtime",
            "runtime_id": runtime_id,
            "arn": arn,
            "stable_endpoint": stable_endpoint,
            "v_current": setup.get("v_current"),
        }
        gateway_url = setup.get("gateway_url")
        control_target = (setup.get("champion") or {}).get("target_name")
        # "live gateway" form only once the full setup exists (a live A/B test on
        # the gateway with the control target); otherwise stay in provisioning.
        if setup.get("ab_test_id") and gateway_url and control_target:
            route["gateway_url"] = gateway_url
            route["control_target"] = control_target
        return route
    finally:
        db.close()


def clear_stale_running_actions() -> list[str]:
    """Clear daemon-thread flags left by a backend restart."""
    db = SessionLocal()
    try:
        rows = (
            db.query(RuntimeCanary)
            .filter(RuntimeCanary.running_action.isnot(None))
            .all()
        )
        cleared: list[str] = []
        for row in rows:
            row.error = (
                f"{row.running_action}: interrupted by a backend restart "
                "— retry the action"
            )
            row.running_action = None
            row.progress = None
            cleared.append(row.id)
        db.commit()
        return cleared
    finally:
        db.close()


def _agent_meta(agent: Any, control: Any) -> dict[str, Any]:
    if agent.method == "harness":
        harness = hc.get_harness(control, agent.resource_id)
        return {
            "id": agent.id,
            "name": agent.name,
            "arn": agent.arn,
            "resource_id": agent.resource_id,
            "harness_name": harness["harnessName"],
            # the managed backing runtime names the endpoint telemetry
            "runtime_name": f"harness_{harness['harnessName']}",
            "backing_runtime_id": canary_harness.backing_runtime_id(harness),
            "canary_capability": experiment_service.canary_capability(agent),
        }
    return {
        "id": agent.id,
        "name": agent.name,
        "arn": agent.arn,
        "resource_id": agent.resource_id,
        "runtime_name": experiment_service.rt_name(control, agent.resource_id),
        "canary_capability": experiment_service.canary_capability(agent),
    }


def is_harness(row: RuntimeCanary) -> bool:
    return (row.artifacts or {}).get("kind") == "harness"


def start_harness_canary(
    agent: Any,
    *,
    control_version: str,
    treatment_version: str,
    workspace: WorkspaceContext,
    start_stage: int = 0,
) -> RuntimeCanary:
    """Ledger row for a Harness canary: two EXISTING versions of one Harness.

    ``treatment_version`` is the latest version (DEFAULT already serves it), so
    ``complete`` never has to re-point anything on AWS; ``edited_spec`` is the
    ledger spec at creation — the spec production runs once treatment wins.
    ``start_stage`` 1 opens the A/B test at 50/50: with DEFAULT already on the
    treatment, 90/10 caps no exposure and only starves the treatment of samples."""
    control = control_client(workspace)
    row = RuntimeCanary(
        workspace_id=agent.workspace_id,
        name=f"CANARY-{agent.name[:32]}",
        champion_agent_id=agent.id,
        champion_agent_name=agent.name,
        challenger_agent_id=agent.id,
        challenger_agent_name=agent.name,
        artifacts={
            "kind": "harness",
            "agent_meta": _agent_meta(agent, control),
            "harness": {
                "control_version": str(control_version),
                "treatment_version": str(treatment_version),
                "start_stage": int(start_stage),
            },
            "edited_spec": copy.deepcopy(agent.spec or {}),
            "rounds": [],
        },
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        row_id = row.id
    finally:
        db.close()
    return _get(row_id)


def start_canary(
    agent: Any,
    edited_spec: AgentSpec,
    workspace: WorkspaceContext,
    source_experiment_id: str | None = None,
) -> RuntimeCanary:
    """Create only the ledger row; setup mints the candidate version + gateway.

    Model 1 collapses champion/challenger onto ONE agent: both columns carry the
    single agent id/name so ``_out`` and existing consumers keep working. The
    candidate is described by ``edited_spec`` (stored as a dump) and materialized
    at setup, never here.
    """
    control = control_client(workspace)
    row = RuntimeCanary(
        workspace_id=agent.workspace_id,
        name=f"CANARY-{agent.name[:32]}",
        champion_agent_id=agent.id,
        champion_agent_name=agent.name,
        challenger_agent_id=agent.id,
        challenger_agent_name=agent.name,
        source_experiment_id=source_experiment_id,
        artifacts={
            "agent_meta": _agent_meta(agent, control),
            "edited_spec": edited_spec.model_dump(),
            "rounds": [],
        },
    )
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
    """No-op: each canary owns a dedicated Gateway, so there is no shared-gateway
    mutex to preflight at the setup entry point."""
    return None


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
            if not early_complete_allowed(row):
                return "reach the final 1/99 stage first"
    return None


EARLY_COMPLETE_STAGE = 1  # the 50/50 ramp stage
EARLY_COMPLETE_VERDICTS = frozenset({"treatment-wins", "tie"})


def early_complete_allowed(row: RuntimeCanary) -> bool:
    """A Harness canary may complete straight from 50/50 on a treatment-wins or tie verdict.

    The architect's Harness canary gets its traffic from Dataset replay, so the 1/99
    stage would replay almost every session into the treatment and add one more
    verdict wait without new comparative evidence — whether to run it is decided on
    the 50/50 verdict. Runtime canaries keep the full 90/10 → 50/50 → 1/99 ramp. A tie
    or a non-significant win still needs the operator override
    (``assert_verdict_allows``), exactly as advancing would; control-wins and
    insufficient evidence stay blocked."""
    if not is_harness(row):
        return False
    setup = row.artifacts.get("setup") or {}
    if int(setup.get("ramp_stage", 0)) != EARLY_COMPLETE_STAGE:
        return False
    _, current = _current_round(row)
    return ((current or {}).get("verdict") or {}).get("verdict") in EARLY_COMPLETE_VERDICTS


def assert_verdict_allows(
    row: RuntimeCanary,
    *,
    allow_non_significant: bool,
) -> None:
    """Enforce the reviewed treatment/tie/control ramp policy."""
    _, current = _current_round(row)
    verdict = (current or {}).get("verdict") or {}
    outcome = verdict.get("verdict")
    if outcome in {"control-wins", "insufficient-data", "insufficient-n"}:
        raise AppError(
            "canary.verdict_blocked",
            f"{outcome} cannot advance; send more traffic or roll back",
            {"verdict": verdict},
            status_code=409,
        )
    needs_override = outcome == "tie" or verdict.get("significant") is False
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
            "tie or non-significant evidence requires explicit operator override",
            {"verdict": verdict},
            status_code=409,
        )


def _create_variant_eval(
    *,
    control: Any,
    workspace: WorkspaceContext,
    eval_name: str,
    resource_id: str,
    runtime_name: str,
    endpoint: str,
    progress: Progress,
    role: str,
) -> dict[str, Any]:
    """Per-variant online eval scoped to the variant's named-endpoint telemetry."""
    progress(f"creating {role} online evaluation config…")
    online_eval = experiment_service.create_online_eval_idempotent(
        control,
        name=eval_name,
        log_group=canary_infra.endpoint_log_group(resource_id, endpoint),
        service_name=canary_infra.endpoint_service_name(runtime_name, endpoint),
        role_arn=workspace.resources["execution_role_arn"],
    )
    return {
        "online_eval_id": online_eval.get("onlineEvaluationConfigId"),
        "online_eval_arn": online_eval.get("onlineEvaluationConfigArn"),
    }


def _refuse_system_subject(row: RuntimeCanary, action: str) -> None:
    """A canary whose champion or challenger is a system-managed preset never
    mints, promotes, rolls back or cleans up — refused before AWS is touched."""
    from app.system_agents.service import refuse_system_agent_id

    refuse_system_agent_id(row.champion_agent_id, action)
    refuse_system_agent_id(row.challenger_agent_id, action)
    meta_id = (row.artifacts or {}).get("agent_meta", {}).get("id")
    if meta_id:
        refuse_system_agent_id(meta_id, action)


def _create_target_ab_test(
    data: Any,
    *,
    test_name: str,
    gateway_arn: str,
    role_arn: str,
    control_target: str,
    control_eval: dict[str, Any],
    treatment_eval: dict[str, Any],
    variants: list[dict[str, Any]],
    progress: Progress,
) -> dict[str, Any]:
    """Target-routing A/B test at the opening ramp weights (adopted on conflict)."""
    weights = "/".join(str(variant["weight"]) for variant in variants)
    progress(f"creating target-routing A/B test at {weights}…")
    try:
        return ac.create_ab_test(
            data,
            name=test_name,
            gatewayArn=gateway_arn,
            roleArn=role_arn,
            enableOnCreate=True,
            evaluationConfig={
                "perVariantOnlineEvaluationConfig": [
                    {
                        "name": "C",
                        "onlineEvaluationConfigArn": control_eval["online_eval_arn"],
                    },
                    {
                        "name": "T1",
                        "onlineEvaluationConfigArn": treatment_eval["online_eval_arn"],
                    },
                ]
            },
            gatewayFilter={"targetPaths": [f"/{control_target}/*"]},
            variants=variants,
        )
    except Exception as exc:
        if not experiment_service._is_conflict(exc):
            raise
        response = next(
            (
                test
                for test in experiment_service.list_ab_tests(data)
                if str(test.get("name", "")).lower() == test_name.lower()
            ),
            None,
        )
        if response is None:
            raise
        return response


def _setup_harness(canary_id: str, row: RuntimeCanary, progress: Progress) -> dict[str, Any]:
    """Harness canary setup: pin two Harness endpoints to the chosen versions, stand
    up a dedicated Gateway (trace delivery on) with one passthrough target per
    endpoint, one online eval per endpoint's telemetry, and the 90/10 A/B test.

    The control endpoint is pinned and persisted FIRST: from then on the
    "provisioning" route serves the control version to platform invokes, so a
    partial setup never leaves them on an unscored treatment.
    """
    meta = row.artifacts["agent_meta"]
    versions = row.artifacts["harness"]
    v_control, v_treatment = versions["control_version"], versions["treatment_version"]
    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    data = data_client(workspace)
    logs = workspace.client("logs")
    harness_id = meta["resource_id"]
    role_arn = workspace.resources["execution_role_arn"]
    stable = canary_harness.control_endpoint(canary_id)
    treatment = canary_harness.treatment_endpoint(canary_id)

    progress(f"pinning control endpoint {stable} → v{v_control}…")
    hc.ensure_harness_endpoint(
        control, harness_id, stable, v_control,
        description=f"Launchpad canary {canary_id[:8]} control",
    )
    partial: dict[str, Any] = {
        "runtime_id": harness_id,
        "stable_endpoint": stable,
        "v_current": v_control,
    }
    _update(canary_id, stage="setup", artifact={"setup": partial})

    gw = canary_infra.create_canary_gateway(
        control_client=control, workspace=workspace, canary_id=canary_id, log=progress
    )
    partial.update(
        gateway_id=gw["gateway_id"], gateway_arn=gw["gateway_arn"], gateway_url=gw["gateway_url"],
    )
    _update(canary_id, stage="setup", artifact={"setup": partial})
    tracing = canary_harness.enable_gateway_tracing(
        logs, gateway_id=gw["gateway_id"], gateway_arn=gw["gateway_arn"], log=progress,
    )

    progress(f"pinning treatment endpoint {treatment} → v{v_treatment}…")
    hc.ensure_harness_endpoint(
        control, harness_id, treatment, v_treatment,
        description=f"Launchpad canary {canary_id[:8]} treatment",
    )

    control_target = f"can{canary_id[:6]}c"
    treatment_target = f"can{canary_id[:6]}t"
    targets: dict[str, str] = {}
    for name, endpoint in ((control_target, stable), (treatment_target, treatment)):
        targets[name] = canary_harness.create_passthrough_target(
            control, gateway_id=gw["gateway_id"], name=name, harness_arn=meta["arn"],
            qualifier=endpoint, region=workspace.region, log=progress,
        )

    evals: dict[str, dict[str, Any]] = {}
    for role, endpoint, suffix in (("control", stable, "oec"), ("treatment", treatment, "oet")):
        log_group = canary_harness.endpoint_log_group(meta["backing_runtime_id"], endpoint)
        canary_harness.ensure_log_group(logs, log_group)
        progress(f"creating {role} online evaluation config…")
        online_eval = experiment_service.create_online_eval_idempotent(
            control,
            name=f"can_{canary_id[:8]}_{suffix}",
            log_group=log_group,
            service_name=canary_harness.endpoint_service_name(meta["harness_name"], endpoint),
            role_arn=role_arn,
        )
        evals[role] = {
            "online_eval_id": online_eval.get("onlineEvaluationConfigId"),
            "online_eval_arn": online_eval.get("onlineEvaluationConfigArn"),
        }

    test_name = _test_name(canary_id)
    start_stage = int(versions.get("start_stage", 0))
    control_weight, treatment_weight = RAMP_WEIGHTS[start_stage]
    variants = ac.target_variants(
        control_target, treatment_target,
        control_weight=control_weight, treatment_weight=treatment_weight,
    )
    response = _create_target_ab_test(
        data,
        test_name=test_name,
        gateway_arn=gw["gateway_arn"],
        role_arn=role_arn,
        control_target=control_target,
        control_eval=evals["control"],
        treatment_eval=evals["treatment"],
        variants=variants,
        progress=progress,
    )
    result = {
        **partial,
        "test_name": test_name,
        "ab_test_id": response.get("abTestId"),
        "champion": {
            "target_name": control_target, "target_id": targets[control_target],
            **evals["control"],
        },
        "challenger": {
            "target_name": treatment_target, "target_id": targets[treatment_target],
            **evals["treatment"],
        },
        "ramp_stage": start_stage,
        # < start_stage ⇒ skipped (never ran); the detail page marks those stages so
        "start_stage": start_stage,
        "weights": {variant["name"]: variant["weight"] for variant in variants},
        "v_candidate": v_treatment,
        "treatment_endpoint": treatment,
        "trace_delivery": tracing,
    }
    _update(canary_id, stage="setup", artifact={"setup": result, "rounds": []})
    return result


def act_setup(canary_id: str, progress: Progress) -> dict[str, Any]:
    """Mint the candidate version, stand up the dedicated Gateway + stable/
    treatment endpoints, and start the 90/10 target-based A/B test."""
    row = _get(canary_id)
    _refuse_system_subject(row, "setup")
    if is_harness(row):
        return _setup_harness(canary_id, row, progress)
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
    data = data_client(workspace)
    runtime_id = meta["resource_id"]
    role_arn = workspace.resources["execution_role_arn"]
    stable = f"stable{canary_id[:6]}"
    treatment = f"treat{canary_id[:6]}"

    # (1) Read the LIVE production version BEFORE the mint — UpdateAgentRuntime
    # auto-rolls DEFAULT to the candidate, so v_current must be captured first.
    progress("reading current production runtime version…")
    v_current = canary_infra.current_version(control, runtime_id)

    # (2) Dedicated per-canary gateway.
    gw = canary_infra.create_canary_gateway(
        control_client=control, workspace=workspace, canary_id=canary_id, log=progress
    )

    # (3) Stable endpoint pinned to v_current, READY — created BEFORE the mint so
    # invoke can serve v_current via this endpoint the moment DEFAULT rolls to the
    # (untested) candidate.
    canary_infra.ensure_endpoint_ready(
        control, runtime_id=runtime_id, endpoint_name=stable,
        version=v_current, log=progress,
    )

    # (4) Persist a PARTIAL setup NOW. active_canary_route's "provisioning" form
    # (stable endpoint present, no live gateway yet) keeps invoke on v_current
    # during the setup window and on any partial failure below — production never
    # lands on the untested candidate, and rollback stays available.
    partial = {
        "runtime_id": runtime_id,
        "stable_endpoint": stable,
        "v_current": v_current,
        "gateway_id": gw["gateway_id"],
        "gateway_arn": gw["gateway_arn"],
        "gateway_url": gw["gateway_url"],
    }
    _update(canary_id, stage="setup", artifact={"setup": partial})

    # (5) Mint the candidate (DEFAULT auto-rolls to it; invoke now routes to the
    # stable endpoint = v_current until the gateway A/B goes live).
    progress("minting candidate runtime version…")
    _, v_candidate, candidate_key = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=spec, control_client=control, workspace=workspace,
        log=progress, canary_id=canary_id, role="candidate",
    )

    # (6) Treatment endpoint pinned to the candidate, READY.
    canary_infra.ensure_endpoint_ready(
        control, runtime_id=runtime_id, endpoint_name=treatment,
        version=v_candidate, log=progress,
    )

    control_target = f"can{canary_id[:6]}c"
    treatment_target = f"can{canary_id[:6]}t"
    progress("creating control/treatment gateway targets + waiting READY…")
    control_target_id = experiment_service.create_runtime_target_idempotent(
        control, gw["gateway_id"], control_target, meta["arn"], qualifier=stable
    )
    treatment_target_id = experiment_service.create_runtime_target_idempotent(
        control, gw["gateway_id"], treatment_target, meta["arn"], qualifier=treatment
    )

    control_eval = _create_variant_eval(
        control=control,
        workspace=workspace,
        eval_name=f"can_{canary_id[:8]}_oec",
        resource_id=meta["resource_id"],
        runtime_name=meta["runtime_name"],
        endpoint=stable,
        progress=progress,
        role="control",
    )
    treatment_eval = _create_variant_eval(
        control=control,
        workspace=workspace,
        eval_name=f"can_{canary_id[:8]}_oet",
        resource_id=meta["resource_id"],
        runtime_name=meta["runtime_name"],
        endpoint=treatment,
        progress=progress,
        role="treatment",
    )

    test_name = _test_name(canary_id)
    variants = ac.target_variants(control_target, treatment_target)
    response = _create_target_ab_test(
        data,
        test_name=test_name,
        gateway_arn=gw["gateway_arn"],
        role_arn=role_arn,
        control_target=control_target,
        control_eval=control_eval,
        treatment_eval=treatment_eval,
        variants=variants,
        progress=progress,
    )

    # Finalize by merging the live keys onto the partial setup persisted at (4),
    # so the "provisioning" route (stable-endpoint) becomes the "live gateway"
    # route (control target + ab_test) atomically.
    result = {
        **partial,
        "test_name": test_name,
        "ab_test_id": response.get("abTestId"),
        # ``champion``/``challenger`` here mean control/treatment TARGETS — the key
        # names are preserved so act_traffic/verdict/advance/_owned_resources read
        # them unchanged.
        "champion": {"target_name": control_target, "target_id": control_target_id, **control_eval},
        "challenger": {
            "target_name": treatment_target,
            "target_id": treatment_target_id,
            **treatment_eval,
        },
        "ramp_stage": 0,
        "weights": {variant["name"]: variant["weight"] for variant in variants},
        "v_candidate": v_candidate,
        # the zip behind v_candidate — cleanup owns it (unless it is still live)
        "candidate_s3_key": candidate_key,
        "treatment_endpoint": treatment,
    }
    _update(canary_id, stage="setup", artifact={"setup": result, "rounds": []})
    return result


def _paired_harness_traffic(
    row: RuntimeCanary,
    workspace: WorkspaceContext,
    prompts: list[str],
    scenario_ids: list[str] | None,
    progress: Progress,
) -> dict[str, Any]:
    """Send every prompt to BOTH Harness endpoints (see ``canary_harness.PAIRED_MODE``).

    Each (prompt, arm) is one InvokeHarness call on the arm's named endpoint in a
    fresh session; up to ``TRAFFIC_MAX_CONCURRENCY`` run at once on daemon threads,
    while ``progress`` stays on this thread. A transient upstream error is retried
    once in a new session; any other failure marks that side of the pair, which the
    verdict then leaves out — it never fails the whole replay."""
    import queue
    import threading
    import uuid

    from app.evaluation.service import transient_invoke_error

    setup = row.artifacts["setup"]
    arn = row.artifacts["agent_meta"]["arn"]
    endpoints = {"control": setup["stable_endpoint"], "treatment": setup["treatment_endpoint"]}
    actor = scoped_actor(row.champion_agent_id, "canary-traffic")
    data = data_client(workspace)
    labels = list(scenario_ids or [])
    pairs = [
        {"index": i, "scenario_id": labels[i] if i < len(labels) else f"item_{i + 1}",
         "prompt": prompt[:160], "control_session_id": None, "treatment_session_id": None}
        for i, prompt in enumerate(prompts)
    ]
    jobs: queue.SimpleQueue[tuple[int, str]] = queue.SimpleQueue()
    for i in range(len(prompts)):
        for arm in ("control", "treatment"):
            jobs.put((i, arm))
    done: queue.SimpleQueue[tuple[int, str, str | None, str | None]] = queue.SimpleQueue()

    def worker() -> None:
        while True:
            try:
                i, arm = jobs.get_nowait()
            except queue.Empty:
                return
            error: str | None = None
            sid: str | None = None
            for attempt in range(2):
                sid = hc.new_session_id()
                try:
                    hc.invoke_harness_text(
                        data, arn, prompts[i], session_id=sid, actor_id=actor,
                        qualifier=endpoints[arm],
                        runtime_user_id=experiment_service.TRAFFIC_USER_ID,
                    )
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001 — recorded per side, never raised
                    error = (getattr(exc, "code", None) or type(exc).__name__)[:120]
                    if attempt or not transient_invoke_error(exc):
                        break
            done.put((i, arm, sid, error))

    workers = max(1, min(experiment_service.TRAFFIC_MAX_CONCURRENCY, 2 * len(prompts)))
    tag = uuid.uuid4().hex[:4]
    threads = [threading.Thread(target=worker, daemon=True, name=f"canary-pair-{tag}-{n}")
               for n in range(workers)]
    for thread in threads:
        thread.start()
    total, failed = 2 * len(prompts), 0
    for finished in range(1, total + 1):
        i, arm, sid, error = done.get()
        pairs[i][f"{arm}_session_id"] = sid
        if error:
            failed += 1
            pairs[i].setdefault("errors", {})[arm] = error
            pairs[i]["error"] = ", ".join(f"{k}: {v}" for k, v in pairs[i]["errors"].items())
        progress(f"paired replay · {finished}/{total} calls · {failed} failed")
    for thread in threads:
        thread.join()
    for p in pairs:
        p.pop("errors", None)
    return {
        "mode": canary_harness.PAIRED_MODE,
        "pairs": pairs,
        "session_ids": [p[k] for p in pairs for k in ("control_session_id", "treatment_session_id")
                        if p[k]],
        "sent": total - failed,
        "failed": failed,
        "status_counts": {"ok": total - failed, **({"error": failed} if failed else {})},
    }


def act_traffic(
    canary_id: str,
    prompts: list[str],
    dataset_info: dict[str, str],
    progress: Progress,
    scenario_ids: list[str] | None = None,
) -> dict[str, Any]:
    row = _get(canary_id)
    workspace = context_for_workspace(row.workspace_id)
    setup = row.artifacts["setup"]
    if is_harness(row):
        attempt = {
            **_paired_harness_traffic(row, workspace, prompts, scenario_ids, progress),
            **dataset_info,
            "baseline_n": 0,
            "completed_at": _now(),
        }
        rounds, current = _current_round(row, create=True)
        assert current is not None
        current.setdefault("traffic_attempts", []).append(attempt)
        current.pop("verdict", None)
        _update(canary_id, stage="traffic", artifact={"rounds": rounds})
        return attempt
    metrics = ac.normalize_ab_results(
        ac.get_ab_test(data_client(workspace), ab_test_id=setup["ab_test_id"])
    )
    baseline_n = metric_sample_count(metrics)
    harness_seams: dict[str, Any] = {}
    if is_harness(row):
        # passthrough targets relay the InvokeHarness body at /<target>/; one
        # synthetic, agent-scoped memory actor keeps replay out of real users' memory
        actor = scoped_actor(row.champion_agent_id, "canary-traffic")
        harness_seams = {
            "path_suffix": canary_harness.TARGET_PATH_SUFFIX,
            "body_for": lambda prompt, _sid: canary_harness.invoke_body(prompt, actor_id=actor),
        }
    result = experiment_service.send_gateway_traffic(
        setup["gateway_url"],
        setup["champion"]["target_name"],
        prompts,
        workspace,
        progress=progress,
        **harness_seams,
    )
    attempt = {
        **result,
        **dataset_info,
        "baseline_n": baseline_n,
        "completed_at": _now(),
    }
    rounds, current = _current_round(row, create=True)
    assert current is not None
    current.setdefault("traffic_attempts", []).append(attempt)
    current.pop("verdict", None)
    _update(canary_id, stage="traffic", artifact={"rounds": rounds})
    return attempt


# a paired verdict waits for the online evaluations (session idle 2 min + judge lag
# ~10 min) until this share of pairs is scored on every evaluator, or the deadline
PAIRED_SCORED_SHARE = 0.9
PAIRED_VERDICT_DEADLINE_S = 1800


def _paired_verdict(row: RuntimeCanary, current: dict[str, Any], progress: Progress,
                    ) -> dict[str, Any]:
    """Verdict from paired replay: each question answered by both versions is one
    unit of evidence (``canary_harness.paired_metrics``), read back from the two
    arms' online-evaluation results by session id."""
    import math

    from app.services.observability import run_insights_queries

    setup = row.artifacts["setup"]
    workspace = context_for_workspace(row.workspace_id)
    pairs = [p for a in current.get("traffic_attempts") or []
             if a.get("mode") == canary_harness.PAIRED_MODE for p in a.get("pairs") or []]
    complete = [p for p in pairs if p.get("control_session_id") and p.get("treatment_session_id")
                and (not p.get("error") or canary_harness.budget_stop_only(p["error"]))]
    config_ids = [setup["champion"]["online_eval_id"], setup["challenger"]["online_eval_id"]]
    sessions = [p[k] for p in complete for k in ("control_session_id", "treatment_session_id")]
    first = min((a.get("completed_at") or _now()) for a in current["traffic_attempts"])
    started = datetime.fromisoformat(first)
    deadline = datetime.now(UTC).timestamp() + PAIRED_VERDICT_DEADLINE_S
    scores: dict[str, dict[str, float]] = {}
    while True:
        hours = math.ceil((datetime.now(UTC) - started).total_seconds() / 3600) + 1
        queries = {
            f"q{n}": canary_harness.paired_scores_query(
                config_ids, sessions[n:n + canary_harness.SCORE_QUERY_CHUNK])
            for n in range(0, len(sessions), canary_harness.SCORE_QUERY_CHUNK)
        }
        rows = run_insights_queries(queries, hours, workspace=workspace) if queries else {}
        scores = canary_harness.parse_paired_scores([r for v in rows.values() for r in v])
        evaluators = {e for s in scores.values() for e in s}
        scored = min(
            (sum(1 for p in complete
                 if e in scores.get(p["control_session_id"], {})
                 and e in scores.get(p["treatment_session_id"], {}))
             for e in evaluators),
            default=0,
        )
        if complete and evaluators and scored >= PAIRED_SCORED_SHARE * len(complete):
            break
        if datetime.now(UTC).timestamp() >= deadline:
            break
        progress(f"waiting for online evaluation of paired sessions · {scored}/{len(complete)} "
                 "pairs scored (judge lag ≈ 10 min)")
        experiment_service._sleep(45)
    metrics, rows_out = canary_harness.paired_metrics(
        complete, scores, polarity=ac.evaluator_polarity)
    verdict = experiment_service.compute_verdict(metrics)
    if not metrics:
        verdict = {"verdict": "insufficient-data",
                   "reason": "no paired session was scored by both online evaluations", "n": 0}
    return {
        "metrics": metrics,
        **verdict,
        "mode": canary_harness.PAIRED_MODE,
        "pairs_sent": len(pairs),
        "pairs_complete": len(complete),
        "pairs": rows_out,
        "baseline_n": 0,
        "recorded_at": _now(),
    }


def act_verdict(canary_id: str, progress: Progress) -> dict[str, Any]:
    row = _get(canary_id)
    setup = row.artifacts["setup"]
    rounds, current = _current_round(row)
    if current is None or not current.get("traffic_attempts"):
        raise RuntimeError("current ramp stage has no traffic attempt")
    if current["traffic_attempts"][-1].get("mode") == canary_harness.PAIRED_MODE:
        stored = _paired_verdict(row, current, progress)
        current["verdict"] = stored
        _update(canary_id, stage="verdict", artifact={"rounds": rounds})
        return stored
    baseline_n = int(current["traffic_attempts"][-1].get("baseline_n", 0))
    data = data_client(context_for_workspace(row.workspace_id))
    deadline = datetime.now(UTC).timestamp() + 900
    metrics: list[dict[str, Any]] = []
    sample_n = 0
    result: dict[str, Any] = {}
    while True:
        result = ac.get_ab_test(data, ab_test_id=setup["ab_test_id"])
        metrics = ac.normalize_ab_results(result)
        sample_n = metric_sample_count(metrics)
        if sample_n > baseline_n:
            break
        if datetime.now(UTC).timestamp() >= deadline:
            break
        progress(
            f"aggregating current-stage evidence · n {sample_n}/{baseline_n + 1} "
            f"· status {result.get('executionStatus', '?')}"
        )
        experiment_service._sleep(45)

    verdict = experiment_service.compute_verdict(metrics)
    if sample_n <= baseline_n:
        verdict = {
            "verdict": "insufficient-data",
            "reason": "no new evaluator samples arrived after current-stage traffic",
            "n": sample_n,
        }
    stored = {
        "metrics": metrics,
        **verdict,
        "baseline_n": baseline_n,
        "recorded_at": _now(),
    }
    current["verdict"] = stored
    _update(canary_id, stage="verdict", artifact={"rounds": rounds})
    return stored


def act_advance(
    canary_id: str,
    progress: Progress,
    *,
    allow_non_significant: bool,
) -> dict[str, Any]:
    row = _get(canary_id)
    assert_verdict_allows(
        row, allow_non_significant=allow_non_significant
    )
    setup = copy.deepcopy(row.artifacts["setup"])
    next_stage = int(setup["ramp_stage"]) + 1
    control_weight, treatment_weight = RAMP_WEIGHTS[next_stage]
    variants = ac.target_variants(
        setup["champion"]["target_name"],
        setup["challenger"]["target_name"],
        control_weight=control_weight,
        treatment_weight=treatment_weight,
    )
    progress(f"updating experiment Gateway traffic to {control_weight}/{treatment_weight}…")
    experiment_service.update_weights_with_pause(
        data_client(context_for_workspace(row.workspace_id)),
        setup["ab_test_id"],
        variants,
    )
    setup.update(
        ramp_stage=next_stage,
        weights={variant["name"]: variant["weight"] for variant in variants},
    )
    _update(canary_id, stage="ramp", artifact={"setup": setup})
    return {"ramp_stage": next_stage, "weights": setup["weights"]}


def act_complete(
    canary_id: str,
    progress: Progress,
    *,
    allow_non_significant: bool,
) -> dict[str, Any]:
    """Promote (Option B — DEFAULT is production truth): stop the test and record
    production = candidate in the ledger.

    Setup's mint already auto-rolled the runtime's DEFAULT endpoint to the
    candidate version, so production (invoked direct-ARN via DEFAULT) already
    serves the candidate. Promotion only needs to (1) stop the A/B test and (2)
    make the ledger reflect that the candidate is now production — no stable
    endpoint repoint. Endpoint teardown happens in cleanup.
    """
    row = _get(canary_id)
    _refuse_system_subject(row, "complete")
    assert_verdict_allows(
        row, allow_non_significant=allow_non_significant
    )
    setup = row.artifacts["setup"]
    meta = row.artifacts["agent_meta"]
    stopped = experiment_service._stop_ab_test(
        data_client(context_for_workspace(row.workspace_id)),
        setup["ab_test_id"],
        progress,
        label="Runtime canary A/B test",
    )
    progress("recording promotion in the ledger (production = candidate)…")
    db = SessionLocal()
    try:
        agent = db.get(Agent, meta["id"])
        if agent is None or agent.status == "deleted":
            raise RuntimeError("the agent behind this canary no longer exists")
        agent.spec = copy.deepcopy(row.artifacts["edited_spec"])
        agent.version = setup["v_candidate"]
        db.commit()
    finally:
        db.close()
    result = {
        "winner": "challenger",
        "promoted_version": setup["v_candidate"],
        "ab_test_status": stopped.get("executionStatus"),
        "completed_at": _now(),
        # < the last ramp stage ⇒ a Harness canary completed early from 50/50
        "completed_at_stage": int(setup.get("ramp_stage", 0)),
    }
    _update(
        canary_id,
        status="completed",
        stage="complete",
        artifact={"complete": result},
    )
    return result


def act_rollback(canary_id: str, progress: Progress) -> dict[str, Any]:
    """Rollback (roll-forward — Option B): stop the test, then re-publish the
    agent's CURRENT (unchanged) spec so DEFAULT rolls forward off the rejected
    candidate back to v_current behavior.

    Setup's mint auto-rolled DEFAULT to the rejected candidate, so a rollback
    that only stopped the test would leave DEFAULT (production truth) serving the
    untested candidate. Re-publishing the unchanged ledger spec mints a new
    version whose behavior == v_current, so DEFAULT — and ``current_version`` —
    is production truth again.
    """
    row = _get(canary_id)
    _refuse_system_subject(row, "rollback")
    if is_harness(row):
        return _rollback_harness(canary_id, row, progress)
    setup = row.artifacts.get("setup") or {}
    meta = row.artifacts["agent_meta"]
    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    # A partial setup (failed mid-way) may have no A/B test yet — only stop one
    # when it exists; the roll-forward below runs unconditionally so DEFAULT is
    # restored to v_current whether or not the candidate was ever minted.
    stopped: dict[str, Any] = {}
    if setup.get("ab_test_id"):
        stopped = experiment_service._stop_ab_test(
            data_client(workspace),
            setup["ab_test_id"],
            progress,
            label="Runtime canary A/B test",
        )
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
    progress("rolling production forward off the rejected candidate…")
    _, v_restored, restored_key = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=current_spec, control_client=control,
        workspace=workspace, log=progress, canary_id=canary_id, role="restore",
    )
    db = SessionLocal()
    try:
        fresh = db.get(Agent, meta["id"])
        if fresh is not None:
            fresh.version = v_restored
            db.commit()
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
    )
    return result


def _prompt_text(system_prompt: Any) -> str:
    """HarnessSystemPrompt ``[{text}]`` → the ledger's single system_prompt string."""
    if isinstance(system_prompt, list):
        return "\n\n".join(
            str(block.get("text") or "") for block in system_prompt if isinstance(block, dict)
        ).strip()
    return str(system_prompt or "")


def _rollback_harness(canary_id: str, row: RuntimeCanary, progress: Progress) -> dict[str, Any]:
    """Harness rollback (roll-forward): stop the test, then re-publish the CONTROL
    version's behaviour (GetHarness at that version → UpdateHarness) so DEFAULT —
    which already serves the rejected treatment — mints a new version that behaves
    like control. The ledger spec's system prompt follows it."""
    setup = row.artifacts.get("setup") or {}
    meta = row.artifacts["agent_meta"]
    v_control = row.artifacts["harness"]["control_version"]
    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    stopped: dict[str, Any] = {}
    if setup.get("ab_test_id"):
        stopped = experiment_service._stop_ab_test(
            data_client(workspace), setup["ab_test_id"], progress, label="Harness canary A/B test",
        )
    harness_id = meta["resource_id"]
    progress(f"re-publishing Harness v{v_control} as the production version…")
    config = hc.get_harness(control, harness_id, version=v_control)
    hc.update_harness(control, canary_harness.rollback_params(config, harness_id))
    ready = hc.wait_harness_ready(control, harness_id, timeout_s=600)
    v_restored = str(ready.get("harnessVersion") or "")
    db = SessionLocal()
    try:
        agent = db.get(Agent, meta["id"])
        if agent is not None:
            spec = dict(agent.spec or {})
            spec["system_prompt"] = _prompt_text(config.get("systemPrompt"))
            agent.spec = spec
            agent.version = v_restored or agent.version
            db.commit()
    finally:
        db.close()
    result = {
        "winner": "champion",
        "restored_version": v_restored,
        "restored_from_version": v_control,
        "ab_test_status": stopped.get("executionStatus"),
        "rolled_back_at": _now(),
    }
    _update(canary_id, status="rolled_back", stage="rollback", artifact={"rollback": result})
    return result


def _owned_resources(
    row: RuntimeCanary,
    control: Any,
    data: Any,
) -> tuple[str | None, list[str], list[str], list[str]]:
    setup = row.artifacts.get("setup") or {}
    gateway_id = setup.get("gateway_id")

    target_names = {f"can{row.id[:6]}c", f"can{row.id[:6]}t"}
    target_ids = {
        target.get("target_id")
        for target in [setup.get("champion") or {}, setup.get("challenger") or {}]
        if target.get("target_id")
    }
    if gateway_id:
        for target in control.list_gateway_targets(
            gatewayIdentifier=gateway_id
        ).get("items", []):
            if target.get("name") in target_names and target.get("targetId"):
                target_ids.add(target["targetId"])

    eval_prefix = f"can_{row.id[:8]}_"
    online_eval_ids = {
        config["onlineEvaluationConfigId"]
        for config in control.list_online_evaluation_configs().get(
            "onlineEvaluationConfigs", []
        )
        if str(config.get("onlineEvaluationConfigName", "")).startswith(eval_prefix)
    }
    for target in [setup.get("champion") or {}, setup.get("challenger") or {}]:
        if target.get("online_eval_id"):
            online_eval_ids.add(target["online_eval_id"])

    ab_test_ids = {
        test["abTestId"]
        for test in experiment_service.list_ab_tests(data)
        if str(test.get("name", "")).lower() == _test_name(row.id).lower()
        and test.get("abTestId")
    }
    if setup.get("ab_test_id"):
        ab_test_ids.add(setup["ab_test_id"])
    return (
        gateway_id,
        sorted(target_ids),
        sorted(online_eval_ids),
        sorted(ab_test_ids),
    )


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
            progress(f"deleting candidate artifact s3://…/{key}")
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
    """Tear down this canary's dedicated gateway, targets, BOTH named endpoints,
    online-eval configs, and A/B test. Under Option B production is invoked via
    the runtime's DEFAULT endpoint, so neither the stable nor the treatment
    endpoint is needed post-canary — both are deleted."""
    row = _get(canary_id)
    setup = row.artifacts.get("setup") or {}
    workspace = context_for_workspace(row.workspace_id)
    control = control_client(workspace)
    data = data_client(workspace)
    gateway_id, target_ids, online_eval_ids, ab_test_ids = _owned_resources(
        row, control, data
    )
    progress("tearing down canary-owned A/B test, evaluators, and targets…")
    for ab_test_id in ab_test_ids:
        try:
            experiment_service._stop_ab_test(
                data,
                ab_test_id,
                progress,
                label="Runtime canary A/B test",
            )
        except Exception:
            pass
    # ac.cleanup_resources keeps delete_gateway=False (it must never delete a
    # shared gateway); the dedicated per-canary gateway is deleted explicitly
    # below after its targets drain.
    results = ac.cleanup_resources(
        control,
        data,
        ab_test_ids=ab_test_ids,
        online_eval_ids=online_eval_ids,
        gateway_id=gateway_id,
        target_ids=target_ids,
        delete_gateway=False,
    )
    runtime_id = setup.get("runtime_id")
    harness = is_harness(row)
    endpoint_names = [setup.get("stable_endpoint"), setup.get("treatment_endpoint")]
    if harness:
        # a failed setup may have pinned endpoints it never recorded
        endpoint_names = [
            canary_harness.control_endpoint(row.id), canary_harness.treatment_endpoint(row.id),
        ]
        runtime_id = runtime_id or row.artifacts["agent_meta"]["resource_id"]
    for endpoint_name in endpoint_names:
        if not (endpoint_name and runtime_id):
            continue
        try:
            status = "deleted"
            if harness:
                progress(f"deleting Harness endpoint {endpoint_name}…")
                if not hc.delete_harness_endpoint(control, runtime_id, endpoint_name):
                    status = "absent"
            else:
                canary_infra.delete_endpoint_quiet(
                    control,
                    runtime_id=runtime_id,
                    endpoint_name=endpoint_name,
                    log=progress,
                )
            results.append(
                {"category": f"endpoint:{endpoint_name}", "status": status, "detail": ""}
            )
        except Exception as exc:
            results.append({
                "category": f"endpoint:{endpoint_name}",
                "status": "skipped",
                "detail": f"{type(exc).__name__}: {exc}",
            })
    if gateway_id and harness:
        results.extend(canary_harness.disable_gateway_tracing(workspace.client("logs"), gateway_id))
    if gateway_id:
        progress("deleting the dedicated canary gateway (draining A/B test)…")
        try:
            canary_infra.delete_canary_gateway(control, gateway_id, log=progress)
            results.append({"category": f"gateway:{gateway_id}", "status": "deleted", "detail": ""})
        except Exception as exc:
            results.append({
                "category": f"gateway:{gateway_id}",
                "status": "skipped",
                "detail": f"{type(exc).__name__}: {exc}",
            })
    results.extend(_cleanup_candidate_objects(row, control, workspace, progress))
    _update(
        canary_id,
        status="cleaned",
        stage="cleanup",
        artifact={"cleanup": results},
    )
    return results
