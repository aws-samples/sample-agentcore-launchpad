"""Scenario helpers for dataset evaluation (devguide "Dataset schema").

Ported from agentxray backend/app/scenarios.py. ``normalize_scenarios`` turns
dataset items into a uniform scenario list; ``ground_truth_metadata`` builds
the ``StartBatchEvaluation`` ``evaluationMetadata.sessionMetadata`` payload
from scenario ground truth (assertions → Builtin.GoalSuccessRate,
expected_trajectory → Builtin.Trajectory*Match, turns[].expected_response →
Builtin.Correctness).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# A scenario's input preview on the run row (`EvalRun.scenario_map`) and the compare read.
INPUT_PREVIEW_CHARS = 200
FINGERPRINT_CHARS = 16


def scenario_kind(item: dict[str, Any]) -> str:
    """AWS dataset schema: an item with ``turns`` is a *fixed* scenario (even when it
    also carries ``input`` / ``actor_profile``); one without ``turns`` but with
    ``input`` or ``actor_profile`` is *dynamic*; anything else is a legacy
    ``{prompt, expected}`` item. Datasets declare no schema type any more, so this
    per-item rule is the only classification."""
    if "turns" in item:
        return "fixed"
    if "actor_profile" in item:
        return "dynamic"
    # a legacy prompt item that also carries a stray `input` (imported JSON)
    # stays legacy: its `prompt` / `expected` are what it replays
    if "input" in item and "prompt" not in item:
        return "dynamic"
    return "legacy"


def simulates(item: dict[str, Any]) -> bool:
    """A dynamic scenario with an actor: an LLM plays the user. A dynamic scenario
    with ``input`` only runs as a single replayed turn instead."""
    return scenario_kind(item) == "dynamic" and "actor_profile" in item


def dataset_kind(items: list[dict[str, Any]]) -> str:
    """``predefined`` (every scenario replays turns) · ``simulated`` (every scenario
    simulates) · ``mixed`` · ``empty`` — derived from the examples, since AWS
    datasets no longer carry a schema type and may mix both kinds."""
    if not items:
        return "empty"
    personas = sum(1 for item in items if simulates(item))
    if personas == 0:
        return "predefined"
    return "simulated" if personas == len(items) else "mixed"


def normalize_scenarios(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the dataset's items in devguide scenario schema.

    Legacy prompt items become single-turn predefined scenarios (a non-empty
    ``expected`` becomes the turn's ``expected_response``); items already in
    scenario shape — fixed (``turns``) or simulated persona (``actor_profile``) —
    pass through as stored. A dynamic scenario with ``input`` and no actor runs
    as one turn (AWS: "a scenario with input and no actor_profile runs as a
    single turn"), so it is replayed as a one-turn fixed scenario.
    """
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        kind = scenario_kind(item)
        if kind == "fixed" or simulates(item):
            out.append(item)
            continue
        if kind == "dynamic":
            rest = {k: v for k, v in item.items() if k not in ("input", "max_turns")}
            out.append({**rest, "turns": [{"input": item["input"]}]})
            continue
        turn: dict[str, Any] = {"input": item["prompt"]}
        if item.get("expected"):
            turn["expected_response"] = item["expected"]
        out.append({"scenario_id": f"item_{i + 1}", "turns": [turn]})
    return out


def aws_examples(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The dataset's items as AWS Dataset examples: legacy prompt items become
    one-turn fixed scenarios (AWS has no prompt shape); every scenario-shaped item —
    fixed, persona, or an ``input``-only dynamic one — is sent **verbatim**,
    ``metadata`` included. Unlike ``normalize_scenarios`` (the replay view) it
    does not rewrite dynamic items: AWS dedupes generated examples on their
    content and ``metadata.source``, so a rewritten copy would no longer match."""
    normalized = normalize_scenarios(items)
    return [
        item if scenario_kind(item) != "legacy" else normalized[i]
        for i, item in enumerate(items)
    ]


# `metadata` keys AgentCore writes onto the examples it generates (they do not count toward
# the 2 KB metadata limit and are not part of a scenario's content).
AGENTCORE_METADATA_KEYS = frozenset({"generation_id"})


def example_signature(example: dict[str, Any]) -> str:
    """Canonical JSON (sorted keys) of an example's *content*: the service-assigned
    ``exampleId`` and the AgentCore-added ``metadata`` keys are dropped, and a
    ``metadata`` left empty by that equals an absent one — so a local scenario and
    its AWS copy compare equal iff the user-authored content is identical."""
    body = {k: v for k, v in example.items() if k != "exampleId"}
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        kept = {k: v for k, v in metadata.items() if k not in AGENTCORE_METADATA_KEYS}
        if kept:
            body["metadata"] = kept
        else:
            body.pop("metadata")
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class ExampleDiff:
    """What turns a DRAFT's examples into the local scenarios, keyed by ``scenario_id``."""

    added: list[dict[str, Any]] = field(default_factory=list)  # local-only scenarios
    updated: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (exampleId, scenario)
    deleted: list[str] = field(default_factory=list)  # exampleIds of cloud-only examples
    unchanged: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "updated": len(self.updated),
            "deleted": len(self.deleted),
            "unchanged": self.unchanged,
        }


def diff_examples(local: list[dict[str, Any]], cloud: list[dict[str, Any]]) -> ExampleDiff:
    """Diff the local scenarios against a DRAFT's examples (ListDatasetExamples) by
    ``scenario_id``. A cloud example without a ``scenario_id`` (or repeating one already
    seen) cannot be matched and is deleted."""
    diff = ExampleDiff()
    by_id: dict[str, dict[str, Any]] = {}
    for ex in cloud:
        example_id = ex.get("exampleId")
        scenario_id = ex.get("scenario_id")
        if scenario_id and scenario_id not in by_id:
            by_id[scenario_id] = ex
        elif example_id:
            diff.deleted.append(example_id)
    for scenario in local:
        match = by_id.pop(scenario["scenario_id"], None)
        if match is None:
            diff.added.append(scenario)
        elif example_signature(match) == example_signature(scenario):
            diff.unchanged += 1
        else:
            diff.updated.append((match["exampleId"], scenario))
    diff.deleted.extend(ex["exampleId"] for ex in by_id.values() if ex.get("exampleId"))
    return diff


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


def input_preview(scenario: dict[str, Any]) -> str:
    """The scenario's first user input, whitespace collapsed, at most
    ``INPUT_PREVIEW_CHARS`` characters: the first replayed prompt, or a simulated
    persona's opening ``input``."""
    prompts = scenario_prompts(scenario)
    raw: Any = prompts[0] if prompts else scenario.get("input", "")
    if isinstance(raw, dict):
        raw = raw.get("content") or raw.get("prompt") or str(raw)
    text = " ".join(str(raw or "").split())
    if len(text) > INPUT_PREVIEW_CHARS:
        text = text[: INPUT_PREVIEW_CHARS - 1] + "…"
    return text


def items_fingerprint(items: list[dict[str, Any]]) -> tuple[str, int] | None:
    """(fingerprint, scenario count) of the items a replay run replays; None for none.

    The fingerprint is the first 16 hex of sha256 over the *normalized* scenarios as
    canonical JSON (sorted keys), so key order inside an item does not change it, a
    legacy prompt list and its normalized form hash the same, and editing any item
    (or the order of the items) does.
    """
    if not items:
        return None
    scenarios = normalize_scenarios(items)
    blob = json.dumps(scenarios, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_CHARS], len(scenarios)


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
