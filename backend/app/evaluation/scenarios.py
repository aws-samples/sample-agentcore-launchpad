"""Scenario helpers for dataset evaluation (devguide "Dataset schema").

Ported from agentxray backend/app/scenarios.py. ``normalize_scenarios`` turns
dataset items into a uniform scenario list; ``ground_truth_metadata`` builds
the per-session ground-truth metadata payload
from scenario ground truth (assertions → Builtin.GoalSuccessRate,
expected_trajectory → Builtin.Trajectory*Match, turns[].expected_response →
Builtin.Correctness).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# A scenario's input preview on the run row (`EvalRun.scenario_map`) and the compare read.
INPUT_PREVIEW_CHARS = 200
FINGERPRINT_CHARS = 16

# D3-d / R3-c (eval Round 3): every scenario is in exactly one split. Tuning readers (prompt
# optimisation, recommendation evidence, canary edits) see only `tuning`; an acceptance run
# replays `held_out`. A scenario without an explicit `split` gets the deterministic default.
SPLIT_TUNING = "tuning"
SPLIT_HELD_OUT = "held_out"
SPLITS = (SPLIT_TUNING, SPLIT_HELD_OUT)
SPLIT_ALL = "all"
RUN_SPLITS = (SPLIT_ALL, *SPLITS)
HELD_OUT_EVERY = 5  # the default holds out ~1 in 5 scenarios
# a dataset with fewer scenarios than this defaults to ALL tuning, so a tiny dataset never has
# an empty tuning split (an explicit `split` still holds out)
MIN_DEFAULT_HOLD_OUT = 5


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
        scenario: dict[str, Any] = {"scenario_id": f"item_{i + 1}", "turns": [turn]}
        if item.get("split"):
            scenario["split"] = item["split"]
        out.append(scenario)
    return out


def default_split(scenario_id: str) -> str:
    """The deterministic default split of a scenario: ``held_out`` when the first 8 hex of
    sha256(scenario_id) are 0 mod ``HELD_OUT_EVERY``, else ``tuning``. It depends on the id
    only, so adding, removing or reordering OTHER scenarios never moves a scenario between
    splits (a held-out scenario that drifted into tuning would leak) and a copied scenario
    keeps its split."""
    digest = hashlib.sha256(str(scenario_id).encode("utf-8")).hexdigest()
    return SPLIT_HELD_OUT if int(digest[:8], 16) % HELD_OUT_EVERY == 0 else SPLIT_TUNING


def scenario_split(scenario: dict[str, Any], dataset_size: int = MIN_DEFAULT_HOLD_OUT) -> str:
    """A normalized scenario's split: its explicit ``split``; else ``tuning`` in a dataset of
    fewer than ``MIN_DEFAULT_HOLD_OUT`` scenarios; else :func:`default_split`."""
    explicit = scenario.get("split")
    if explicit in SPLITS:
        return str(explicit)
    if dataset_size < MIN_DEFAULT_HOLD_OUT:
        return SPLIT_TUNING
    return default_split(str(scenario.get("scenario_id") or ""))


def item_splits(items: list[dict[str, Any]]) -> list[str]:
    """The split of every dataset item, in item order (legacy items by their ``item_<n>``) —
    the dataset's size decides whether the default holds anything out."""
    scenarios = normalize_scenarios(items)
    return [scenario_split(s, len(scenarios)) for s in scenarios]


def select_split(items: list[dict[str, Any]], split: str) -> list[dict[str, Any]]:
    """The dataset items in ``split`` (``all`` = every item), as stored. A legacy prompt item
    keeps the scenario id of its position in the FULL dataset when it is replayed alone, so
    the subset is returned as normalized scenarios whenever it is filtered."""
    if split == SPLIT_ALL:
        return list(items)
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    # each kept scenario carries its resolved split, so the subset (smaller than the dataset)
    # never re-derives it from its own size
    return [{**scenario, "split": resolved}
            for scenario, resolved in zip(normalize_scenarios(items), item_splits(items),
                                          strict=True)
            if resolved == split]


def split_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    splits = item_splits(items)
    return {name: splits.count(name) for name in SPLITS}


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
        if has_expected_trajectory(scenario):
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


def has_expected_trajectory(scenario: dict[str, Any]) -> bool:
    """N17-obs376: the scenario states a trajectory — an explicit ``[]`` is "no tools
    expected" (scored, as on Global); only an absent / null field is "no expectation"."""
    return isinstance(scenario.get("expected_trajectory"), list)


def available_ground_truth(items: list[dict[str, Any]]) -> set[str]:
    """Judge placeholders a dataset's scenarios can actually fill.

    Named after ``evaluators_local.GROUND_TRUTH_PLACEHOLDERS`` so a judge prompt's
    placeholders can be diffed against a run's scope directly. An empty item
    list (window / session-id scope) supplies nothing.
    """
    available: set[str] = set()
    for scenario in normalize_scenarios(items):
        if any(t.get("expected_response") for t in scenario.get("turns", [])):
            available.add("expected_response")
        if has_expected_trajectory(scenario):
            available.add("expected_tool_trajectory")
        if scenario.get("assertions"):
            available.add("assertions")
    return available
