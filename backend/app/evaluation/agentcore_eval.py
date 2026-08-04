"""Thin, injectable wrappers over the bedrock-agentcore control/data clients.

Vendored from agentcore_eva_opt (github.com/xiehust/agentcore_eva_opt)
backend/app/agentcore.py — evaluation, insights, evaluators, datasets,
configuration bundles, A/B tests and recommendations. Wrappers take explicit
clients so tests inject stubs; payloads mirror Lab4_AgentCore_Optimization.
"""


from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

# Default built-in evaluators used across batch + online evaluation.
BUILTIN_EVALUATORS = [
    "Builtin.GoalSuccessRate",
    "Builtin.Helpfulness",
    "Builtin.Correctness",
]

# The 13 general-purpose built-in evaluators, by evaluation level (see
# AgentCore Evaluations docs: built-in-evaluators-overview). IDs are used as
# `Builtin.<Name>`.
ALL_BUILTIN_EVALUATORS: dict[str, str] = {
    # session level
    "Builtin.GoalSuccessRate": "SESSION",
    # trace level — quality
    "Builtin.Helpfulness": "TRACE",
    "Builtin.Correctness": "TRACE",
    "Builtin.Faithfulness": "TRACE",
    "Builtin.ResponseRelevance": "TRACE",
    "Builtin.Conciseness": "TRACE",
    "Builtin.Coherence": "TRACE",
    "Builtin.InstructionFollowing": "TRACE",
    "Builtin.Refusal": "TRACE",
    # trace level — safety
    "Builtin.Harmfulness": "TRACE",
    "Builtin.Stereotyping": "TRACE",
    # tool-call level
    "Builtin.ToolSelectionAccuracy": "TOOL_CALL",
    "Builtin.ToolParameterAccuracy": "TOOL_CALL",
}

# Ground-truth-only trajectory matchers — they score against
# evaluationMetadata.groundTruth.expectedTrajectory, so they are only valid
# on dataset runs whose scenarios define expected_trajectory (levels verified
# against a live ListEvaluators).
TRAJECTORY_EVALUATORS: dict[str, str] = {
    "Builtin.TrajectoryExactOrderMatch": "SESSION",
    "Builtin.TrajectoryInOrderMatch": "SESSION",
    "Builtin.TrajectoryAnyOrderMatch": "SESSION",
}

# Built-in evaluators whose score is a *penalty*: the judge answers "Yes" /
# "Harmful" / "Stereotyping" when the response is BAD, so the lower-mean arm is
# the better arm. Verified against the AWS built-in prompt templates
# (AgentCore docs: prompt-templates-builtin). Every other evaluator scores a
# desirable property, where a higher mean wins.
LOWER_IS_BETTER_EVALUATORS = frozenset(
    {"Builtin.Refusal", "Builtin.Harmfulness", "Builtin.Stereotyping"}
)


def evaluator_polarity(evaluator: str) -> int:
    """+1 when a higher mean is the better arm, -1 when a lower mean is.

    Accepts a bare id (``Builtin.Refusal``) or an evaluator ARN
    (``arn:aws:bedrock-agentcore:::evaluator/Builtin.Refusal``). Custom judges
    are +1: AWS exposes no direction on ``ratingScale``, and the launchpad's own
    default scale is pass=1.0 / fail=0.0.
    """
    return -1 if (evaluator or "").rsplit("/", 1)[-1] in LOWER_IS_BETTER_EVALUATORS else 1


EVAL_TERMINAL = {"COMPLETED", "FAILED", "STOPPED", "COMPLETED_WITH_ERRORS"}
REC_TERMINAL = {"COMPLETED", "FAILED"}

_GSR_EVALUATOR_ARN = "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"


def to_log_group_arn(name_or_arn: str, region: str, account_id: str) -> str:
    """Normalize a CloudWatch log-group *name* to its full ARN.

    StartRecommendation requires ARNs of the form
    ``arn:aws:logs:<region>:<account>:log-group:<name>``. The frontend passes
    the raw log-group name (e.g. ``/aws/bedrock-agentcore/runtimes/...-DEFAULT``);
    values already in ARN form are returned unchanged.
    """
    if name_or_arn.startswith("arn:aws:logs:"):
        return name_or_arn
    return f"arn:aws:logs:{region}:{account_id}:log-group:{name_or_arn}"


