"""Reference coverage of the ACTUAL evaluation targets a dataset produces (SE-047 B).

One pure helper shared by plan validation, materialization of an *existing* evaluator
reference and the run preflight, so the same question is answered the same way
everywhere: *for this evaluator's level and the ground-truth fields it reads, does every
target it will be applied to carry that reference?*

Targets follow the runner's own grouping (``execution.ground_truth_for_sessions``): an
ordinary scenario is one session whose turns are its traces; an opt-in procedure
scenario expands to one session per (repeat, actor alias, session alias), each session
carrying the ``expectedResponse`` of the turns it replays, and the scenario-level
``assertions`` / ``expectedTrajectory`` attach ONLY to the outcome session (the one that
ran the last step) — seed sessions have none of them.

Evaluator needs come from real configuration, never from a plan kind: judge placeholders
(``{expected_response}`` / ``{assertions}`` / ``{expected_tool_trajectory}``), the base
of a derived evaluator, the canonical trajectory builtins, or the declarative rules of a
managed code evaluator. An external code evaluator's needs are unknown and reported so.
"""

from __future__ import annotations

from typing import Any

from app.evaluation import execution
from app.evaluation.agentcore_eval import (
    ALL_BUILTIN_EVALUATORS,
    TRAJECTORY_EVALUATORS,
    ground_truth_placeholders,
)
from app.evaluation.scenarios import normalize_scenarios

FIELDS = ("expected_response", "assertions", "expected_tool_trajectory")
KNOWN_BUILTINS: dict[str, str] = {**ALL_BUILTIN_EVALUATORS, **TRAJECTORY_EVALUATORS}
CONFIG_KINDS = ("llmAsAJudge", "derived", "codeBased")
LEVELS = ("TRACE", "TOOL_CALL", "SESSION")


