"""The calibration measures the PRODUCT's pass bar (N17-obs425 follow-up).

``calibration.positive`` used its own bars (Faithfulness ≥ 0.75, Helpfulness ≥ 0.667), so its
κ / false-pass columns did not measure the rule the console applies. The primary call now
uses ``evaluators_local.pass_threshold`` (+ polarity: a Refusal row passes when it did NOT
refuse); the earlier bar stays as a secondary ``lenient`` column; ``rescore`` and
``--calibration-rerender`` re-summarize stored judge rows without calling a judge.
"""

import json
import sys
from pathlib import Path

import pytest

from app.evaluation.evaluators_local import pass_threshold

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "scripts"))
from evalwb import calibration  # noqa: E402


@pytest.mark.parametrize("evaluator_id", ["Builtin.Faithfulness", "Builtin.Helpfulness",
                                          "Builtin.Correctness", "Builtin.Coherence",
                                          "Builtin.Conciseness", "Builtin.GoalSuccessRate"])
def test_the_primary_bar_is_the_products(evaluator_id):
    assert calibration.product_bar(evaluator_id) == pass_threshold(evaluator_id)


def test_the_calls_at_the_product_bar():
    assert calibration.call("Builtin.Faithfulness", 0.75) is False  # Generally Yes
    assert calibration.call("Builtin.Faithfulness", 1.0) is True
    assert calibration.call("Builtin.Helpfulness", 0.667) is False  # Somewhat Helpful
    assert calibration.call("Builtin.Helpfulness", 0.833) is True
    assert calibration.call("Builtin.Coherence", 0.75) is False     # Generally Yes (bar 1.0)
    assert calibration.call("Builtin.Coherence", 1.0) is True
    # Refusal: positive = refused, which the product counts as NOT passed
    assert calibration.call("Builtin.Refusal", 1.0) is True
    assert calibration.call("Builtin.Refusal", 0.0) is False


def _rows(evaluator, good, bad, reps=1):
    out = []
    for rep in range(reps):
        for k, (g, b) in enumerate(zip(good, bad, strict=True)):
            for side, score in (("good", g), ("bad", b)):
                out.append({"pair": f"p{k}", "side": side, "variant": "base", "rep": rep,
                            "evaluator": evaluator, "model": "m", "score": score,
                            "expected": side == "good", "defect": "d", "lang": "en",
                            "twin": None, "bad_max": None, "positive": None})
    return out


def test_rescore_and_the_lenient_column():
    rows = calibration.rescore(_rows("Builtin.Faithfulness", [1.0, 1.0], [0.75, 0.25]))
    [p] = calibration.summarize(rows)["per_evaluator"]
    assert p["bar"] == 1.0 and p["kappa"] == 1.0 and p["false_pass_rate"] == 0.0
    assert p["lenient"] == {"bar": 0.75, "kappa": 0.5, "false_pass_rate": 0.5,
                            "false_fail_rate": 0.0}
    md = calibration.markdown(calibration.summarize(rows), {})
    assert "| 1.0 | 1.0 | ✓ | 0 % | 0 % | 0.75 · 0.5 · 50 % |" in md
    [same] = calibration.summarize(calibration.rescore(_rows("Builtin.Correctness", [1.0],
                                                             [0.0])))["per_evaluator"]
    assert same["lenient"] is None  # no separate lenient bar for Correctness


def test_rerender_reuses_the_stored_rows_without_a_judge(tmp_path, monkeypatch):
    import eval_workbench

    stored = {"meta": {"fingerprint": "fp", "tier": "hard", "pairs": 2, "reps": 1,
                       "models": ["m"], "mini": False, "generated_at": "t", "sha": "s"},
              "summary": {}, "rows": _rows("Builtin.Faithfulness", [1.0, 1.0], [0.75, 0.25])}
    source = tmp_path / "calibration.json"
    source.write_text(json.dumps(stored), encoding="utf-8")

    def no_judge(*_a, **_k):
        raise AssertionError("a re-render must not call a judge")

    monkeypatch.setattr(calibration, "judge", no_judge)
    out = tmp_path / "out"
    code = eval_workbench.main(["--offline", "--calibration-rerender", str(source),
                                "--report-dir", str(out)])
    assert code == 0
    report = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
    assert report["meta"]["fingerprint"] == "fp" and report["meta"]["tier"] == "hard"
    assert report["meta"]["bars"] == {"Builtin.Faithfulness": 1.0}
    [p] = report["summary"]["per_evaluator"]
    assert p["false_pass_rate"] == 0.0 and p["lenient"]["false_pass_rate"] == 0.5


def test_a_record_without_rows_is_refused(tmp_path):
    import eval_workbench

    source = tmp_path / "record.json"
    source.write_text(json.dumps({"meta": {}, "summary": {}}), encoding="utf-8")
    assert eval_workbench.main(["--offline", "--calibration-rerender", str(source),
                                "--report-dir", str(tmp_path / "o")]) == 1


def test_coherence_keeps_its_old_cut_as_the_lenient_column():
    rows = calibration.rescore(_rows("Builtin.Coherence", [1.0, 1.0], [0.75, 0.25]))
    [p] = calibration.summarize(rows)["per_evaluator"]
    assert p["bar"] == 1.0 and p["false_pass_rate"] == 0.0
    assert p["lenient"]["bar"] == 0.7 and p["lenient"]["false_pass_rate"] == 0.5