def config_bundle_baggage(bundle_arn: str, bundle_version: str) -> str:
    """W3C baggage header the runtime reads to pick a configuration bundle."""
    return (
        f"aws.agentcore.configbundle_arn={bundle_arn},"
        f"aws.agentcore.configbundle_version={bundle_version}"
    )


# ─── Configuration bundles (control plane) ──────────────────────────────────
def create_configuration_bundle(
    client: Any,
    *,
    agent_arn: str,
    bundle_name: str,
    system_prompt: str,
    tool_descriptions: dict[str, str],
    commit_message: str,
    description: str = "",
) -> dict[str, Any]:
    return client.create_configuration_bundle(
        bundleName=bundle_name,
        description=description or bundle_name,
        components={
            agent_arn: {
                "configuration": {
                    "system_prompt": system_prompt,
                    "tools": {
                        name: {"description": description}
                        for name, description in tool_descriptions.items()
                    },
                }
            }
        },
        commitMessage=commit_message,
        clientToken=str(uuid.uuid4()),
    )


def get_configuration_bundle(client: Any, *, bundle_id: str) -> dict[str, Any]:
    return client.get_configuration_bundle(bundleId=bundle_id)


def get_configuration_bundle_version(
    client: Any, *, bundle_id: str, version_id: str
) -> dict[str, Any]:
    return client.get_configuration_bundle_version(
        bundleId=bundle_id, versionId=version_id
    )


def update_configuration_bundle(
    client: Any,
    *,
    agent_arn: str,
    bundle_id: str,
    system_prompt: str,
    tool_descriptions: dict[str, str],
    parent_version_ids: list[str],
    commit_message: str,
) -> dict[str, Any]:
    return client.update_configuration_bundle(
        bundleId=bundle_id,
        components={
            agent_arn: {
                "configuration": {
                    "system_prompt": system_prompt,
                    "tools": {
                        name: {"description": description}
                        for name, description in tool_descriptions.items()
                    },
                }
            }
        },
        parentVersionIds=parent_version_ids,
        commitMessage=commit_message,
        clientToken=str(uuid.uuid4()),
    )


# ─── Runtime invocation (data plane) ────────────────────────────────────────
def invoke_agent_runtime(
    client: Any,
    *,
    agent_arn: str,
    session_id: str,
    prompt: str,
    baggage: str | None = None,
) -> str:
    import json

    kwargs: dict[str, Any] = {
        "agentRuntimeArn": agent_arn,
        "runtimeSessionId": session_id,
        "payload": json.dumps({"prompt": prompt}).encode(),
    }
    if baggage:
        kwargs["baggage"] = baggage
    resp = client.invoke_agent_runtime(**kwargs)
    body = resp["response"].read()
    return body.decode("utf-8") if isinstance(body, bytes) else str(body)


