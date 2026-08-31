"""Scenario helpers for dataset evaluation (devguide "Dataset schema").

Ported from agentxray backend/app/scenarios.py. ``normalize_scenarios`` turns
dataset items into a uniform scenario list; ``ground_truth_metadata`` builds
the ``StartBatchEvaluation`` ``evaluationMetadata.sessionMetadata`` payload
from scenario ground truth (assertions → Builtin.GoalSuccessRate,
expected_trajectory → Builtin.Trajectory*Match, turns[].expected_response →
Builtin.Correctness).
"""

from __future__ import annotations

from typing import Any


def normalize_scenarios(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the dataset's items in devguide scenario schema.

    Legacy prompt items become single-turn predefined scenarios (a non-empty
    ``expected`` becomes the turn's ``expected_response``); items already in
    scenario shape — predefined (``turns``) or simulated persona
    (``actor_profile``) — pass through as stored.
    """
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if "turns" in item or "actor_profile" in item:
            out.append(item)
            continue
        turn: dict[str, Any] = {"input": item["prompt"]}
        if item.get("expected"):
            turn["expected_response"] = item["expected"]
        out.append({"scenario_id": f"item_{i + 1}", "turns": [turn]})
    return out


def scenario_prompts(scenario: dict[str, Any]) -> list[str]:
    """The user prompts of a scenario's turns, in replay order.

    A turn ``input`` is normally a string; dict inputs (imported JSON) fall
    back to their ``content``/``prompt`` field, then to ``str()``.
    """
    prompts: list[str] = []
    for turn in scenario.get("turns", []):
        raw = turn.get("input")
        if isinstance(raw, dict):
            raw = raw.get("content") or raw.get("prompt") or str(raw)
        prompts.append(str(raw))
    return prompts


def ground_truth_metadata(
    scenarios: list[dict[str, Any]], session_ids: list[str]
) -> list[dict[str, Any]]:
    """Build ``sessionMetadata`` entries for scenarios that carry ground truth.

    Shape (verified against the boto3 service model):
    ``{sessionId, testScenarioId, groundTruth: {inline: {assertions:
    [{text}], expectedTrajectory: {toolNames}, turns: [{input: {prompt},
    expectedResponse: {text}}]}}}`` — only non-empty keys are included, and
    scenarios with no ground truth at all are omitted entirely. Sessions pair
    with scenarios by position.
    """
    out: list[dict[str, Any]] = []
    for scenario, session_id in zip(scenarios, session_ids, strict=True):
        inline: dict[str, Any] = {}
        if scenario.get("assertions"):
            inline["assertions"] = [{"text": a} for a in scenario["assertions"]]
        if scenario.get("expected_trajectory"):
            inline["expectedTrajectory"] = {"toolNames": scenario["expected_trajectory"]}
        turns = [
            {
                "input": {"prompt": prompt},
                "expectedResponse": {"text": turn["expected_response"]},
            }
            for turn, prompt in zip(
                scenario.get("turns", []), scenario_prompts(scenario), strict=True
            )
            if turn.get("expected_response")
        ]
        if turns:
            inline["turns"] = turns
        if not inline:
            continue
        out.append(
            {
                "sessionId": session_id,
                "testScenarioId": scenario["scenario_id"],
                "groundTruth": {"inline": inline},
            }
        )
    return out


def available_ground_truth(items: list[dict[str, Any]]) -> set[str]:
    """Judge placeholders a dataset's scenarios can actually fill.

    Named after ``agentcore_eval.GROUND_TRUTH_PLACEHOLDERS`` so a judge prompt's
    placeholders can be diffed against a run's scope directly. An empty item
    list (window / session-id scope) supplies nothing.
    """
    available: set[str] = set()
    for scenario in normalize_scenarios(items):
        if any(t.get("expected_response") for t in scenario.get("turns", [])):
            available.add("expected_response")
        if scenario.get("expected_trajectory"):
            available.add("expected_tool_trajectory")
        if scenario.get("assertions"):
            available.add("assertions")
    return available
