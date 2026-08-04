"""Optimization loop orchestration (adapted from agentcore_eva_opt
routers/abtest.py + recommend.py + bundles.py — github.com/xiehust/agentcore_eva_opt).

Stepwise actions, each user-triggered (stage = furthest point completed):
    recommend → accept → bundles → gateway → abtest → traffic → verdict
    → promote → cleanup
Long actions run on a daemon thread via run_action, streaming a progress
line onto the experiment row (running_action/progress) so the UI can poll
and a reload resumes mid-action. Short actions run inline in the request.

The experiment gateway is separate from launchpad-gw: AWS_IAM auth, no
protocolType, targets of type http→agentcoreRuntime so A/B routing happens
at {gatewayUrl}/{target}/invocations.
"""

import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer.pipeline import create_deployment, execute_deploy_job
from app.evaluation import agentcore_eval as ac
from app.evaluation.scenarios import scenario_prompts
from app.models.ledger import Agent, Deployment, Job
from app.optimization.models import Experiment
from app.schemas.agent import AgentSpec
from app.services.agentcore.client import control_client, data_client
from app.services.agentcore.gateway import sigv4_post
from app.services.harness_convert import graft_config_bundle

EXP_GATEWAY_NAME = "launchpad-exp-gw"

# Online evaluation for an experiment: the arms are scored by whatever evaluators
# the operator picks at the GATEWAY stage. The default pair is what every
# experiment used before the set became selectable.
ONLINE_EVAL_DEFAULT = ("Builtin.GoalSuccessRate", "Builtin.Helpfulness")
ONLINE_EVAL_MAX = 10  # CreateOnlineEvaluationConfig caps the list at 10

_sleep = time.sleep  # injectable


def _is_conflict(exc: Exception) -> bool:
    return type(exc).__name__ == "ConflictException"


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ in {"ResourceNotFoundException", "NotFoundException"}


def _update(exp_id: str, **fields: Any) -> None:
    db = SessionLocal()
    try:
        exp = db.get(Experiment, exp_id)
        artifacts = fields.pop("artifact", None)
        if artifacts:
            merged = dict(exp.artifacts)
            merged.update(artifacts)
            exp.artifacts = merged
        for key, value in fields.items():
            setattr(exp, key, value)
        db.commit()
    finally:
        db.close()


def _get(exp_id: str) -> Experiment:
    db = SessionLocal()
    try:
        return db.get(Experiment, exp_id)
    finally:
        db.close()


Progress = Callable[[str], None]


def _spawn(target: Callable[[], None]) -> None:  # injectable for tests
    threading.Thread(target=target, daemon=True).start()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def promotion_complete(artifacts: dict[str, Any]) -> bool:
    promote = artifacts.get("promote") or {}
    return bool(
        promote.get("deployment_id")
        and promote.get("ab_test_status") == "STOPPED"
    )


def legacy_promotion(artifacts: dict[str, Any]) -> bool:
    promote = artifacts.get("promote") or {}
    return bool(promote.get("after_weights") and not promotion_complete(artifacts))


def run_action(exp_id: str, action: str, fn: Callable[[Progress], Any]) -> None:
    """Run a stage action on a daemon thread with row-level progress.

    The wrapped fn persists its own artifact + stage on success; this runner
    only owns the running_action/progress/error lifecycle. A failure keeps
    the stage so re-POSTing the same action retries it (AWS creates are
    idempotent — conflict-adopt by name).
    """
    def progress(msg: str) -> None:
        _update(exp_id, progress=msg[:300])

    def runner() -> None:
        try:
            fn(progress)
            _update(exp_id, running_action=None, progress=None)
        except Exception as exc:
            # the action prefix lets the UI pin the failure to its button
            _update(exp_id, running_action=None, progress=None,
                    error=f"{action}: {type(exc).__name__}: {exc}"[:500])

    _update(exp_id, running_action=action, error=None)
    _spawn(runner)


def clear_stale_running_actions() -> list[str]:
    """Startup sweep: a restarted worker can't still be running an action.

    Action threads are daemons of the previous process — after a restart a
    non-null running_action is stale and would 409 every retry forever. Clear
    it and leave a retryable error so the UI shows what happened.
    """
    db = SessionLocal()
    try:
        rows = db.query(Experiment).filter(
            Experiment.running_action.isnot(None)
        ).all()
        cleared: list[str] = []
        for exp in rows:
            exp.error = (f"{exp.running_action}: interrupted by a backend "
                         "restart — retry the action")
            exp.running_action = None
            exp.progress = None
            cleared.append(exp.id)
        db.commit()
        return cleared
    finally:
        db.close()


# strands `@tool def name(...):` followed by a docstring — the summary line is
# the tool description the model sees, so it is what a recommendation improves.
_TOOL_DEF_RE = re.compile(
    r"@tool\s*\ndef\s+(\w+)\s*\([^)]*\)[^:]*:\s*\n\s+(?:\"\"\"|''')([\s\S]*?)(?:\"\"\"|''')"
)


def discover_agent_tools(spec: dict[str, Any]) -> dict[str, str]:
    """toolName → current description, from the agent's own spec.

    Sources: registry tool attachments (spec.tools) and `@tool` docstrings in
    the agent's code / code bundle. Gateway-served tools (KB targets, MCP)
    only exist at runtime and can't be discovered here — the recommend UI
    lets the user add those by hand.
    """
    tools: dict[str, str] = {}
    for entry in spec.get("tools") or []:
        if isinstance(entry, dict) and entry.get("name"):
            tools[str(entry["name"])] = str(
                entry.get("description") or entry.get("desc") or ""
            )
    sources = [spec.get("code")] if isinstance(spec.get("code"), str) else []
    bundle = spec.get("code_bundle")
    if isinstance(bundle, dict):
        sources += [s for s in bundle.values() if isinstance(s, str)]
    for src in sources:
        for name, doc in _TOOL_DEF_RE.findall(src or ""):
            # docstring summary only — Args/Returns sections are signature
            # docs, not part of the description contract
            summary = re.split(r"\n\s*\n|\n\s*(?:Args|Returns|Raises):", doc)[0]
            tools.setdefault(name, " ".join(summary.split())[:500])
    tools.update({
        str(name): str(description)
        for name, description in (spec.get("tool_description_overrides") or {}).items()
    })
    return tools


