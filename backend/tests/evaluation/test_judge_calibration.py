"""EVAL-TRUST 2.1–2.4 (Round 2 item 1) — the judge calibration set and its harness.

The pairs are real corpus turns answered correctly and minimally wrong; their labels are
known by construction (``tests/fixtures/judge_calibration/README.md``). These hermetic tests
pin the set (reproducible, every construction rule holds), the cases the harness builds and
the metric arithmetic; the judge numbers themselves come from ``--calibration`` live runs.
"""

import json
import sys
from pathlib import Path

import pytest
from strands_evals.types.trace import AgentInvocationSpan, ToolExecutionSpan

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "scripts"))
from evalwb import calibration  # noqa: E402

from tests.evaluation.fakes import FakeJudge  # noqa: E402

FIXTURE = BACKEND / "tests" / "fixtures" / "judge_calibration" / "pairs.json"
PAIRS = calibration.load(FIXTURE)


def _builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_pairs", FIXTURE.parent / "build_pairs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_set_is_reproducible_from_the_real_corpus():
    built = _builder().build()
    assert json.loads(json.dumps(built, ensure_ascii=False, sort_keys=True)) == PAIRS


def test_the_set_covers_both_languages_every_defect_and_the_bar_size():
    assert 50 <= len(PAIRS) <= 100
    langs = {p["lang"] for p in PAIRS}
    assert langs == {"zh", "en"}
    assert {p["defect"] for p in PAIRS} == {
        "fact_wrong", "wrong_source", "wrong_refusal", "tool_not_used", "other_person_data",
        "self_contradiction", "padded",  # N17-obs404 (Coherence, Conciseness)
        "off_topic", "other_question", "half_off_topic", "control",  # r17-bh9 relevance
        "tool_repeated", "tool_unrelated",  # N17-obs477 ToolSelectionAccuracy
        "injection_refusal", "declined_action"}  # N17-obs478 Refusal
    for pair in PAIRS:
        assert pair["labels"], pair["id"]
        for label in pair["labels"].values():
            if pair["defect"] == "control":  # a correct answer judged alone (no wrong side)
                assert pair["bad"] is None and label == {"good": True, "bad": None}
                continue
            assert label["good"] != label["bad"], pair["id"]  # each side gets the other call
        for value in pair["wrong"]:
            assert value in pair["bad"]["answer"] and value not in pair["good"]["answer"]
        for fact in pair["facts"]:
            assert fact in "\n".join(t["result"] for t in pair["tools"])


def test_the_construction_refuses_a_wrong_value_the_tools_support():
    builder = _builder()
    with pytest.raises(AssertionError, match="occurs in the tool results"):
        builder.pair("x", "zh", "fact_wrong", builder.HR, builder.FACT,
                     good="还剩 9 天", bad="还剩 25 天", facts=["9"], wrong=["25"], why="t")


def _pair(pid):
    return next(p for p in PAIRS if p["id"] == pid)


def _spans(case, kind):
    return [s for t in case.actual_trajectory.traces for s in t.spans if isinstance(s, kind)]


def test_cases_carry_the_real_tools_and_each_sides_answer():
    pair = _pair("hr-notool-zh")
    good, bad = calibration.case(pair, "good"), calibration.case(pair, "bad")
    assert [s.tool_call.name for s in _spans(good, ToolExecutionSpan)] == [
        "launchpad_gw_hr-database___get_employee"]
    assert _spans(bad, ToolExecutionSpan) == []  # tool_not_used: the call is gone
    assert good.expected_assertion == pair["assertions"][0]
    assert _spans(good, AgentInvocationSpan)[-1].agent_response == pair["good"]["answer"]
    verbose = calibration.case(pair, "good", "verbose")
    assert verbose.actual_output.startswith(pair["verbose"]["prefix"])
    assert verbose.actual_output.endswith(pair["verbose"]["suffix"])


def test_position_reverses_the_tool_calls_and_history_comes_first():
    pair = _pair("chengdu-rate-zh")
    base = [s.tool_call.name for s in _spans(calibration.case(pair, "good"), ToolExecutionSpan)]
    flipped = [s.tool_call.name for s in _spans(calibration.case(pair, "good", "position"),
                                                ToolExecutionSpan)]
    assert flipped == base[::-1] and len(base) == 2
    stock = calibration.case(_pair("stock-qty-en"), "good")
    assert [t.trace_id for t in stock.actual_trajectory.traces] == ["h0", "judged"]


