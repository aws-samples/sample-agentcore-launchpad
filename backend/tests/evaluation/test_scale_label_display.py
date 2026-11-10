"""N17-obs428 — every built-in scale label has its zh display text.

The console's zh result screens showed built-in labels in English ("Very Helpful",
"Partially Correct", "Yes"). The display table ``frontend/src/lib/builtin-scale-labels.zh.json``
(read by ``frontend/src/lib/scale-labels.ts``) maps each built-in evaluator's labels to their
per-evaluator meaning; this test enumerates the backend's built-in scale definitions and
fails on any label the table misses, so a new built-in or a new label cannot ship in English.
Display only: stored labels, scores and pass logic never read the table.
"""

from __future__ import annotations

import json
from pathlib import Path

from strands_evals.evaluators.skill_instruction_following_evaluator import SkillFollowingScore
from strands_evals.evaluators.skill_selection_accuracy_evaluator import SkillSelectionScore

from app.services.strands_eval.context_relevance import ContextRelevanceScore
from app.services.strands_eval.evaluators import GLOBAL_LABELS, LEVELS

TABLE_PATH = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib"
              / "builtin-scale-labels.zh.json")
TABLE = {k: v for k, v in json.loads(TABLE_PATH.read_text()).items() if not k.startswith("_")}
# the scales strands-evals / Launchpad define outside GLOBAL_LABELS
OTHER_SCALES = {
    "Builtin.SkillSelectionAccuracy": [s.value for s in SkillSelectionScore],
    "Builtin.SkillInstructionFollowing": [s.value for s in SkillFollowingScore],
    "Builtin.ContextRelevance": [s.value for s in ContextRelevanceScore],
}
SHARED = ("NOT_APPLICABLE",)  # every built-in may answer it (nothing to rate)


def builtin_scales() -> dict[str, list[str]]:
    scales = {evaluator_id: list(labels) for evaluator_id, labels in GLOBAL_LABELS.items()}
    scales.update(OTHER_SCALES)
    return scales


def test_every_builtin_evaluator_has_a_known_scale():
    builtins = {e for e in LEVELS if e.startswith("Builtin.")}
    assert builtins and builtins == set(builtin_scales()), (
        "a built-in without a scale source here: add it to OTHER_SCALES (and the zh table)",
        sorted(builtins ^ set(builtin_scales())))


def test_every_builtin_scale_label_has_zh_display_text():
    missing = [(evaluator_id, label) for evaluator_id, labels in builtin_scales().items()
               for label in labels if not TABLE.get(evaluator_id, {}).get(label)]
    missing += [("*", label) for label in SHARED if not TABLE.get("*", {}).get(label)]
    assert not missing, f"built-in labels without zh display text: {missing}"


def test_the_table_names_only_builtins_and_polarity_pairs_differ():
    assert set(TABLE) - {"*"} <= set(builtin_scales())
    for evaluator_id, labels in builtin_scales().items():
        shown = [TABLE[evaluator_id][label] for label in labels]
        assert len(set(shown)) == len(shown), (evaluator_id, shown)  # never two labels alike
    assert TABLE["Builtin.Refusal"]["Yes"] == "已拒答"  # Yes = the agent refused
    assert TABLE["Builtin.Helpfulness"]["Very Helpful"] == "非常有帮助"
    assert TABLE["Builtin.Correctness"]["Partially Correct"] == "部分正确"