def experiment_capability(agent_row: Any) -> dict[str, Any]:
    """Backend-owned config-bundle experiment capability projection."""
    spec = agent_row.spec or {}
    base = {
        "eligible": False,
        "system_prompt": False,
        "tool_descriptions": False,
        "reason": None,
        "reason_code": None,
    }
    if agent_row.method != "zip_runtime":
        return {
            **base,
            "reason_code": "not-http-runtime",
            "reason": (
                "Only Launchpad-managed HTTP runtime agents support "
                "config-bundle experiments."
            ),
        }
    if spec.get("protocol", "http") != "http":
        return {
            **base,
            "reason_code": "a2a",
            "reason": "A2A protocol agents do not consume routed configuration bundles.",
        }
    if spec.get("source_harness"):
        from app.services.harness_convert import GRAFT_START, has_config_bundle_graft

        main_py = (spec.get("code_bundle") or {}).get("main.py", "")
        if not has_config_bundle_graft(main_py):
            return {
                **base,
                "reason_code": "missing-graft",
                "reason": "This converted runtime is missing the Launchpad config-bundle graft.",
            }
        return {
            **base,
            "eligible": True,
            "system_prompt": True,
            "tool_descriptions": GRAFT_START in main_py,
        }
    if spec.get("code") or spec.get("code_bundle"):
        return {
            **base,
            "reason_code": "custom-source-unverified",
            "reason": (
                "Custom runtime source is not verified to consume "
                "Launchpad configuration bundles."
            ),
        }
    return {
        **base,
        "eligible": True,
        "system_prompt": True,
        "tool_descriptions": True,
    }


def canary_capability(agent_row: Any) -> dict[str, Any]:
    """Backend-owned target-canary subject capability projection.

    Model 1 canaries mint a candidate version of ONE agent, so the subject must
    be a runtime whose candidate can be minted from an edited spec. Container
    candidate minting needs a CodeBuild image push (a follow-up), so only
    ``zip_runtime`` / ``studio`` are eligible today.
    """
    base = {"eligible": False, "reason": None, "reason_code": None}
    if agent_row.status != "active":
        return {
            **base,
            "reason_code": "not-active",
            "reason": "Canary agent must be active.",
        }
    if agent_row.method not in {"zip_runtime", "container", "studio"}:
        return {
            **base,
            "reason_code": "not-runtime",
            "reason": "Target canaries require an AgentCore Runtime agent.",
        }
    if agent_row.method == "container":
        return {
            **base,
            "reason_code": "container-followup",
            "reason": "Container canary candidate minting via CodeBuild is a follow-up.",
        }
    if (agent_row.spec or {}).get("protocol", "http") != "http":
        return {
            **base,
            "reason_code": "a2a",
            "reason": "A2A agents are not compatible with HTTP target-canary traffic.",
        }
    if ":runtime/" not in str(agent_row.arn or ""):
        return {
            **base,
            "reason_code": "no-runtime-arn",
            "reason": "The agent has no deployed AgentCore Runtime ARN.",
        }
    return {**base, "eligible": True}


def _agent_meta(exp: Experiment) -> dict[str, Any]:
    """Runtime facts captured at create time; rebuilt lazily for old rows."""
    meta = exp.artifacts.get("agent_meta")
    if meta and "tools" in meta and "experiment_capability" in meta:
        return meta
    from app.models.ledger import Agent  # local import — avoids cycle at module load

    db = SessionLocal()
    try:
        agent_row = db.get(Agent, exp.agent_id)
    finally:
        db.close()
    if meta:  # old row — backfill newer projections, keep captured facts
        if agent_row is not None:
            meta = {
                **meta,
                "tools": discover_agent_tools(agent_row.spec or {}),
                "experiment_capability": experiment_capability(agent_row),
            }
            _update(exp.id, artifact={"agent_meta": meta})
        return meta
    if agent_row is None:
        raise RuntimeError("agent behind this experiment no longer exists")
    control = control_client()
    meta = {
        "id": agent_row.id,
        "name": agent_row.name,
        "arn": agent_row.arn,
        "resource_id": agent_row.resource_id,
        "runtime_name": rt_name(control, agent_row.resource_id),
        "system_prompt": (agent_row.spec or {}).get("system_prompt", ""),
        "tools": discover_agent_tools(agent_row.spec or {}),
        "experiment_capability": experiment_capability(agent_row),
    }
    _update(exp.id, artifact={"agent_meta": meta})
    return meta


def _noop(_msg: str) -> None:
    pass


# ─── stage implementations ───────────────────────────────────────────────────
REC_TYPES = ("system_prompt", "tool_descriptions")

# artifact keys owned by each recommendation type — a re-generation of one
# type replaces exactly these and leaves the other type's output in place
_REC_KEYS: dict[str, tuple[str, ...]] = {
    "system_prompt": ("system_prompt_status", "system_prompt_error",
                      "recommended_prompt", "explanation"),
    "tool_descriptions": ("tool_status", "tool_error", "tool_descriptions",
                          "analyzed_tools"),
}