def test_items_expand_each_label_into_sides_and_probes():
    work = calibration.items([_pair("chengdu-rate-zh")])
    by = {(w["evaluator"], w["side"], w["variant"]) for w in work}
    for evaluator in ("Builtin.Correctness", "Builtin.Faithfulness"):
        assert (evaluator, "good", "base") in by and (evaluator, "bad", "base") in by
        assert (evaluator, "good", "verbose") in by and (evaluator, "bad", "verbose") not in by
        assert (evaluator, "good", "position") in by and (evaluator, "bad", "position") in by
    expected = {(w["side"], w["expected"]) for w in work if w["evaluator"] == "Builtin.Correctness"}
    assert expected == {("good", True), ("bad", False)}


def test_kappa():
    assert calibration.kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert calibration.kappa([True, False, True, False], [True, True, True, True]) == 0.0
    assert calibration.kappa([True, True, False, False], [True, False, False, True]) == 0.0
    assert calibration.kappa([], []) is None


def _row(pair, side, expected, positive, *, variant="base", rep=0, model="m", score=None,
         lang="zh", twin=None, defect="fact_wrong", evaluator="Builtin.Correctness"):
    return {"pair": pair, "side": side, "expected": expected, "positive": positive,
            "variant": variant, "rep": rep, "model": model, "lang": lang, "twin": twin,
            "defect": defect, "evaluator": evaluator, "label": str(positive),
            "score": score if score is not None else (1.0 if positive else 0.0)}


def test_summary_rates_rerun_probes_language_and_cross_judge():
    rows = [
        _row("p1", "good", True, True), _row("p1", "bad", False, False),
        _row("p2", "good", True, False), _row("p2", "bad", False, True),  # both wrong
        _row("p1", "good", True, True, rep=1), _row("p1", "bad", False, True, rep=1),  # flip
        _row("p2", "good", True, False, rep=1), _row("p2", "bad", False, True, rep=1),
        _row("p1", "good", True, False, variant="verbose"),  # verbosity flips p1
        _row("p2", "good", True, False, variant="verbose"),
        _row("p1", "good", True, True, model="n"), _row("p1", "bad", False, False, model="n"),
    ]
    rows[0]["twin"] = rows[2]["twin"] = "t"
    rows[2]["lang"] = "en"
    summary = calibration.summarize(rows)
    m = next(p for p in summary["per_evaluator"] if p["model"] == "m")
    assert m["items"] == 4 and m["false_pass_rate"] == 0.5 and m["false_fail_rate"] == 0.5
    assert m["kappa"] == 0.0 and m["meets_bar"] is False
    assert m["rerun_flip_rate"] == 0.25  # p1/bad changed between the reps
    assert m["verbosity"] == {"items": 2, "verdict_changed": 1, "change_rate": 0.5,
                              "mean_score_delta": -0.5}
    assert m["language"]["twins"] == 1 and m["language"]["agree_rate"] == 0.0
    (cross,) = summary["cross_judge"]
    assert (cross["a"], cross["b"], cross["items"], cross["agree_rate"]) == ("m", "n", 2, 1.0)
    assert "κ" in calibration.markdown(summary, {"models": ["m", "n"]})


class _Oracle(FakeJudge):
    """A Correctness judge that is right by construction: Incorrect iff the judged turn
    contains one of the calibration set's wrong values."""

    def __init__(self, wrong):
        super().__init__()
        self.wrong = wrong

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        text = json.dumps(messages, ensure_ascii=False, default=str)
        verdict = "Incorrect" if any(w in text for w in self.wrong) else "Perfectly Correct"
        self.overrides = {"score": verdict}
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def test_the_harness_runs_the_production_engine_and_scores_a_perfect_judge_kappa_one():
    pairs = [p for p in PAIRS if "Builtin.Correctness" in p["labels"]][:8]
    wrong = sorted({w for p in pairs for w in p["wrong"]})
    work = calibration.items(pairs, ["Builtin.Correctness"])
    rows = calibration.judge(work, ["fake-a"], 2,
                             judge_factory=lambda model_id, **kw: _Oracle(wrong),
                             sleep=lambda _s: None, concurrency=2)
    assert all(r["error"] is None for r in rows), [r["error"] for r in rows if r["error"]]
    (summary,) = calibration.summarize(rows)["per_evaluator"]
    assert summary["kappa"] == 1.0 and summary["false_pass_rate"] == 0.0
    assert summary["false_fail_rate"] == 0.0 and summary["rerun_flip_rate"] == 0.0
    assert summary["verbosity"]["verdict_changed"] == 0