# ─── Batch evaluation (data plane) ──────────────────────────────────────────
def start_batch_evaluation(
    client: Any,
    *,
    name: str,
    service_name: str,
    log_groups: list[str],
    session_ids: list[str] | None = None,
    evaluators: list[str] | None = None,
    time_range: dict[str, Any] | None = None,
    session_metadata: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score sessions with evaluators. Scope with explicit ``session_ids``
    (a run's own traffic) or a ``time_range`` {startTime, endTime} over the
    agent's existing traffic (passive evaluation) — same filterConfig shape
    as insights. ``session_metadata`` carries per-session scenario ground
    truth via ``evaluationMetadata.sessionMetadata``."""
    filter_config: dict[str, Any] = {}
    if session_ids:
        filter_config["sessionIds"] = session_ids
    if time_range:
        filter_config["timeRange"] = time_range
    cw: dict[str, Any] = {
        "serviceNames": [service_name],
        "logGroupNames": log_groups,
    }
    if filter_config:
        cw["filterConfig"] = filter_config
    kwargs: dict[str, Any] = {
        "batchEvaluationName": name,
        "evaluators": [{"evaluatorId": e} for e in (evaluators or BUILTIN_EVALUATORS)],
        "dataSourceConfig": {"cloudWatchLogs": cw},
        "clientToken": str(uuid.uuid4()),
    }
    if session_metadata:
        kwargs["evaluationMetadata"] = {"sessionMetadata": session_metadata}
    return client.start_batch_evaluation(**kwargs)


def get_batch_evaluation(client: Any, *, batch_id: str) -> dict[str, Any]:
    return client.get_batch_evaluation(batchEvaluationId=batch_id)


def parse_eval_scores(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull per-evaluator average scores from a get_batch_evaluation result."""
    out: list[dict[str, Any]] = []
    er = result.get("evaluationResults", {})
    for s in er.get("evaluatorSummaries", []):
        avg = s.get("statistics", {}).get("averageScore")
        if avg is not None:
            out.append({"evaluatorId": s.get("evaluatorId"), "score": avg})
    return out


def poll_batch_evaluation(
    client: Any,
    *,
    batch_id: str,
    progress: Any = None,
    interval: float = 20.0,
    max_polls: int = 30,
) -> dict[str, Any]:
    for _ in range(max_polls):
        result = client.get_batch_evaluation(batchEvaluationId=batch_id)
        status = result.get("status")
        if progress:
            progress(f"batch eval status: {status}")
        if status in EVAL_TERMINAL:
            return result
        time.sleep(interval)
    return client.get_batch_evaluation(batchEvaluationId=batch_id)


# ─── Insights (failure analysis / user intent / execution summary) ──────────
# Insights reuse the batch-evaluation API: StartBatchEvaluation with an
# `insights` list INSTEAD of `evaluators` (the two are mutually exclusive).
INSIGHT_TYPES = [
    "Builtin.Insight.FailureAnalysis",
    "Builtin.Insight.UserIntent",
    "Builtin.Insight.ExecutionSummary",
]


def start_insights_evaluation(
    client: Any,
    *,
    name: str,
    service_name: str,
    log_groups: list[str],
    insights: list[str] | None = None,
    session_ids: list[str] | None = None,
    time_range: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start an insights analysis over agent sessions.

    Scope with either explicit ``session_ids`` (e.g. a past run's sessions) or
    a ``time_range`` {startTime, endTime}. Max 500 sessions per analysis.
    """
    filter_config: dict[str, Any] = {}
    if session_ids:
        filter_config["sessionIds"] = session_ids
    if time_range:
        filter_config["timeRange"] = time_range
    cw: dict[str, Any] = {
        "serviceNames": [service_name],
        "logGroupNames": log_groups,
    }
    if filter_config:
        cw["filterConfig"] = filter_config
    return client.start_batch_evaluation(
        batchEvaluationName=name,
        insights=[{"insightId": i} for i in (insights or INSIGHT_TYPES)],
        dataSourceConfig={"cloudWatchLogs": cw},
        clientToken=str(uuid.uuid4()),
    )


def parse_insights(result: dict[str, Any]) -> dict[str, Any]:
    """Pull the three insight result trees from a get_batch_evaluation result.

    Only keys present in the response are returned:
      * failures            — categories → subCategories → rootCauses (each
                              root cause carries a `recommendation` + sessions)
      * userIntents         — flat clusters with per-session userMessages
      * executionSummaries  — flat clusters with approachTaken/finalOutcome
    """
    out: dict[str, Any] = {}
    if "failureAnalysisResult" in result:
        out["failures"] = result["failureAnalysisResult"].get("failures", [])
    if "userIntentResult" in result:
        out["userIntents"] = result["userIntentResult"].get("userIntents", [])
    if "executionSummaryResult" in result:
        out["executionSummaries"] = result["executionSummaryResult"].get(
            "executionSummaries", []
        )
    return out


# ─── Custom evaluators (control plane) ──────────────────────────────────────
# Judge placeholders fed from ``evaluationReferenceInputs`` rather than from the
# trace itself. Per the AgentCore docs (create-evaluator): "Custom evaluators
# that use ground truth placeholders cannot be used in online evaluation
# configurations" — live traffic carries no ground truth. ``{context}``,
# ``{assistant_turn}``, ``{available_tools}``, ``{tool_turn}`` and
# ``{actual_tool_trajectory}`` are all trace-derived and stay valid online.
GROUND_TRUTH_PLACEHOLDERS = (
    "expected_response",
    "expected_tool_trajectory",
    "assertions",
)


def ground_truth_placeholders(instructions: str) -> list[str]:
    """Ground-truth placeholders a custom judge's instructions reference."""
    text = instructions or ""
    return [p for p in GROUND_TRUTH_PLACEHOLDERS if f"{{{p}}}" in text]


def judge_instructions(detail: dict[str, Any]) -> str:
    """The LLM-judge prompt on a GetEvaluator/ListEvaluators record.

    Empty for a code-based (Lambda) evaluator, which carries no instructions.
    """
    judge = (detail.get("evaluatorConfig") or {}).get("llmAsAJudge") or {}
    return judge.get("instructions") or ""


def create_llm_judge_evaluator(
    client: Any,
    *,
    name: str,
    instructions: str,
    rating_scale: list[dict[str, Any]],
    model_id: str,
    level: str = "TRACE",
    description: str = "",
) -> dict[str, Any]:
    """Create an LLM-as-a-judge custom evaluator.

    ``instructions`` must contain at least one placeholder for the level —
    e.g. ``{context}`` / ``{assistant_turn}`` for TRACE. ``rating_scale`` is a
    numerical scale: [{"value": 1.0, "label": ..., "definition": ...}, ...].
    """
    return client.create_evaluator(
        evaluatorName=name,
        description=description or name,
        level=level,
        evaluatorConfig={
            "llmAsAJudge": {
                "instructions": instructions,
                "ratingScale": {"numerical": rating_scale},
                "modelConfig": {
                    "bedrockEvaluatorModelConfig": {"modelId": model_id}
                },
            }
        },
        clientToken=str(uuid.uuid4()),
    )


def get_evaluator(client: Any, *, evaluator_id: str) -> dict[str, Any]:
    return client.get_evaluator(evaluatorId=evaluator_id)


def update_evaluator(
    client: Any,
    *,
    evaluator_id: str,
    instructions: str,
    rating_scale: list[dict[str, Any]],
    model_id: str,
    level: str,
    description: str,
) -> dict[str, Any]:
    """Full-replace update of an LLM-as-a-judge evaluator config.

    UpdateEvaluator takes the complete llmAsAJudge config (same shape as
    create) — partial patches are not supported, so callers must send every
    field back."""
    return client.update_evaluator(
        evaluatorId=evaluator_id,
        description=description,
        level=level,
        evaluatorConfig={
            "llmAsAJudge": {
                "instructions": instructions,
                "ratingScale": {"numerical": rating_scale},
                "modelConfig": {
                    "bedrockEvaluatorModelConfig": {"modelId": model_id}
                },
            }
        },
        clientToken=str(uuid.uuid4()),
    )


def list_evaluators(client: Any) -> list[dict[str, Any]]:
    """All evaluators in the account/region (built-ins first, then custom)."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_evaluators(**kwargs)
        out.extend(resp.get("evaluators", []))
        token = resp.get("nextToken")
        if not token:
            return out


def delete_evaluator(client: Any, *, evaluator_id: str) -> dict[str, Any]:
    return client.delete_evaluator(evaluatorId=evaluator_id)


# ─── Recommendations (data plane) ───────────────────────────────────────────
def _agent_traces(log_group_arns: list[str], service_names: list[str]) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "cloudwatchLogs": {
            "logGroupArns": log_group_arns,
            "serviceNames": service_names,
            "startTime": now - timedelta(days=7),
            "endTime": now,
        }
    }


def start_system_prompt_recommendation(
    client: Any,
    *,
    name: str,
    system_prompt: str,
    log_group_arns: list[str],
    service_names: list[str],
) -> dict[str, Any]:
    return client.start_recommendation(
        name=name,
        type="SYSTEM_PROMPT_RECOMMENDATION",
        recommendationConfig={
            "systemPromptRecommendationConfig": {
                "systemPrompt": {"text": system_prompt},
                "agentTraces": _agent_traces(log_group_arns, service_names),
                "evaluationConfig": {
                    "evaluators": [{"evaluatorArn": _GSR_EVALUATOR_ARN}]
                },
            }
        },
        clientToken=str(uuid.uuid4()),
    )


def start_tool_description_recommendation(
    client: Any,
    *,
    name: str,
    tools: list[dict[str, str]],
    log_group_arns: list[str],
    service_names: list[str],
) -> dict[str, Any]:
    tools_payload = [
        {"toolName": t["toolName"], "toolDescription": {"text": t["description"]}}
        for t in tools
    ]
    return client.start_recommendation(
        name=name,
        type="TOOL_DESCRIPTION_RECOMMENDATION",
        recommendationConfig={
            "toolDescriptionRecommendationConfig": {
                "toolDescription": {"toolDescriptionText": {"tools": tools_payload}},
                "agentTraces": _agent_traces(log_group_arns, service_names),
            }
        },
        clientToken=str(uuid.uuid4()),
    )


def get_recommendation(client: Any, *, recommendation_id: str) -> dict[str, Any]:
    return client.get_recommendation(recommendationId=recommendation_id)


def poll_recommendation(
    client: Any,
    *,
    recommendation_id: str,
    progress: Any = None,
    interval: float = 20.0,
    max_polls: int = 30,
) -> dict[str, Any]:
    for _ in range(max_polls):
        result = client.get_recommendation(recommendationId=recommendation_id)
        status = result.get("status")
        if progress:
            progress(f"recommendation status: {status}")
        if status in REC_TERMINAL:
            return result
        time.sleep(interval)
    return client.get_recommendation(recommendationId=recommendation_id)


# ─── A/B test variant builders (pure) ───────────────────────────────────────
def config_bundle_variants(
    control_arn: str,
    control_version: str,
    treatment_arn: str,
    treatment_version: str,
    control_weight: int = 50,
    treatment_weight: int = 50,
) -> list[dict[str, Any]]:
    return [
        {
            "name": "C",
            "weight": control_weight,
            "variantConfiguration": {
                "configurationBundle": {
                    "bundleArn": control_arn,
                    "bundleVersion": control_version,
                }
            },
        },
        {
            "name": "T1",
            "weight": treatment_weight,
            "variantConfiguration": {
                "configurationBundle": {
                    "bundleArn": treatment_arn,
                    "bundleVersion": treatment_version,
                }
            },
        },
    ]


def target_variants(
    target_v1: str,
    target_v2: str,
    control_weight: int = 90,
    treatment_weight: int = 10,
) -> list[dict[str, Any]]:
    return [
        {
            "name": "C",
            "weight": control_weight,
            "variantConfiguration": {"target": {"name": target_v1}},
        },
        {
            "name": "T1",
            "weight": treatment_weight,
            "variantConfiguration": {"target": {"name": target_v2}},
        },
    ]


def create_ab_test(client: Any, **kwargs: Any) -> dict[str, Any]:
    """Pass-through to create_ab_test; callers assemble the full kwargs."""
    kwargs.setdefault("clientToken", str(uuid.uuid4()))
    return client.create_ab_test(**kwargs)


def get_ab_test(client: Any, *, ab_test_id: str) -> dict[str, Any]:
    return client.get_ab_test(abTestId=ab_test_id)


def update_ab_test_weights(
    client: Any, *, ab_test_id: str, variants: list[dict[str, Any]]
) -> dict[str, Any]:
    return client.update_ab_test(abTestId=ab_test_id, variants=variants)


def normalize_ab_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a get_ab_test result into the frontend ABComparisonChart shape:
    [{evaluatorId, label, polarity, control:{name,mean,sampleSize}, variants:[{...}]}].

    ``polarity`` travels with the metric so every consumer (verdict maths, the
    console, a stored artifact read back later) reads the same direction instead
    of re-deriving it from the id.
    """
    metrics_out: list[dict[str, Any]] = []
    results = result.get("results", {}) or {}
    for m in results.get("evaluatorMetrics", []):
        arn = m.get("evaluatorArn", "")
        label = arn.split("/")[-1] if arn else m.get("evaluatorId", "evaluator")
        cs = m.get("controlStats", {}) or {}
        variants = []
        for vr in m.get("variantResults", []):
            variants.append(
                {
                    "name": vr.get("name", "T1"),
                    "mean": vr.get("mean"),
                    "sampleSize": vr.get("sampleSize"),
                    "pValue": vr.get("pValue"),
                    "percentChange": vr.get("percentChange"),
                    "isSignificant": vr.get("isSignificant"),
                }
            )
        metrics_out.append(
            {
                "evaluatorId": arn or label,
                "label": label,
                "polarity": evaluator_polarity(arn or label),
                "control": {
                    "name": cs.get("name", "C"),
                    "mean": cs.get("mean"),
                    "sampleSize": cs.get("sampleSize"),
                },
                "variants": variants,
            }
        )
    return metrics_out


# ─── Cleanup fan-out ─────────────────────────────────────────────────────────
_sleep = time.sleep  # injectable for tests
# Deleting a resource can fail transiently while a dependency (the async A/B-test
# delete, an online-eval still referenced by it, etc.) is still propagating —
# retry those categories with backoff instead of leaking them.
_NOT_FOUND = {"ResourceNotFoundException", "NotFoundException"}


def cleanup_resources(
    control_client: Any,
    data_client: Any,
    *,
    ab_test_ids: list[str] | None = None,
    online_eval_ids: list[str] | None = None,
    evaluator_ids: list[str] | None = None,
    bundle_ids: list[str] | None = None,
    gateway_id: str | None = None,
    target_ids: list[str] | None = None,
    delete_gateway: bool = True,
    runtime_ids: list[str] | None = None,
    role_name: str | None = None,
    delivery_id: str | None = None,
    logs_client: Any = None,
    iam_client: Any = None,
    gateway_wait_interval: float = 6.0,
    gateway_wait_timeout: float = 300.0,
) -> list[dict[str, str]]:
    """Delete each resource category independently; never abort on one failure."""
    out: list[dict[str, str]] = []

    def _do(category: str, fn: Any, *, attempts: int = 1, delay: float = 6.0) -> None:
        last: Exception | None = None
        for i in range(attempts):
            try:
                fn()
                out.append({"category": category, "status": "deleted", "detail": ""})
                return
            except Exception as exc:  # noqa: BLE001 — per-category tolerance
                if type(exc).__name__ in _NOT_FOUND:  # already gone == success
                    out.append(
                        {"category": category, "status": "deleted", "detail": "already gone"}
                    )
                    return
                last = exc
                if i < attempts - 1:
                    _sleep(delay)
        out.append(
            {
                "category": category,
                "status": "skipped",
                "detail": f"{type(last).__name__}: {last}",
            }
        )

    for ab in ab_test_ids or []:
        def _del_ab(ab: str = ab) -> None:
            try:
                data_client.update_ab_test(abTestId=ab, executionStatus="STOPPED")
            except Exception:  # noqa: BLE001
                pass
            data_client.delete_ab_test(abTestId=ab)

        # delete_ab_test requires STOPPED (async) — retry while the stop lands.
        _do(f"abtest:{ab}", _del_ab, attempts=10, delay=6)

    for oe in online_eval_ids or []:
        def _del_oe(oe: str = oe) -> None:
            try:
                control_client.update_online_evaluation_config(
                    onlineEvaluationConfigId=oe, executionStatus="DISABLED"
                )
            except Exception:  # noqa: BLE001
                pass
            control_client.delete_online_evaluation_config(onlineEvaluationConfigId=oe)

        # online-eval delete is rejected while the (async-deleting) A/B test
        # still references it — retry with backoff.
        _do(f"online-eval:{oe}", _del_oe, attempts=10, delay=6)

    for ev in evaluator_ids or []:
        _do(f"evaluator:{ev}", lambda ev=ev: control_client.delete_evaluator(evaluatorId=ev))

    for b in bundle_ids or []:
        _do(f"bundle:{b}", lambda b=b: control_client.delete_configuration_bundle(bundleId=b))

    if delivery_id and logs_client is not None:
        _do("gateway-tracing", lambda: logs_client.delete_delivery(id=delivery_id))

    for t in target_ids or []:
        _do(
            f"gateway-target:{t}",
            lambda t=t: control_client.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=t
            ),
        )

    if gateway_id and delete_gateway:
        def _del_gateway() -> None:
            # DeleteGateway is rejected until the gateway's async-deleting A/B
            # test + targets have fully propagated away — verified live to take
            # a few MINUTES *after* list_ab_tests / list_gateway_targets already
            # report them gone, so draining the lists is not a reliable signal.
            # The only reliable signal is a successful delete: retry until it
            # takes or the budget is exhausted (→ _do marks it skipped).
            deadline = time.monotonic() + gateway_wait_timeout
            while True:
                try:
                    control_client.delete_gateway(gatewayIdentifier=gateway_id)
                    return
                except Exception as exc:  # noqa: BLE001
                    if type(exc).__name__ in _NOT_FOUND:
                        return  # already gone == success
                    if time.monotonic() >= deadline:
                        raise
                    _sleep(gateway_wait_interval)

        _do("gateway", _del_gateway)  # _del_gateway owns the retry loop

    for r in runtime_ids or []:
        _do(f"runtime:{r}", lambda r=r: control_client.delete_agent_runtime(agentRuntimeId=r))

    if role_name and iam_client is not None:
        def _del_role() -> None:
            # A role with policies attached cannot be deleted directly.
            for p in iam_client.list_role_policies(RoleName=role_name).get(
                "PolicyNames", []
            ):
                iam_client.delete_role_policy(RoleName=role_name, PolicyName=p)
            for ap in iam_client.list_attached_role_policies(RoleName=role_name).get(
                "AttachedPolicies", []
            ):
                iam_client.detach_role_policy(
                    RoleName=role_name, PolicyArn=ap["PolicyArn"]
                )
            iam_client.delete_role(RoleName=role_name)

        _do("iam-role", _del_role)

    return out


# ─── Config diff (pure) ─────────────────────────────────────────────────────
def diff_configs(
    a_prompt: str,
    a_tools: dict[str, str],
    b_prompt: str,
    b_tools: dict[str, str],
) -> dict[str, Any]:
    """Structured per-key diff between two bundle configurations."""
    tool_diffs = []
    for name in sorted(set(a_tools) | set(b_tools)):
        before = a_tools.get(name)
        after = b_tools.get(name)
        if before != after:
            tool_diffs.append({"tool": name, "before": before, "after": after})
    return {
        "systemPromptChanged": a_prompt.strip() != b_prompt.strip(),
        "systemPromptBefore": a_prompt,
        "systemPromptAfter": b_prompt,
        "toolDiffs": tool_diffs,
        "changedKeyCount": (1 if a_prompt.strip() != b_prompt.strip() else 0)
        + len(tool_diffs),
    }


# ─── Evaluation datasets (control plane, public preview) ────────────────────
# CreateDataset/GetDataset/ListDatasets/DeleteDataset — the devguide "Manage
# datasets" lifecycle. Dataset names share the batch-eval name constraint:
# ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ (no hyphens).
DATASET_SCHEMA_TYPES = {
    "legacy": "AGENTCORE_EVALUATION_PREDEFINED_V1",
    "predefined": "AGENTCORE_EVALUATION_PREDEFINED_V1",
    "simulated": "AGENTCORE_EVALUATION_SIMULATED_V1",
}

DATASET_TERMINAL = {"ACTIVE", "CREATE_FAILED"}


def sanitize_dataset_name(name: str) -> str:
    """Coerce any display name into ^[a-zA-Z][a-zA-Z0-9_]{0,47}$."""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in name)
    cleaned = cleaned.strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"ds_{cleaned}" if cleaned else f"ds_{uuid.uuid4().hex[:8]}"
    return cleaned[:48]


def create_dataset(
    client: Any,
    *,
    name: str,
    schema_type: str,
    examples: list[dict[str, Any]],
    description: str = "",
) -> dict[str, Any]:
    """CreateDataset with inline examples (async — poll to ACTIVE after)."""
    kwargs: dict[str, Any] = {
        "datasetName": name,
        "schemaType": schema_type,
        "source": {"inlineExamples": {"examples": examples}},
        "clientToken": str(uuid.uuid4()),
    }
    if description:
        kwargs["description"] = description
    return client.create_dataset(**kwargs)


def get_dataset(client: Any, *, dataset_id: str) -> dict[str, Any]:
    return client.get_dataset(datasetId=dataset_id)


def list_datasets(client: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        resp = client.list_datasets(**kwargs)
        out.extend(resp.get("datasets", []))
        token = resp.get("nextToken")
        if not token:
            return out


def list_dataset_examples(client: Any, *, dataset_id: str) -> list[dict[str, Any]]:
    """All examples of a cloud dataset, in the schema they were created with
    (each carries a service-assigned ``exampleId`` on top)."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"datasetId": dataset_id}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_dataset_examples(**kwargs)
        out.extend(resp.get("examples", []))
        token = resp.get("nextToken")
        if not token:
            return out


def delete_dataset(client: Any, *, dataset_id: str) -> dict[str, Any]:
    return client.delete_dataset(datasetId=dataset_id)


def poll_dataset_active(
    client: Any,
    *,
    dataset_id: str,
    progress: Any = None,
    sleeper: Any = time.sleep,
    interval: float = 2.0,
    max_polls: int = 60,
) -> dict[str, Any]:
    """Poll GetDataset until ACTIVE. Raises on CREATE_FAILED / timeout."""
    for _ in range(max_polls):
        result = client.get_dataset(datasetId=dataset_id)
        status = result.get("status")
        if progress:
            progress(f"dataset status: {status}")
        if status == "ACTIVE":
            return result
        if status == "CREATE_FAILED":
            reason = result.get("failureReason") or "unknown failure"
            raise RuntimeError(f"dataset creation failed: {reason}")
        sleeper(interval)
    raise TimeoutError(f"dataset {dataset_id} not ACTIVE after {max_polls} polls")
