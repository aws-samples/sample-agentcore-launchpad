"""EVAL-TRUST 2.1b — the HARD calibration tier and its per-type metrics.

``tests/fixtures/judge_calibration/hard_pairs.json`` (built by ``build_hard_pairs.py``): real
corpus turns whose wrong side hides its defect — one wrong figure among many, a unit or
rounding slip, one unsupported claim, a right number from the wrong row / turn, a slip in
combining two tool results, a partial refusal or hidden over-sharing, a subtly worse long
answer, a contradiction several sentences apart — plus ``control`` items (hard but correct,
no wrong side) that measure false fails. These hermetic tests pin the set and the
arithmetic; the judge numbers come from ``--calibration-tier hard`` live runs.
"""

import importlib.util
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "scripts"))
from evalwb import calibration  # noqa: E402

FIXTURES = BACKEND / "tests" / "fixtures" / "judge_calibration"
HARD = calibration.load(FIXTURES / "hard_pairs.json")
TYPES = {"long_multi_fact", "unit_rounding", "partial_support", "wrong_row_or_stale",
         "combine_tools", "near_miss_refusal", "subtle_quality", "distant_coherence",
         "control"}


def _builder():
    sys.path.insert(0, str(FIXTURES))
    spec = importlib.util.spec_from_file_location("build_hard_pairs",
                                                  FIXTURES / "build_hard_pairs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hard_tier_is_reproducible_from_the_real_corpus():
    built = _builder().build()
    assert json.loads(json.dumps(built, ensure_ascii=False, sort_keys=True)) == HARD


def test_the_hard_tier_covers_every_type_in_both_languages():
    assert len(HARD) >= 40 and {p["lang"] for p in HARD} == {"zh", "en"}
    assert {p["defect"] for p in HARD} == TYPES
    assert all(p["tier"] == "hard" for p in HARD)
    assert sum(1 for p in HARD if p["lang"] == "zh") >= 20
    controls = [p for p in HARD if p["defect"] == "control"]
    assert len(controls) >= 4
    assert all(p["bad"] is None and all(v["bad"] is None for v in p["labels"].values())
               for p in controls)
    multi_turn = [p for p in HARD if len(p["history"]) >= 2]
    assert len(multi_turn) >= 10  # 3–9 turn sessions
    assert any(len({t["name"] for t in p["tools"]}) >= 2 for p in HARD)  # multi-tool


def test_every_label_holds_by_construction():
    module = _builder()
    for pair in HARD:
        context = "\n".join([t["result"] for t in pair["tools"]] + [pair["prompt"]]
                            + [x for h in pair["history"]
                               for x in [h["prompt"], h["answer"]]
                               + [t["result"] for t in h["tools"]]])
        for fact in pair["facts"]:
            assert fact in context and fact in pair["good"]["answer"], (pair["id"], fact)
        for value in pair["wrong"]:
            assert value in pair["bad"]["answer"] and value not in pair["good"]["answer"]
            kind = pair["wrong_kinds"][value]
            assert (value in context) == (kind == "borrowed"), (pair["id"], value, kind)
        if pair["defect"] != "control":
            assert pair["wrong"], pair["id"]  # every wrong side names its defect
    assert module.context_text  # the builder applies the same rule when it writes


def test_a_control_is_judged_on_its_correct_side_only():
    work = calibration.items(HARD, probes=False)
    controls = {p["id"] for p in HARD if p["defect"] == "control"}
    assert {w["side"] for w in work if w["pair"] in controls} == {"good"}
    assert {w["variant"] for w in work} == {"base"}
    assert {w["side"] for w in work if w["pair"] not in controls} == {"good", "bad"}


def _row(pair, side, score, expected, rep=0, evaluator="Builtin.Faithfulness",
         defect="long_multi_fact", model="m"):
    return {"pair": pair, "side": side, "score": score, "expected": expected, "rep": rep,
            "evaluator": evaluator, "defect": defect, "model": model, "variant": "base",
            "positive": score >= 0.75}


def test_by_type_pools_every_rep_and_reports_the_gap():
    rows = [_row("p1", "good", 1.0, True), _row("p1", "bad", 0.25, False),
            _row("p1", "good", 1.0, True, rep=1), _row("p1", "bad", 1.0, False, rep=1),
            _row("c1", "good", 0.5, True, defect="control")]
    result = {r["defect"]: r for r in calibration.by_type(rows)}
    lmf = result["long_multi_fact"]
    assert (lmf["good"], lmf["bad"], lmf["false_pass_rate"], lmf["false_fail_rate"]) == \
        (2, 2, 0.5, 0.0)
    assert lmf["p_bad_lt_good"] == 0.5 and lmf["gap"] == 0.375
    assert result["control"]["false_fail_rate"] == 1.0 and result["control"]["kappa"] is None
    assert "| long_multi_fact | Builtin.Faithfulness | m | 2 / 2 | 50 % | 0 % |" in \
        "\n".join(calibration.by_type_markdown(calibration.by_type(rows)))


def test_the_tier_comparison_lines_up_evaluator_and_model():
    def summary(kappa, rate, gap):
        return {"per_evaluator": [{"evaluator": "Builtin.Faithfulness", "model": "m",
                                   "kappa": kappa, "false_pass_rate": 0.0,
                                   "false_fail_rate": 0.0,
                                   "discrimination": {"rate": rate, "gap": gap}}]}
    lines = calibration.compare_tiers(summary(1.0, 1.0, 0.9), summary(0.6, 0.8, 0.4))
    assert "| Builtin.Faithfulness | m | 1.0 → 0.6 | 0 % → 0 % | 0 % → 0 % | 100 % → 80 % | " \
           "0.9 → 0.4 |" in lines