def test_an_always_pass_judge_is_caught():
    pairs = [p for p in PAIRS if "Builtin.Correctness" in p["labels"]][:6]
    work = [w for w in calibration.items(pairs, ["Builtin.Correctness"]) if w["variant"] == "base"]
    rows = calibration.judge(work, ["fake-b"], 1,
                             judge_factory=lambda model_id, **kw: FakeJudge(
                                 overrides={"score": "Perfectly Correct"}),
                             sleep=lambda _s: None)
    (summary,) = calibration.summarize(rows)["per_evaluator"]
    assert summary["false_pass_rate"] == 1.0 and summary["kappa"] == 0.0


def test_graded_judges_report_pairwise_discrimination_and_kappa_at_every_cut():
    """The Round 2 Faithfulness reading: good sides all Completely Yes, bad sides Generally
    Yes / Neutral — a perfect discriminator that a single ≥ 0.75 threshold calls 50 %
    false-pass. P(bad < good) is the primary metric; κ is shown at each cut point."""
    rows = []
    for k, bad in enumerate((0.75, 0.75, 0.5, 0.0)):
        rows.append(_row(f"p{k}", "good", True, True, score=1.0,
                         evaluator="Builtin.Faithfulness"))
        rows.append(_row(f"p{k}", "bad", False, bad >= 0.75, score=bad,
                         evaluator="Builtin.Faithfulness"))
    rows.append(_row("tie", "good", True, True, score=1.0, evaluator="Builtin.Faithfulness"))
    rows.append(_row("tie", "bad", False, True, score=1.0, evaluator="Builtin.Faithfulness"))
    (p,) = calibration.summarize(rows)["per_evaluator"]
    assert p["graded"] is True
    # gap (2.1b) = mean(good − bad) = (0.25 + 0.25 + 0.5 + 1.0 + 0) / 5
    assert p["discrimination"] == {"pairs": 5, "rate": 0.8, "tie_rate": 0.2,
                                   "inverted_rate": 0.0, "gap": 0.4}
    assert p["kappa_at"]["1.0"] == 0.8  # only the tie is miscalled at the top-label cut
    assert p["kappa_at"]["0.75"] < p["kappa_at"]["1.0"]
    assert "P(bad<good)" in calibration.markdown(calibration.summarize(rows), {})


def test_binary_judges_keep_binary_kappa_only():
    rows = [_row("p", "good", True, True, evaluator="Builtin.GoalSuccessRate"),
            _row("p", "bad", False, False, evaluator="Builtin.GoalSuccessRate")]
    (p,) = calibration.summarize(rows)["per_evaluator"]
    assert p["graded"] is False and p["discrimination"] is None and p["kappa_at"] is None
    assert p["kappa"] == 1.0


def test_anchors_report_the_share_of_wrong_sides_at_or_below_their_bound():
    rows = [_row("p1", "good", True, True, score=0.833, evaluator="Builtin.Helpfulness"),
            _row("p1", "bad", False, False, score=0.333, evaluator="Builtin.Helpfulness"),
            _row("p2", "good", True, True, score=0.833, evaluator="Builtin.Helpfulness"),
            _row("p2", "bad", False, False, score=0.667, evaluator="Builtin.Helpfulness")]
    for row in rows:
        row["bad_max"] = 0.333 if row["side"] == "bad" else None
    (p,) = calibration.summarize(rows)["per_evaluator"]
    assert p["anchor"] == {"items": 2, "bad_max": 0.333, "hit_rate": 0.5, "mean_score": 0.5}