def stage_recommend(
    exp_id: str,
    agent: dict[str, Any],
    progress: Progress = _noop,
    types: tuple[str, ...] = REC_TYPES,
    tools: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    data = data_client()
    log_group = f"/aws/bedrock-agentcore/runtimes/{agent['resource_id']}-DEFAULT"
    log_group_arns = [
        ac.to_log_group_arn(log_group, settings.region, settings.account_id),
        ac.to_log_group_arn("aws/spans", settings.region, settings.account_id),
    ]
    service_names = [f"{agent['runtime_name']}.DEFAULT"]
    current_prompt = agent["system_prompt"]
    out: dict[str, Any] = {}

    # regeneration is now a first-class flow — job names get a per-run suffix
    # so a re-run never collides with the job an earlier run created
    run_tag = uuid.uuid4().hex[:6]

    if "system_prompt" in types:
        progress("generating system-prompt recommendation from recent traces…")
        sp = ac.start_system_prompt_recommendation(
            data,
            name=f"exp_{exp_id[:8]}_sp_{run_tag}",
            system_prompt=current_prompt,
            log_group_arns=log_group_arns,
            service_names=service_names,
        )
        sp_result = ac.poll_recommendation(
            data, recommendation_id=sp["recommendationId"], max_polls=45
        )
        sp_payload = sp_result.get("recommendationResult", {}).get(
            "systemPromptRecommendationResult", {}
        )
        sp_status = sp_result.get("status") or ""
        sp_out = sp_payload.get("recommendedSystemPrompt") or ""
        if sp_status == "COMPLETED" and sp_out:
            out.update(
                system_prompt_status=sp_status,
                recommended_prompt=sp_out[:4000],
                explanation=sp_payload.get("explanation", "")[:600],
            )
        else:
            # a job AWS did not complete (safety-filter ValidationException,
            # too few traces, …) has no recommendation — never substitute
            # invented text for it, or the failure reads as a success
            # downstream (bundles/A-B would run on text no optimizer produced)
            out.update(
                system_prompt_status=sp_status or "FAILED",
                system_prompt_error=_rec_error(sp_payload, sp_status)[:300],
            )

    if "tool_descriptions" in types:
        # the optimizer improves descriptions for the tools it is handed —
        # they must be the agent's real tools, or it has nothing to match
        # against in the traces
        analyzed = tools or agent.get("tools") or {}
        out["analyzed_tools"] = analyzed
        if not analyzed:
            out["tool_status"] = "no-tools"
            out["tool_descriptions"] = {}
        else:
            try:
                progress("generating tool-description recommendation…")
                suggestions, status, err = _run_tool_recommendation(
                    data, exp_id, run_tag, analyzed,
                    log_group_arns, service_names,
                )
                # the job rejects the WHOLE tool list when any listed tool is
                # absent from the sampled traces (live-verified
                # ValidationException) — retry once with only traced tools
                missing = _tools_not_in_traces(err)
                remaining = {k: v for k, v in analyzed.items()
                             if k not in missing}
                if status != "COMPLETED" and missing and remaining:
                    progress("retrying without tools absent from traces: "
                             f"{sorted(missing)}…")
                    out["analyzed_tools"] = remaining
                    suggestions, status, err = _run_tool_recommendation(
                        data, exp_id, f"{run_tag}r", remaining,
                        log_group_arns, service_names,
                    )
                if status == "COMPLETED":
                    out["tool_status"] = "COMPLETED"
                    out["tool_descriptions"] = suggestions
                else:
                    out["tool_status"] = "error"
                    out["tool_error"] = (
                        err or f"recommendation job ended {status}")[:300]
                    out["tool_descriptions"] = {}
            except Exception as exc:
                out["tool_status"] = "error"
                out["tool_error"] = f"{type(exc).__name__}: {exc}"[:200]
                out["tool_descriptions"] = {}

    return out


def _run_tool_recommendation(
    data: Any, exp_id: str, tag: str, tools: dict[str, str],
    log_group_arns: list[str], service_names: list[str],
) -> tuple[dict[str, str], str, str]:
    """One tool-description job → (suggestions, job status, error text)."""
    td = ac.start_tool_description_recommendation(
        data,
        name=f"exp_{exp_id[:8]}_td_{tag}",
        tools=[{"toolName": k, "description": v} for k, v in tools.items()],
        log_group_arns=log_group_arns,
        service_names=service_names,
    )
    result = ac.poll_recommendation(
        data, recommendation_id=td["recommendationId"], max_polls=30
    )
    payload = result.get("recommendationResult", {}).get(
        "toolDescriptionRecommendationResult", {}
    )
    suggestions: dict[str, str] = {}
    for tool in payload.get("tools", []):
        name = tool.get("toolName", "")
        desc = tool.get("recommendedToolDescription", "")
        if name and desc:
            suggestions[name] = desc
    err = payload.get("errorMessage") or ""
    if payload.get("errorCode"):
        err = f"{payload['errorCode']}: {err}" if err else str(payload["errorCode"])
    return suggestions, result.get("status") or "COMPLETED", err


_NOT_TRACED_RE = re.compile(
    r"not found in the sampled agent traces: \[([^\]]*)\]"
)


def _tools_not_in_traces(err: str) -> set[str]:
    m = _NOT_TRACED_RE.search(err or "")
    if not m:
        return set()
    return {p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()}


def _rec_error(payload: dict[str, Any], status: str) -> str:
    """Operator-facing failure text for a recommendation AWS did not complete."""
    code = payload.get("errorCode") or ""
    msg = payload.get("errorMessage") or ""
    if code and msg:
        return f"{code}: {msg}"
    return code or msg or f"recommendation job ended {status or 'unknown'}"


def system_prompt_rec_failed(rec: dict[str, Any]) -> bool:
    """True when the stored system-prompt recommendation is a failed one.

    A row carrying neither key never ran the prompt generator (tool-only run)
    or predates the status field — both count as not-failed, so old rows and
    tool-description-only experiments keep accepting as before.
    """
    status = rec.get("system_prompt_status")
    if status is None and not rec.get("system_prompt_error"):
        return False
    return status != "COMPLETED" or not rec.get("recommended_prompt")


DEFAULT_TOOL_DESCS = {"calculator": "Evaluate a basic arithmetic expression"}


def create_bundle_idempotent(control: Any, **kwargs: Any) -> dict[str, Any]:
    """create_configuration_bundle with conflict-adopt — a retried bundles
    action after a partial failure must re-use the bundle it already made."""
    try:
        return ac.create_configuration_bundle(control, **kwargs)
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        name = kwargs["bundle_name"]
        match = None
        token: str | None = None
        while match is None:
            page = control.list_configuration_bundles(
                **({"nextToken": token} if token else {})
            )
            match = next((b for b in page.get("bundles", [])
                          if b.get("bundleName") == name), None)
            token = page.get("nextToken")
            if match is None and not token:
                raise
        detail = control.get_configuration_bundle(bundleId=match["bundleId"])
        return {"bundleId": match["bundleId"],
                "bundleArn": match.get("bundleArn"),
                "versionId": detail.get("versionId")}


def stage_bundles(
    exp_id: str, agent: dict[str, Any], treatment_prompt: str,
    treatment_tool_descs: dict[str, str] | None = None,
) -> dict:
    control = control_client()
    current_prompt = agent["system_prompt"]
    # control mirrors production: the agent's own tool descriptions; treatment
    # overlays the accepted edits on that same base
    current_descs = agent.get("tools") or DEFAULT_TOOL_DESCS
    control_bundle = create_bundle_idempotent(
        control,
        agent_arn=agent["arn"],
        bundle_name=f"exp_{exp_id[:8]}_control",
        system_prompt=current_prompt,
        tool_descriptions=current_descs,
        commit_message="control — current production config",
    )
    treatment_bundle = create_bundle_idempotent(
        control,
        agent_arn=agent["arn"],
        bundle_name=f"exp_{exp_id[:8]}_treatment",
        system_prompt=treatment_prompt,
        tool_descriptions={**current_descs, **(treatment_tool_descs or {})},
        commit_message="treatment — accepted recommendation",
    )
    return {
        "control": {
            "bundle_id": control_bundle.get("bundleId"),
            "arn": control_bundle.get("bundleArn"),
            "version": control_bundle.get("versionId") or "1",
        },
        "treatment": {
            "bundle_id": treatment_bundle.get("bundleId"),
            "arn": treatment_bundle.get("bundleArn"),
            "version": treatment_bundle.get("versionId") or "1",
        },
    }


def create_runtime_target_idempotent(
    control: Any, gateway_id: str, name: str, agent_arn: str,
    qualifier: str = "DEFAULT",
) -> str:
    """Create (or adopt on conflict) an http-runtime gateway target.

    ``qualifier`` selects the runtime endpoint the target fronts — DEFAULT
    (auto-follows latest) by default; a named endpoint pins a version, which
    the target-based canary uses for its stable/treatment variant pair.
    """
    try:
        target = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            targetConfiguration={
                "http": {"agentcoreRuntime": {"arn": agent_arn, "qualifier": qualifier}}
            },
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            clientToken=str(uuid.uuid4()),
        )
        target_id = target["targetId"]
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        items = control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
        target_id = next(t["targetId"] for t in items if t.get("name") == name)
    for _ in range(30):
        detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        if detail.get("status") == "READY":
            return target_id
        _sleep(5)
    raise TimeoutError(f"target {name} not READY")