def reference_targets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every session and trace target of the dataset with the reference fields it
    carries: ``{"id", "kind": "session"|"trace", "scenario_id", "fields": set}``."""
    out: list[dict[str, Any]] = []
    for scenario in normalize_scenarios(items):
        sid = str(scenario.get("scenario_id") or "")
        turns = scenario.get("turns") or []
        session_fields = set()
        if scenario.get("assertions"):
            session_fields.add("assertions")
        if scenario.get("expected_trajectory"):
            session_fields.add("expected_tool_trajectory")
        if execution.is_executable(scenario):
            plan = execution.parse_plan(scenario)
            last = plan.steps[-1]
            keys = plan.session_keys
            keys = keys() if callable(keys) else keys
            for repeat in range(1, plan.repeat + 1):
                for actor, session in keys:
                    steps = [s for s in plan.steps if (s.actor, s.session) == (actor, session)]
                    label = f"{sid}#r{repeat}/{session}"
                    responses = [bool((turns[s.turn] or {}).get("expected_response"))
                                 for s in steps]
                    fields = set(session_fields) if (actor, session) == (
                        last.actor, last.session) else set()
                    if responses and all(responses):
                        fields.add("expected_response")
                    out.append({"id": label, "kind": "session", "scenario_id": sid,
                                "fields": fields})
                    for s in steps:
                        t_fields = set(fields) - {"expected_response"}
                        if responses[steps.index(s)]:
                            t_fields.add("expected_response")
                        out.append({"id": f"{label}/turn {s.turn + 1}", "kind": "trace",
                                    "scenario_id": sid, "fields": t_fields})
            continue
        responses = [bool((t or {}).get("expected_response")) for t in turns]
        fields = set(session_fields)
        if responses and all(responses):
            fields.add("expected_response")
        out.append({"id": sid, "kind": "session", "scenario_id": sid, "fields": fields})
        for i, has in enumerate(responses):
            t_fields = set(session_fields) | ({"expected_response"} if has else set())
            out.append({"id": f"{sid}/turn {i + 1}", "kind": "trace", "scenario_id": sid,
                        "fields": t_fields})
    return out


def coverage_gaps(items: list[dict[str, Any]], needs: set[str], level: str) -> list[str]:
    """Target ids that lack a reference the evaluator reads. SESSION-level evaluators are
    applied to every session; TRACE/TOOL_CALL evaluators to every trace (a trace also
    sees its session's scoped references). Empty ``needs`` → no gaps."""
    if not needs:
        return []
    kind = "session" if level == "SESSION" else "trace"
    gaps: list[str] = []
    for target in reference_targets(items):
        if target["kind"] != kind:
            continue
        missing = sorted(set(needs) - target["fields"])
        if missing:
            gaps.append(f"{target['id']} lacks {'/'.join(missing)}")
    return gaps


def validate_evaluator_config(config: Any) -> str | None:
    """Exactly ONE complete supported definition (installed control-plane model:
    ``llmAsAJudge{instructions, ratingScale, modelConfig}``, ``derived{baseEvaluatorId,
    modelConfig}``, ``codeBased{lambdaConfig{lambdaArn}}``). Returns the problem, or None."""
    if not isinstance(config, dict):
        return "evaluatorConfig is not an object"
    kinds = [k for k in config if k in CONFIG_KINDS]
    unknown = sorted(set(config) - set(CONFIG_KINDS))
    if unknown:
        return f"evaluatorConfig carries unknown members {unknown}"
    if len(kinds) != 1:
        return "evaluatorConfig must carry exactly one of llmAsAJudge / derived / codeBased"
    kind, body = kinds[0], config[kinds[0]]
    if not isinstance(body, dict) or not body:
        return f"{kind} definition is empty"

    def model_ok(mc: Any) -> bool:
        if not isinstance(mc, dict) or not mc:
            return False
        bedrock = mc.get("bedrockEvaluatorModelConfig")
        responses = mc.get("responsesEvaluatorModelConfig")
        if bedrock is not None:
            return isinstance(bedrock, dict) and isinstance(bedrock.get("modelId"), str) \
                and bool(bedrock["modelId"].strip())
        return isinstance(responses, dict) and bool(responses)

    if kind == "llmAsAJudge":
        if not isinstance(body.get("instructions"), str) or not body["instructions"].strip():
            return "llmAsAJudge.instructions missing"
        scale = body.get("ratingScale")
        if not isinstance(scale, dict) or not any(
                isinstance(scale.get(k), list) and scale[k] for k in ("numerical", "categorical")):
            return "llmAsAJudge.ratingScale missing"
        if not model_ok(body.get("modelConfig")):
            return "llmAsAJudge.modelConfig missing"
        return None
    if kind == "derived":
        base = body.get("baseEvaluatorId")
        if not isinstance(base, str) or not base.strip():
            return "derived.baseEvaluatorId missing"
        if not model_ok(body.get("modelConfig")):
            return "derived.modelConfig missing"
        return None
    lam = body.get("lambdaConfig")
    if not isinstance(lam, dict) or not isinstance(lam.get("lambdaArn"), str) \
            or not lam["lambdaArn"].startswith("arn:aws:lambda:"):
        return "codeBased.lambdaConfig.lambdaArn missing"
    return None


def builtin_needs(evaluator_id: str) -> set[str]:
    return {"expected_tool_trajectory"} if evaluator_id in TRAJECTORY_EVALUATORS else set()


def needs_from_config(detail: dict[str, Any], managed_rules: list[dict[str, Any]] | None
                      ) -> tuple[set[str] | None, str]:
    """(needs, kind) from a GetEvaluator record. ``None`` needs = unknown (an external code
    evaluator whose rules this platform does not own)."""
    config = detail.get("evaluatorConfig") or {}
    if "llmAsAJudge" in config:
        judge = config.get("llmAsAJudge") or {}
        return set(ground_truth_placeholders(str(judge.get("instructions") or ""))), "judge"
    if "derived" in config:
        derived = config.get("derived") or {}
        return set(builtin_needs(str(derived.get("baseEvaluatorId") or ""))), "derived"
    if managed_rules is None:
        return None, "code"
    needs: set[str] = set()
    for c in managed_rules:
        if c.get("type") == "reference_trajectory":
            needs.add("expected_tool_trajectory")
        if c.get("type") == "reference_response":
            needs.add("expected_response")
    return needs, "code"