def test_response_relevance_pairs_and_controls():
    """r17-bh9: ResponseRelevance was unmeasured — 8 pairs (off topic, a neighbouring
    question, half unrelated; zh + en) and 3 controls the rubric must not mark down."""
    relevance = [p for p in PAIRS if "Builtin.ResponseRelevance" in p["labels"]]
    pairs = [p for p in relevance if p["defect"] != "control"]
    controls = [p for p in relevance if p["defect"] == "control"]
    assert len(pairs) == 8 and len(controls) == 3
    assert {p["defect"] for p in pairs} == {"off_topic", "other_question", "half_off_topic"}
    assert {p["lang"] for p in pairs} == {"zh", "en"}
    work = calibration.items(relevance)
    assert {w["variant"] for w in work} <= {"base", "position"}  # no verbosity padding
    assert {w["side"] for w in work if w["pair"].startswith("relevance-control")} == {"good"}


class _ToolOracle(FakeJudge):
    """A ToolSelectionAccuracy judge that is right by construction: No iff the target call
    is the shell, or repeats a call already in the conversation."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        text = json.dumps(messages, ensure_ascii=False, default=str)
        history, _, target = text.partition("Target tool-call to evaluate")
        call = target.split("Tool call: ", 1)[1].split('"', 1)[0]  # the end of the JSON string
        no = call.startswith("shell(") or f"Tool call: {call}" in history
        self.overrides = {"score": "No" if no else "Yes"}
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def test_tool_selection_pairs_judge_each_sides_last_call():
    """N17-obs477: a tool-level pair differs in the judged turn's tool calls, not its answer;
    the harness judges the LAST call, lists the same tools (with descriptions) on both
    sides, and runs no verbosity / position probe."""
    tsa = [p for p in PAIRS if "Builtin.ToolSelectionAccuracy" in p["labels"]]
    assert {p["defect"] for p in tsa} == {"tool_repeated", "tool_unrelated", "control"}
    assert {p["lang"] for p in tsa} == {"zh", "en"} and len(tsa) == 6
    repeat = _pair("tsa-repeat-zh")
    good, bad = calibration.case(repeat, "good"), calibration.case(repeat, "bad")
    assert [s.tool_call.name for s in _spans(good, ToolExecutionSpan)] == ["kb_search"]
    calls = [s.tool_call for s in _spans(bad, ToolExecutionSpan)]
    assert len(calls) == 2 and calls[0].arguments == calls[1].arguments
    unrelated = _pair("tsa-unrelated-en")
    for side in ("good", "bad"):
        (agent,) = _spans(calibration.case(unrelated, side), AgentInvocationSpan)
        assert [t.name for t in agent.available_tools] == ["kb_search", "shell"]
        assert agent.available_tools[0].description
    work = calibration.items(tsa)
    assert {w["variant"] for w in work} == {"base"}
    assert {w["side"] for w in work if w["defect"] == "control"} == {"good"}
    rows = calibration.judge(work, ["fake-a"], 1,
                             judge_factory=lambda model_id, **kw: _ToolOracle(),
                             sleep=lambda _s: None, concurrency=2)
    assert all(r["error"] is None for r in rows), [r["error"] for r in rows if r["error"]]
    assert all(r["positive"] == r["expected"] for r in rows), [
        (r["pair"], r["side"], r["label"]) for r in rows if r["positive"] != r["expected"]]


def test_refusal_limitation_pairs_are_balanced_and_sourced():
    """N17-obs478: explicit declines next to a limitation are refused; information the
    assistant looked for (or has no source for) and reports missing is not. zh / en twins,
    each construction naming the real run / session / turn it copies."""
    pairs = [p for p in PAIRS if p["id"].startswith(("refusal-injection", "refusal-action",
                                                     "refusal-notfound", "refusal-noaccess"))]
    assert len(pairs) == 8 and sum(p["lang"] == "zh" for p in pairs) == 4
    assert all(p["source"]["from"] for p in pairs)
    declines = [p for p in pairs if p["defect"] in ("injection_refusal", "declined_action")]
    assert all(p["labels"]["Builtin.Refusal"] == {"good": True, "bad": False} for p in declines)
    limits = [p for p in pairs if p["defect"] == "wrong_refusal"]
    assert all(p["labels"]["Builtin.Refusal"] == {"good": False, "bad": True} for p in limits)