def normalize_online_evaluators(
    ids: Sequence[str] | None, control: Any | None = None
) -> list[str]:
    """Validate an operator-chosen evaluator set for online evaluation.

    ``None``/empty falls back to :data:`ONLINE_EVAL_DEFAULT`. Online evaluation
    scores live traces, which carry no ground truth, so two families are rejected
    here rather than halfway through the gateway stage: the built-in trajectory
    matchers, and custom judges whose instructions reference a ground-truth
    placeholder (``{expected_response}`` & friends — AWS refuses those in an
    online evaluation config too, but only at ``CreateOnlineEvaluationConfig``,
    after :func:`stage_gateway` has already created the gateway and the runtime
    target).

    Inspecting a custom judge costs one ``GetEvaluator``, so it happens after
    dedup and the count cap and only for non-``Builtin.`` ids — a built-in-only
    selection, the common case, makes no AWS call at all.
    """
    chosen: list[str] = []
    for raw in ids or ():
        evaluator = str(raw).strip()
        if not evaluator or evaluator in chosen:
            continue
        if evaluator in ac.TRAJECTORY_EVALUATORS:
            raise AppError(
                "experiment.evaluator_unsupported",
                f"{evaluator} scores against dataset ground truth, which online "
                "evaluation does not carry — use a batch evaluation run instead",
                {"evaluator": evaluator},
                status_code=400,
            )
        if evaluator.startswith("Builtin.") and evaluator not in ac.ALL_BUILTIN_EVALUATORS:
            raise AppError(
                "experiment.evaluator_unsupported",
                f"unknown built-in evaluator {evaluator}",
                {"evaluator": evaluator},
                status_code=400,
            )
        chosen.append(evaluator)
    if not chosen:
        return list(ONLINE_EVAL_DEFAULT)
    if len(chosen) > ONLINE_EVAL_MAX:
        raise AppError(
            "experiment.evaluator_unsupported",
            f"online evaluation accepts at most {ONLINE_EVAL_MAX} evaluators, "
            f"got {len(chosen)}",
            {"count": len(chosen)},
            status_code=400,
        )
    custom = [e for e in chosen if not e.startswith("Builtin.")]
    if custom:
        client = control or control_client()
        for evaluator in custom:
            _assert_no_ground_truth(client, evaluator)
    return chosen


def _assert_no_ground_truth(control: Any, evaluator: str) -> None:
    """Reject a custom judge that needs ground truth it will never get online."""
    try:
        detail = ac.get_evaluator(control, evaluator_id=evaluator)
    except Exception as exc:
        if _is_not_found(exc):
            raise AppError(
                "experiment.evaluator_unsupported",
                f"unknown evaluator {evaluator}",
                {"evaluator": evaluator},
                status_code=400,
            ) from exc
        # a control-plane blip must not block gateway creation: AWS enforces the
        # same constraint server-side, so fail open and let it have the last word
        return
    placeholders = ac.ground_truth_placeholders(ac.judge_instructions(detail))
    if placeholders:
        rendered = ", ".join(f"{{{p}}}" for p in placeholders)
        raise AppError(
            "experiment.evaluator_unsupported",
            f"{evaluator} references {rendered}, which is ground truth online "
            "evaluation does not carry — use a batch evaluation run instead",
            {"evaluator": evaluator, "placeholders": placeholders},
            status_code=400,
        )


