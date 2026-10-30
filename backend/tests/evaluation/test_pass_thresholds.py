"""N17-obs425 — the pass bar is the evaluator's own, not one 0.7 for all.

The console counted every normalized score ≥ 0.7 as passed, so SkillInstructionFollowing's
"Mostly Followed" (0.75) passed although the accepted rule is pass = Fully Followed only. The
catalog now carries a per-evaluator ``pass_threshold`` (default 0.7; two-label evaluators and
SkillInstructionFollowing 1.0) and the console's pass / fail, its passed / failed counts and the
row colours use it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import strands_evals.evaluators as library

from app.evaluation import evaluators_local as ev

_CLASSES = {name.removesuffix("Evaluator"): cls for name, cls in vars(library).items()
            if inspect.isclass(cls) and isinstance(getattr(cls, "_score_mapping", None), dict)}


def _mapping(evaluator_id: str) -> dict[str, float]:
    cls = _CLASSES[evaluator_id.removeprefix("Builtin.")]
    return {str(getattr(k, "value", k)): float(v) for k, v in cls._score_mapping.items()}


def test_skill_instruction_following_has_five_labels_and_passes_only_fully_followed():
    scale = _mapping("Builtin.SkillInstructionFollowing")
    assert scale == {"Fully Followed": 1.0, "Mostly Followed": 0.75, "Partially Followed": 0.5,
                     "Minimally Followed": 0.25, "Not Followed": 0.0}
    sif = "Builtin.SkillInstructionFollowing"
    assert [label for label, s in scale.items() if ev.passes(s, sif)] == ["Fully Followed"]


@pytest.mark.parametrize("evaluator_id", sorted(ev.ALL_BUILTIN_EVALUATORS))
def test_a_two_label_builtin_passes_only_on_its_good_label(evaluator_id):
    name = evaluator_id.removeprefix("Builtin.")
    if name not in _CLASSES:
        pytest.skip("not a strands prompt-template evaluator (Launchpad's own scale)")
    scale = _mapping(evaluator_id)
    if len(scale) != 2:
        assert ev.pass_threshold(evaluator_id) <= 1.0
        return
    assert ev.pass_threshold(evaluator_id) == 1.0
    good = [label for label, s in scale.items() if ev.passes(s, evaluator_id)]
    assert len(good) == 1


def test_correctness_passes_only_perfectly_correct():
    scale = _mapping("Builtin.Correctness")
    assert [label for label, s in scale.items() if ev.passes(s, "Builtin.Correctness")] \
        == ["Perfectly Correct"]


def test_faithfulness_passes_only_completely_yes():
    """User decision 2026-10-25 (hard calibration tier c76c90a4a420b75b-hard): Generally Yes
    (0.75) passed answers carrying a hidden wrong fact or an unsupported claim; only the
    outcome moves, the score is the judge's as before."""
    assert ev.PASS_THRESHOLDS["Builtin.Faithfulness"] == 1.0
    scale = _mapping("Builtin.Faithfulness")
    assert [label for label, s in scale.items() if ev.passes(s, "Builtin.Faithfulness")] \
        == ["Completely Yes"]


def test_coherence_passes_only_completely_yes():
    """Lead decision 2026-10-25, the Faithfulness rule (calibration c76c90a4a420b75b; real-turn
    classification coherence-real-turns-20261025): a minor nit (Generally Yes) now fails."""
    assert ev.PASS_THRESHOLDS["Builtin.Coherence"] == 1.0
    scale = _mapping("Builtin.Coherence")
    assert [label for label, s in scale.items() if ev.passes(s, "Builtin.Coherence")] \
        == ["Completely Yes"]
    # ResponseRelevance keeps its own 0.7 bar — see the dedicated pin below
    assert ev.pass_threshold("Builtin.ResponseRelevance") == 0.7


def test_response_relevance_passes_generally_yes():
    """Lead decision 2026-10-25: ResponseRelevance stays at 0.7, so Generally Yes passes and an
    unrelated extra (a closing offer for another service) is a known limit. Real-turn reading
    docs/cn/reports/judge-calibration/responserelevance-real-turns-20261025.md: 54 % of the real
    Generally Yes turns are class B / C. Calibration c76c90a4a420b75b (easy tier): at 0.7 GLM
    κ 1.0, Kimi 3 / 24 false passes, 0 / 33 false fails. Pinned as a value, not as the default,
    so a change of DEFAULT_PASS_THRESHOLD cannot move it silently."""
    assert ev.pass_threshold("Builtin.ResponseRelevance") == 0.7
    scale = _mapping("Builtin.ResponseRelevance")
    assert [label for label, s in scale.items() if ev.passes(s, "Builtin.ResponseRelevance")] \
        == ["Generally Yes", "Completely Yes"]


def test_refusal_is_inverted_before_the_bar():
    assert ev.passes(0.0, "Builtin.Refusal") and not ev.passes(1.0, "Builtin.Refusal")


def test_multi_point_scales_keep_the_default_bar():
    assert ev.pass_threshold("Builtin.Helpfulness") == ev.DEFAULT_PASS_THRESHOLD == 0.7
    assert ev.passes(0.833, "Builtin.Helpfulness") and not ev.passes(0.667, "Builtin.Helpfulness")
    assert ev.passes(0.75, "Builtin.ResponseRelevance")
    assert not ev.passes(0.5, "Builtin.Correctness")
    helpful = _mapping("Builtin.Helpfulness")
    assert [label for label, s in helpful.items() if ev.passes(s, "Builtin.Helpfulness")] \
        == ["Very helpful", "Above and beyond"]
    assert ev.pass_threshold("custom-judge-id") == 0.7
    arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:evaluator/Builtin.GoalSuccessRate"
    assert ev.pass_threshold(arn) == 1.0


@pytest.mark.parametrize("evaluator_id", sorted(ev.TRAJECTORY_EVALUATORS))
def test_trajectory_matches_are_binary(evaluator_id):
    assert ev.pass_threshold(evaluator_id) == 1.0


def test_the_catalog_lists_every_evaluators_bar(client):
    rows = {r["id"]: r for r in client.get("/api/eval/evaluators").json()["evaluators"]}
    assert rows["Builtin.SkillInstructionFollowing"]["pass_threshold"] == 1.0
    assert rows["Builtin.Helpfulness"]["pass_threshold"] == 0.7
    assert rows["Builtin.TrajectoryExactOrderMatch"]["pass_threshold"] == 1.0
    assert all("pass_threshold" in r for r in rows.values())
    assert ev.builtin_out("Builtin.GoalSuccessRate")["pass_threshold"] == 1.0


def test_the_console_mirrors_the_catalog_bars():
    """`frontend/src/lib/evaluators.ts` PASS_THRESHOLDS / DEFAULT_PASS_THRESHOLD = the backend's."""
    ts = (Path(__file__).resolve().parents[3] / "frontend/src/lib/evaluators.ts").read_text()
    body = ts.split("export const PASS_THRESHOLDS", 1)[1].split("};", 1)[0]
    ui = {k: float(v) for k, v in re.findall(r'"([\w.]+)":\s*([\d.]+)', body)}
    assert ui == ev.PASS_THRESHOLDS
    default = re.search(r"export const DEFAULT_PASS_THRESHOLD = ([\d.]+);", ts)
    assert default is not None and float(default.group(1)) == ev.DEFAULT_PASS_THRESHOLD
