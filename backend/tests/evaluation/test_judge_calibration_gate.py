"""Round 3 R3-e — the judge gate and the drift watch.

Any change to a builtin judge's prompt (``global_templates``, a strands-evals bump) or to the
judge model ids changes ``judge_fingerprint.fingerprint()``; ``make verify`` then fails here
until the change carries a calibration report recorded at the new fingerprint
(``make judge-calibration`` → ``docs/cn/reports/judge-calibration/<fingerprint>.json``).
``make judge-drift`` is the daily mini-run against that record (``calibration.mini`` /
``calibration.drift``).
"""

import json
import sys
from pathlib import Path

import pytest

from app.evaluation import judge_fingerprint
from app.services.strands_eval import global_templates

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "scripts"))
from evalwb import calibration  # noqa: E402

RECORDS = BACKEND.parent / "docs" / "cn" / "reports" / "judge-calibration"
PAIRS = calibration.load(BACKEND / "tests" / "fixtures" / "judge_calibration" / "pairs.json")
HARD_PAIRS = calibration.load(
    BACKEND / "tests" / "fixtures" / "judge_calibration" / "hard_pairs.json")


def test_a_judge_change_carries_a_calibration_report_at_its_fingerprint():
    fp = judge_fingerprint.fingerprint()
    path = RECORDS / f"{fp}.json"
    assert path.is_file(), (
        f"the judge fingerprint is {fp} and there is no calibration report for it: a judge "
        f"prompt or judge model id changed. Run `make judge-calibration` (both CN judges, the "
        f"full set, 3 reps) and commit docs/cn/reports/judge-calibration/{fp}.{{json,md}}")
    record = json.loads(path.read_text(encoding="utf-8"))
    meta = record["meta"]
    assert record["fingerprint"] == meta["fingerprint"] == fp
    assert not meta["mini"] and meta["pairs"] == len(PAIRS) and not meta.get("evaluators")
    assert meta["reps"] >= judge_fingerprint.MIN_REPORT_REPS
    assert set(judge_fingerprint.CALIBRATED_JUDGES) <= set(meta["models"])
    assert (RECORDS / f"{fp}.md").is_file()


def test_a_judge_change_carries_a_hard_tier_report_too():
    """EVAL-TRUST 2.1b: the hard tier is recorded next to the easy one, at the same
    fingerprint (``make judge-calibration`` runs both)."""
    fp = judge_fingerprint.fingerprint()
    path = RECORDS / f"{fp}-hard.json"
    assert path.is_file(), (
        f"no HARD-tier calibration report for judge fingerprint {fp}: run `make "
        f"judge-calibration` (it records both tiers) and commit "
        f"docs/cn/reports/judge-calibration/{fp}-hard.{{json,md}}")
    record = json.loads(path.read_text(encoding="utf-8"))
    meta = record["meta"]
    assert record["fingerprint"] == meta["fingerprint"] == fp and meta["tier"] == "hard"
    assert not meta["mini"] and meta["pairs"] == len(HARD_PAIRS) and not meta.get("evaluators")
    assert meta["reps"] >= judge_fingerprint.MIN_REPORT_REPS
    assert set(judge_fingerprint.CALIBRATED_JUDGES) <= set(meta["models"])
    assert (RECORDS / f"{fp}-hard.md").is_file()


def test_the_fingerprint_moves_with_a_template_and_a_judge_model(monkeypatch):
    before = judge_fingerprint.fingerprint()
    monkeypatch.setitem(global_templates.SYSTEM_PROMPTS, "Builtin.Helpfulness",
                        global_templates.SYSTEM_PROMPTS["Builtin.Helpfulness"] + "\n- extra")
    after_template = judge_fingerprint.fingerprint()
    monkeypatch.undo()
    assert judge_fingerprint.fingerprint() == before != after_template
    monkeypatch.setattr(judge_fingerprint, "CALIBRATED_JUDGES", ("global.zai.glm-5.3",))
    assert judge_fingerprint.fingerprint() != before


def test_the_fingerprint_covers_every_llm_builtin_prompt():
    prompts = judge_fingerprint.judge_prompts()
    assert len(prompts) == 16 and all(prompts.values())
    assert set(prompts["Builtin.Correctness"]) == {"system_prompt", "reference_system_prompt"}
    assert "Fully Relevant" in prompts["Builtin.ContextRelevance"]["system_prompt"]


def test_the_mini_set_covers_every_evaluator_in_both_languages():
    mini = calibration.mini(PAIRS)
    assert mini == calibration.mini(PAIRS) and len(mini) < len(PAIRS) / 2
    for evaluator_id in {e for p in PAIRS for e in p["labels"]}:
        assert {p["lang"] for p in mini if evaluator_id in p["labels"]} == {"zh", "en"}
    work = calibration.items(mini, probes=False)
    assert {w["variant"] for w in work} == {"base"}


def _entry(evaluator, model, *, graded, value, errors=0):
    return {"evaluator": evaluator, "model": model, "graded": graded, "errors": errors,
            "discrimination": {"rate": value} if graded else None,
            "kappa": None if graded else value}


@pytest.mark.parametrize("now,recorded,drifted", [
    (0.75, 0.9, False),    # within the tolerance
    (0.5, 0.9, True),      # fell more than the tolerance
    (0.45, 0.5, True),     # under the graded floor
    (None, 0.9, True),     # only judge errors
])
def test_drift_flags_a_drop_against_the_record(now, recorded, drifted):
    summary = {"per_evaluator": [_entry("Builtin.Faithfulness", "m", graded=True, value=now)]}
    base = {"per_evaluator": [_entry("Builtin.Faithfulness", "m", graded=True, value=recorded)]}
    result = calibration.drift(summary, base)
    assert result["drift"] is drifted and result["rows"][0]["recorded"] == recorded
    assert ("DRIFT" in calibration.drift_markdown(result, {})) is drifted


def test_drift_reads_kappa_for_a_binary_judge():
    summary = {"per_evaluator": [_entry("Builtin.Refusal", "m", graded=False, value=0.8)]}
    base = {"per_evaluator": [_entry("Builtin.Refusal", "m", graded=False, value=1.0)]}
    [row] = calibration.drift(summary, base)["rows"]
    assert (row["metric"], row["now"], row["drift"]) == ("kappa", 0.8, False)