def create_online_eval_idempotent(
    control: Any, *, name: str, log_group: str, service_name: str, role_arn: str,
    evaluators: Sequence[str] | None = None,
) -> dict[str, Any]:
    try:
        return control.create_online_evaluation_config(
            onlineEvaluationConfigName=name,
            description=f"Launchpad experiment online eval · {name}",
            dataSourceConfig={
                "cloudWatchLogs": {
                    "logGroupNames": [log_group],
                    "serviceNames": [service_name],
                }
            },
            evaluators=[
                {"evaluatorId": e}
                for e in (evaluators or ONLINE_EVAL_DEFAULT)
            ],
            rule={
                "samplingConfig": {"samplingPercentage": 100.0},
                "sessionConfig": {"sessionTimeoutMinutes": 2},
            },
            evaluationExecutionRoleArn=role_arn,
            enableOnCreate=True,
            clientToken=str(uuid.uuid4()),
        )
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        configs = control.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
        return next(
            c for c in configs if c.get("onlineEvaluationConfigName") == name
        )


def find_experiment_gateway(control: Any | None = None) -> dict[str, Any] | None:
    """Return the live shared experiment Gateway detail without creating it."""
    control = control or control_client()
    items = control.list_gateways(maxResults=100).get("items", [])
    summary = next((g for g in items if g.get("name") == EXP_GATEWAY_NAME), None)
    if summary is None:
        return None
    return control.get_gateway(gatewayIdentifier=summary["gatewayId"])


def ensure_experiment_gateway(
    progress: Progress = _noop,
    control: Any | None = None,
) -> dict[str, Any]:
    """Create or adopt the shared experiment Gateway and wait until READY."""
    settings = get_settings()
    control = control or control_client()
    progress("creating experiment gateway…")
    try:
        gateway = control.create_gateway(
            name=EXP_GATEWAY_NAME,
            description="Launchpad experiment gateway (A/B routing)",
            authorizerType="AWS_IAM",
            roleArn=settings.resources["gateway_role_arn"],
            clientToken=str(uuid.uuid4()),
        )
        gateway_id = gateway["gatewayId"]
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        existing = find_experiment_gateway(control)
        if existing is None:
            raise
        gateway_id = existing["gatewayId"]

    progress("waiting for gateway READY…")
    for _ in range(30):
        detail = control.get_gateway(gatewayIdentifier=gateway_id)
        if detail.get("status") == "READY":
            return {
                "gateway_id": gateway_id,
                "gateway_arn": detail["gatewayArn"],
                "gateway_url": detail["gatewayUrl"],
            }
        _sleep(5)
    raise TimeoutError(f"gateway {gateway_id} not READY")


def list_ab_tests(data: Any | None = None) -> list[dict[str, Any]]:
    """List every A/B test, following the preview API's nextToken pagination."""
    data = data or data_client()
    tests: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        response = (
            data.list_ab_tests(nextToken=token)
            if token
            else data.list_ab_tests()
        )
        tests.extend(response.get("abTests", []))
        token = response.get("nextToken")
        if not token:
            return tests


def assert_gateway_available(
    gateway_arn: str,
    *,
    own_test_name: str | None = None,
    data: Any | None = None,
) -> None:
    """Reject a foreign active A/B test on the shared Gateway."""
    active = [
        test
        for test in list_ab_tests(data)
        if test.get("gatewayArn") == gateway_arn
        and test.get("executionStatus") != "STOPPED"
        and (
            own_test_name is None
            or str(test.get("name", "")).lower() != own_test_name.lower()
        )
    ]
    if active:
        raise AppError(
            "experiment.gateway_busy",
            "the shared experiment Gateway already has an active A/B test",
            {
                "gateway_arn": gateway_arn,
                "active_tests": [
                    {
                        "id": test.get("abTestId"),
                        "name": test.get("name"),
                        "execution_status": test.get("executionStatus"),
                    }
                    for test in active
                ],
            },
            status_code=409,
        )


def assert_shared_gateway_available(
    *,
    own_test_name: str | None = None,
    control: Any | None = None,
    data: Any | None = None,
) -> None:
    """Read-only preflight used before an action starts AWS mutation."""
    gateway = find_experiment_gateway(control)
    if gateway is not None:
        assert_gateway_available(
            gateway["gatewayArn"], own_test_name=own_test_name, data=data
        )


def stage_gateway(
    exp_id: str, agent: dict[str, Any], progress: Progress = _noop,
    evaluators: Sequence[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    control = control_client()
    gateway = ensure_experiment_gateway(progress, control)
    gateway_id = gateway["gateway_id"]

    target_v1 = f"exp{exp_id[:6]}v1"
    progress("creating v1 runtime target…")
    target_id = create_runtime_target_idempotent(control, gateway_id, target_v1, agent["arn"])
    log_group = f"/aws/bedrock-agentcore/runtimes/{agent['resource_id']}-DEFAULT"
    chosen = normalize_online_evaluators(evaluators, control)
    progress(f"creating online evaluation config ({len(chosen)} evaluators)…")
    online_eval = create_online_eval_idempotent(
        control,
        name=f"exp_{exp_id[:8]}_oe1",
        log_group=log_group,
        service_name=f"{agent['runtime_name']}.DEFAULT",
        role_arn=settings.resources["execution_role_arn"],
        evaluators=chosen,
    )
    return {
        **gateway,
        "target_v1": target_v1,
        "target_id_v1": target_id,
        "online_eval_arn": online_eval.get("onlineEvaluationConfigArn"),
        "online_eval_id": online_eval.get("onlineEvaluationConfigId"),
        # what the two arms are scored on — a claimed pre-existing config keeps
        # its own set, so this records the request, not a re-read from AWS
        "online_evaluators": chosen,
    }


def stage_abtest(exp_id: str, gateway_art: dict, bundle_art: dict) -> dict[str, Any]:
    settings = get_settings()
    data = data_client()
    test_name = f"exp_{exp_id[:8]}_bundle"
    assert_gateway_available(
        gateway_art["gateway_arn"], own_test_name=test_name, data=data
    )
    variants = ac.config_bundle_variants(
        bundle_art["control"]["arn"],
        bundle_art["control"]["version"],
        bundle_art["treatment"]["arn"],
        bundle_art["treatment"]["version"],
    )
    try:
        response = ac.create_ab_test(
            data,
            name=test_name,
            gatewayArn=gateway_art["gateway_arn"],
            roleArn=settings.resources["execution_role_arn"],
            enableOnCreate=True,
            evaluationConfig={"onlineEvaluationConfigArn": gateway_art["online_eval_arn"]},
            variants=variants,
        )
    except Exception as exc:
        if not _is_conflict(exc):
            raise
        response = next(
            (
                test
                for test in list_ab_tests(data)
                if str(test.get("name", "")).lower() == test_name.lower()
            ),
            None,
        )
        if response is None:
            assert_gateway_available(
                gateway_art["gateway_arn"], own_test_name=test_name, data=data
            )
            raise
    return {"ab_test_id": response.get("abTestId"), "variants": variants}


def send_gateway_traffic(
    gateway_url: str, target: str, prompts: list[str],
    poster: Any = None, signer: Any = None, progress: Progress = _noop,
) -> dict[str, Any]:
    """SigV4 POST each prompt through the experiment gateway (A/B routes them)."""
    url = f"{gateway_url.rstrip('/')}/{target}/invocations"
    session_ids: list[str] = []
    failed = 0
    for prompt in prompts:
        session_id = str(uuid.uuid4())
        response = sigv4_post(
            url,
            {"prompt": prompt, "sessionId": session_id},
            session_id=session_id,
            poster=poster,
            signer=signer,
        )
        if response.status_code == 200:
            session_ids.append(session_id)
        else:
            failed += 1
        progress(f"sent {len(session_ids)}/{len(prompts)} ({failed} failed)")
    return {"session_ids": session_ids, "sent": len(session_ids), "failed": failed}


def compute_verdict(metrics: list[dict[str, Any]], min_n: int = 3) -> dict[str, Any]:
    """Honest small-n verdict from normalized A/B metrics.

    Evaluators do not all point the same way: ``Builtin.Refusal`` and the other
    penalty scores are better *lower*, so each raw ``t_mean - c_mean`` is
    oriented by :func:`ac.evaluator_polarity` before it is averaged. ``avg_delta``
    is therefore polarity-normalized — positive always favours treatment,
    whatever the evaluator measures — and custom judges count as higher-is-better
    (AWS exposes no direction for them).

    The average is weighted by ``min`` of the two arms' sample sizes: a delta is
    only as trustworthy as its smaller arm, so an evaluator that produced two
    scores no longer outvotes one that produced forty.
    """
    if not metrics:
        return {"verdict": "insufficient-data", "reason": "no evaluator metrics yet"}
    weighted_delta = 0.0
    weight_total = 0.0
    total_n = 0
    significant = False
    for metric in metrics:
        polarity = ac.evaluator_polarity(
            metric.get("evaluatorId") or metric.get("label") or ""
        )
        control = metric.get("control", {})
        for variant in metric.get("variants", []):
            c_mean, t_mean = control.get("mean"), variant.get("mean")
            c_n = control.get("sampleSize") or 0
            v_n = variant.get("sampleSize") or 0
            total_n += c_n + v_n
            if c_mean is not None and t_mean is not None:
                # means with no reported sample size still carry evidence — keep
                # them in the average at unit weight rather than dropping them
                weight = float(min(c_n, v_n)) or 1.0
                weighted_delta += polarity * (t_mean - c_mean) * weight
                weight_total += weight
            if variant.get("isSignificant"):
                significant = True
    if not weight_total:
        return {"verdict": "insufficient-data", "reason": "arms have no means yet"}
    avg_delta = weighted_delta / weight_total
    if total_n < min_n * 2:
        return {"verdict": "insufficient-n", "avg_delta": round(avg_delta, 4),
                "n": total_n}
    winner = "treatment" if avg_delta > 0 else ("control" if avg_delta < 0 else "tie")
    return {
        "verdict": f"{winner}-wins" if winner != "tie" else "tie",
        "avg_delta": round(avg_delta, 4),
        "n": total_n,
        "significant": significant,
    }


# ─── stepwise actions ────────────────────────────────────────────────────────
ASYNC_ACTIONS = frozenset(
    {
        "recommend", "gateway", "abtest", "traffic", "verdict", "promote",
        "cleanup",
    }
)

_ACTION_PREREQS: dict[str, tuple[str, str]] = {
    # action → (artifact that must exist, reason returned on 409)
    "accept": ("recommend", "run recommend first"),
    "gateway": ("bundles", "create the bundles first"),
    "abtest": ("gateway", "create the gateway first"),
    "traffic": ("abtest", "create the A/B test first"),
    "verdict": ("traffic", "send traffic first"),
    "promote": ("verdict", "wait for the verdict first"),
}


def stage_not_ready_reason(exp: Experiment, action: str) -> str | None:
    """None when the action's prerequisite artifact exists, else the reason."""
    if action == "bundles":
        rec = exp.artifacts.get("recommend") or {}
        # old auto-pipeline rows never wrote accepted_* — an existing bundles
        # artifact keeps their retry path open
        if rec.get("accepted_prompt") or "bundles" in exp.artifacts:
            return None
        return "accept a recommendation first"
    rule = _ACTION_PREREQS.get(action)
    if rule and rule[0] not in exp.artifacts:
        return rule[1]
    return None


def act_recommend(
    exp_id: str,
    progress: Progress,
    types: Sequence[str] | None = None,
    tools: dict[str, str] | None = None,
) -> None:
    exp = _get(exp_id)
    sel = tuple(t for t in REC_TYPES if t in (types or REC_TYPES))
    rec = stage_recommend(exp_id, _agent_meta(exp), progress,
                          types=sel, tools=tools)
    # merge over the prior artifact: the type(s) just generated replace their
    # own keys; the other type's output and any earlier accept survive
    merged = dict(exp.artifacts.get("recommend") or {})
    for t in sel:
        for key in _REC_KEYS[t]:
            merged.pop(key, None)
    merged.update(rec)
    _update(exp_id, stage="recommend", artifact={"recommend": merged})


def action_accept(
    exp: Experiment, prompt: str, tool_descriptions: dict[str, str] | None
) -> dict[str, Any]:
    """Persist the (possibly user-edited) recommendation; unlocks bundles."""
    rec = dict(exp.artifacts.get("recommend") or {})
    rec["accepted_prompt"] = prompt[:4000]
    if tool_descriptions:
        rec["accepted_tool_descriptions"] = {
            str(k): str(v) for k, v in tool_descriptions.items()
        }
    _update(exp.id, stage="bundles", artifact={"recommend": rec})
    return rec


def action_bundles(exp: Experiment) -> dict[str, Any]:
    rec = exp.artifacts.get("recommend") or {}
    treatment_prompt = rec.get("accepted_prompt") or rec.get("recommended_prompt") or ""
    result = stage_bundles(
        exp.id, _agent_meta(exp), treatment_prompt,
        rec.get("accepted_tool_descriptions"),
    )
    _update(exp.id, stage="bundles", artifact={"bundles": result})
    return result


def act_gateway(
    exp_id: str, progress: Progress, evaluators: Sequence[str] | None = None
) -> None:
    exp = _get(exp_id)
    result = stage_gateway(exp_id, _agent_meta(exp), progress, evaluators=evaluators)
    _update(exp_id, stage="gateway", artifact={"gateway": result})


def act_abtest(exp_id: str, progress: Progress) -> None:
    exp = _get(exp_id)
    progress("creating config-bundle A/B test (50/50)…")
    result = stage_abtest(exp_id, exp.artifacts["gateway"], exp.artifacts["bundles"])
    _update(exp_id, stage="abtest", artifact={"abtest": result})


def resolve_traffic_prompts(dataset: Any) -> list[str]:
    """Extract sendable prompts from an EvalDataset (legacy/predefined only)."""
    if dataset.kind == "simulated":
        raise ValueError("simulated datasets need an actor loop — pick a "
                         "predefined or legacy prompt dataset")
    prompts: list[str] = []
    for item in dataset.items or []:
        if dataset.kind == "predefined":
            # a scenario's first user turn — reuse the eval replay extractor so
            # dict inputs ({"content"|"prompt": …}, imported JSON) unwrap the
            # same way here as when the dataset is replayed for evaluation
            turn_prompts = [p for p in scenario_prompts(item) if p.strip()]
            text = turn_prompts[0] if turn_prompts else ""
        else:
            text = str(item.get("prompt") or "")
        if text.strip():
            prompts.append(text.strip())
    if not prompts:
        raise ValueError("dataset has no usable prompts")
    return prompts


def act_traffic(
    exp_id: str, prompts: list[str], dataset_info: dict[str, str],
    progress: Progress,
) -> None:
    exp = _get(exp_id)
    gateway = exp.artifacts["gateway"]
    result = send_gateway_traffic(
        gateway["gateway_url"], gateway["target_v1"],
        prompts,
        progress=progress,
    )
    result.update(dataset_info)
    _update(exp_id, stage="traffic", artifact={"traffic": result})


def act_verdict(exp_id: str, progress: Progress) -> None:
    exp = _get(exp_id)
    ab_test_id = exp.artifacts["abtest"]["ab_test_id"]
    data = data_client()
    deadline = time.time() + 900
    metrics: list[dict[str, Any]] = []
    while True:
        result = ac.get_ab_test(data, ab_test_id=ab_test_id)
        metrics = ac.normalize_ab_results(result)
        if compute_verdict(metrics)["verdict"] not in ("insufficient-data",):
            break
        if time.time() >= deadline:
            break
        progress(f"aggregating · status {result.get('executionStatus', '?')} — "
                 "results take ~10–15 min after the last session")
        _sleep(45)
    verdict = compute_verdict(metrics)
    _update(exp_id, status="ready", stage="verdict",
            artifact={"verdict": {"metrics": metrics, **verdict}})


def start_experiment(agent_row: Any) -> Experiment:
    """Create the experiment row only — every stage waits for its action."""
    control = control_client()
    agent_meta = {
        "id": agent_row.id,
        "name": agent_row.name,
        "arn": agent_row.arn,
        "resource_id": agent_row.resource_id,
        "runtime_name": rt_name(control, agent_row.resource_id),
        "system_prompt": (agent_row.spec or {}).get("system_prompt", ""),
        "tools": discover_agent_tools(agent_row.spec or {}),
        "experiment_capability": experiment_capability(agent_row),
    }
    db = SessionLocal()
    try:
        exp = Experiment(
            name=f"EXP-{agent_row.name[:20]}", agent_id=agent_row.id,
            agent_name=agent_row.name, artifacts={"agent_meta": agent_meta},
        )
        db.add(exp)
        db.commit()
        exp_id = exp.id
    finally:
        db.close()
    return _get(exp_id)


def rt_name(control: Any, runtime_id: str) -> str:
    return control.get_agent_runtime(agentRuntimeId=runtime_id)["agentRuntimeName"]


# ─── explicit actions ────────────────────────────────────────────────────────
def update_weights_with_pause(data: Any, ab_test_id: str, variants: list[dict]) -> None:
    """Weights can only change while PAUSED/NOT_STARTED — pause, update, resume.

    The pause transition is asynchronous; wait for the status to actually flip
    before touching the variants.
    """
    ab = ac.get_ab_test(data, ab_test_id=ab_test_id)
    was_running = ab.get("executionStatus") == "RUNNING"
    if was_running:
        data.update_ab_test(abTestId=ab_test_id, executionStatus="PAUSED")
        for _ in range(30):
            if ac.get_ab_test(data, ab_test_id=ab_test_id).get(
                "executionStatus"
            ) == "PAUSED":
                break
            _sleep(3)
    ac.update_ab_test_weights(data, ab_test_id=ab_test_id, variants=variants)
    if was_running:
        data.update_ab_test(abTestId=ab_test_id, executionStatus="RUNNING")


def _stop_ab_test(
    data: Any,
    ab_test_id: str,
    progress: Progress,
    label: str = "A/B test",
) -> dict[str, Any]:
    current = ac.get_ab_test(data, ab_test_id=ab_test_id)
    if current.get("executionStatus") != "STOPPED":
        progress(f"stopping {label}…")
        data.update_ab_test(abTestId=ab_test_id, executionStatus="STOPPED")
    for _ in range(60):
        current = ac.get_ab_test(data, ab_test_id=ab_test_id)
        status = current.get("executionStatus")
        if status == "STOPPED":
            return current
        progress(f"waiting for A/B test to stop · status {status or '?'}")
        _sleep(3)
    raise TimeoutError(f"{label} {ab_test_id} did not reach STOPPED")


def act_promote(exp_id: str, progress: Progress) -> dict[str, Any]:
    """Stop the A/B test, apply treatment defaults, and deploy in place."""
    _update(exp_id, status="ready")
    exp = _get(exp_id)
    data = data_client()
    ab_test_id = exp.artifacts["abtest"]["ab_test_id"]
    stopped = _stop_ab_test(data, ab_test_id, progress)
    prior = exp.artifacts.get("promote") or {}
    attempt = {
        "ab_test_id": ab_test_id,
        "ab_test_status": stopped.get("executionStatus"),
        "stopped_at": _now(),
    }
    _update(
        exp_id,
        artifact={"promotion_attempt": attempt},
    )

    rec = exp.artifacts.get("recommend") or {}
    meta = exp.artifacts.get("agent_meta") or {}
    db = SessionLocal()
    try:
        agent = db.get(Agent, exp.agent_id)
        if agent is None or agent.status == "deleted":
            raise RuntimeError("production agent no longer exists")
        spec_data = dict(agent.spec or {})
        prompt = str(
            rec.get("accepted_prompt")
            or rec.get("recommended_prompt")
            or meta.get("system_prompt")
            or spec_data.get("system_prompt")
            or ""
        ).strip()
        accepted_tools = {
            str(name): str(description)
            for name, description in (
                rec.get("accepted_tool_descriptions") or {}
            ).items()
        }
        overrides = dict(spec_data.get("tool_description_overrides") or {})
        overrides.update(accepted_tools)
        spec_data.update({
            "name": agent.name,
            "method": agent.method,
            "system_prompt": prompt,
            "tool_description_overrides": overrides,
        })
        spec = AgentSpec(**spec_data)
        if spec.source_harness:
            bundle = dict(spec.code_bundle or {})
            if "main.py" not in bundle:
                raise RuntimeError("converted runtime bundle has no main.py")
            bundle["main.py"] = graft_config_bundle(
                bundle["main.py"],
                default_system_prompt=prompt,
                tool_description_overrides=overrides,
            )
            spec = spec.model_copy(update={"code_bundle": bundle})
        agent.spec = spec.model_dump()
        agent.status = "deploying"
        agent.error = None
        deployment, job = create_deployment(
            db, agent, mode="update", skip_register=True
        )
        deployment_id = deployment.id
        job_id = job.id
    finally:
        db.close()

    _update(
        exp_id,
        artifact={
            "promotion_attempt": {
                **attempt,
                "deployment_id": deployment_id,
                "job_id": job_id,
            }
        },
    )
    progress("deploying accepted treatment to the production runtime…")
    execute_deploy_job(job_id)
    db = SessionLocal()
    try:
        deployment = db.get(Deployment, deployment_id)
        job = db.get(Job, job_id)
        agent = db.get(Agent, exp.agent_id)
        if (
            deployment is None
            or job is None
            or agent is None
            or deployment.status != "succeeded"
            or job.status != "succeeded"
        ):
            detail = job.error if job is not None else "deployment ledger row missing"
            raise RuntimeError(f"production deployment failed: {detail}")
        agent_version = agent.version
    finally:
        db.close()

    result = {
        "ab_test_id": ab_test_id,
        "ab_test_status": "STOPPED",
        "agent_id": exp.agent_id,
        "deployment_id": deployment_id,
        "job_id": job_id,
        "agent_version": agent_version,
        "applied_system_prompt": True,
        "applied_tool_descriptions": sorted(accepted_tools),
        "completed_at": _now(),
    }
    if prior.get("after_weights"):
        result["prior_shift"] = dict(prior["after_weights"])
    _update(exp_id, status="promoted", stage="promote", artifact={"promote": result})
    return result


def act_cleanup(exp_id: str, progress: Progress = _noop) -> list[dict[str, str]]:
    """Tear down record-owned resources; keep the shared experiment Gateway."""
    exp = _get(exp_id)
    control = control_client()
    data = data_client()
    artifacts = exp.artifacts
    progress("tearing down A/B tests, online evals, bundles, gateway targets…")
    ab_ids = [a for a in [
        (artifacts.get("abtest") or {}).get("ab_test_id"),
        (artifacts.get("canary") or {}).get("canary_ab_test_id"),
    ] if a]
    # resolve online-eval ids from the live listing by name prefix — the
    # artifact id can be stale after an idempotent adopt
    prefix = f"exp_{exp.id[:8]}_"
    online_evals = [
        c["onlineEvaluationConfigId"]
        for c in control.list_online_evaluation_configs().get(
            "onlineEvaluationConfigs", []
        )
        if str(c.get("onlineEvaluationConfigName", "")).startswith(prefix)
    ]
    bundles = [b for b in [
        ((artifacts.get("bundles") or {}).get("control") or {}).get("bundle_id"),
        ((artifacts.get("bundles") or {}).get("treatment") or {}).get("bundle_id"),
    ] if b]
    gateway = artifacts.get("gateway") or {}
    targets = [t for t in [gateway.get("target_id_v1"),
                           (artifacts.get("canary") or {}).get("target_id_v2")] if t]
    results = ac.cleanup_resources(
        control, data,
        ab_test_ids=ab_ids,
        online_eval_ids=online_evals,
        bundle_ids=bundles,
        gateway_id=gateway.get("gateway_id"),
        target_ids=targets,
        delete_gateway=False,
    )
    _update(exp.id, status="cleaned", stage="cleanup",
            artifact={"cleanup": results})
    return results
