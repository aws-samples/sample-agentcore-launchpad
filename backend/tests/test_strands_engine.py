"""N12 E1-3 — the Strands evaluation engine (`evaluation/strands_engine.py` over the pure
`services/strands_eval/engine_core.py`), hermetic: the span fixture through `FakeLogs`, a
schema-driven `FakeJudge` as the model, SQLite for `eval_results`."""

import json
from pathlib import Path

import pytest
from strands_evals.types.trace import Session

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import evaluators_local, results_store
from app.evaluation.jobs import record_id
from app.evaluation.models import EvalEvaluator, EvalResult
from app.evaluation.strands_engine import StrandsEngine
from app.optimization.models import ExperimentAssignment
from app.services.strands_eval import engine_core
from app.services.strands_eval import insights as insights_lib
from app.services.strands_eval.results import aggregate_scores
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeJudge, FakeLogs

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "spans_session.json").read_text()
)
SID, GROUP = FIXTURE["session_id"], FIXTURE["log_group"]
SID2 = "sess-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _records_for(session_id: str) -> list[dict]:
    return [{**r, "attributes": {**r["attributes"], "session.id": session_id}}
            for r in FIXTURE["records"]]


def logs_with(*session_ids: str) -> FakeLogs:
    routes = {}
    for sid in session_ids or (SID,):
        routes[f"= '{sid}' | sort"] = [{"@message": json.dumps(r)} for r in _records_for(sid)]
        routes[f"parentSpanId | filter attributes.session.id = '{sid}'"] = []
    return FakeLogs(routes)


@pytest.fixture
def ws():
    return ws_ctx(id=DEFAULT_WORKSPACE_ID, region="cn-northwest-1")


def engine_with(logs=None, judge=None, *, judges=None, lambda_client=None, sleep=None):
    judge = judge or FakeJudge()
    calls: list = []

    def judge_factory(model_id):
        calls.append(model_id)
        if judges is not None:
            return judges[model_id]
        return judge

    slept: list = []
    eng = StrandsEngine(
        logs_client=lambda _ws: logs or logs_with(),
        lambda_client=lambda _ws: lambda_client,
        judge_factory=judge_factory,
        sleep=sleep or slept.append,
    )
    eng.judge_calls = calls  # type: ignore[attr-defined]
    eng.slept = slept  # type: ignore[attr-defined]
    return eng


def evaluate(eng, ws, **overrides):
    kwargs = dict(source="run", owner_id="run1", session_ids=[SID], log_groups=[GROUP],
                  evaluator_ids=["Builtin.Helpfulness"], ground_truth=None,
                  judge_model_id=None, stop=None, progress=None, persist=True)
    kwargs.update(overrides)
    return eng.evaluate_sessions(ws, **kwargs)


def rows():
    with SessionLocal() as db:
        return db.query(EvalResult).order_by(EvalResult.id).all()


# ─── records ─────────────────────────────────────────────────────────────────
def test_one_record_per_session_and_evaluator_with_score_label_explanation(ws):
    judge = FakeJudge(overrides={"score": "Very helpful", "reasoning": "clear answer"})
    records = evaluate(engine_with(judge=judge), ws,
                       evaluator_ids=["Builtin.Helpfulness", "Builtin.GoalSuccessRate"])
    assert [r["evaluator_id"] for r in records] == ["Builtin.Helpfulness",
                                                    "Builtin.GoalSuccessRate"]
    helpful = records[0]
    assert helpful["session_id"] == SID and helpful["level"] == "TRACE"
    assert helpful["score"] == pytest.approx(0.833) and helpful["label"] == "Very Helpful"
    assert helpful["explanation"] == "clear answer"
    assert helpful["error_type"] is None and helpful["error_message"] is None
    assert helpful["trace_id"] == "dddddddddddddddddddddddddddddddd"
    assert helpful["id"] == record_id("run1", SID, "Builtin.Helpfulness", 0)
    assert records[1]["level"] == "SESSION"


def test_persist_writes_eval_results_rows_idempotently(ws):
    eng = engine_with()
    evaluate(eng, ws)
    evaluate(eng, ws)  # a re-run (recheck / resumed poller) rewrites the same rows
    stored = rows()
    assert len(stored) == 1
    row = stored[0]
    assert row.source == "run" and row.run_id == "run1" and row.config_id is None
    assert row.workspace_id == DEFAULT_WORKSPACE_ID and row.session_id == SID
    assert row.evaluator_id == "Builtin.Helpfulness" and row.score is not None
    assert results_store.read_run_records(SessionLocal(), "run1")[0]["id"] == row.id


def test_online_source_fills_config_id_not_run_id(ws):
    evaluate(engine_with(), ws, source="online", owner_id="cfg1")
    row = rows()[0]
    assert row.source == "online" and row.config_id == "cfg1" and row.run_id is None


def test_persist_false_writes_nothing(ws):
    records = evaluate(engine_with(), ws, source="ondemand", owner_id=SID, persist=False)
    assert records and rows() == []


def test_arm_is_filled_from_experiment_assignments(ws):
    with SessionLocal() as db:
        db.add(ExperimentAssignment(workspace_id=DEFAULT_WORKSPACE_ID, owner_kind="experiment",
                                    owner_id="exp1", session_id=SID, arm="T1"))
        db.commit()
    evaluate(engine_with(), ws)
    assert rows()[0].arm == "T1"


def test_sessions_without_an_assignment_have_no_arm(ws):
    evaluate(engine_with(), ws)
    assert rows()[0].arm is None


# ─── ground truth ─────────────────────────────────────────────────────────────
def test_trajectory_evaluator_scores_ground_truth_without_any_judge(ws):
    eng = engine_with()
    records = evaluate(eng, ws, evaluator_ids=["Builtin.TrajectoryInOrderMatch"],
                       ground_truth={SID: {"expected_trajectory": ["hr_lookup"]}})
    assert records[0]["score"] == 1.0 and records[0]["label"] == "Yes"
    assert eng.judge_calls == []  # no model was ever built


def test_trajectory_mismatch_scores_zero(ws):
    records = evaluate(engine_with(), ws, evaluator_ids=["Builtin.TrajectoryExactOrderMatch"],
                       ground_truth={SID: {"expected_trajectory": ["issue_refund"]}})
    assert records[0]["score"] == 0.0 and records[0]["label"] == "No"


def test_missing_expected_trajectory_is_not_applicable(ws):
    records = evaluate(engine_with(), ws, evaluator_ids=["Builtin.TrajectoryAnyOrderMatch"])
    assert records[0]["label"] == "NOT_APPLICABLE" and records[0]["score"] is None
    assert aggregate_scores(records) == []  # not counted into the run mean


def test_ground_truth_becomes_the_evaluation_data():
    truth = {"scenario_id": "refund", "input": "I want a refund",
             "expected_response": "refund policy explained",
             "assertions": ["confirms the refund", "stays polite"],
             "expected_trajectory": ["lookup_order"]}
    session = Session(session_id=SID, traces=[])
    case = engine_core.evaluation_data(SID, {"output": "done", "trajectory": session}, truth)
    assert case.input == "I want a refund" and case.actual_output == "done"
    assert case.expected_output == "refund policy explained"
    assert case.expected_assertion == "confirms the refund\nstays polite"
    assert case.expected_trajectory == ["lookup_order"] and case.name == "refund"
    assert case.actual_trajectory is session


def test_without_ground_truth_the_input_is_the_sessions_first_prompt():
    provider = StrandsEngine(logs_client=lambda _ws: logs_with(),
                             judge_factory=lambda _m: FakeJudge()).provider(None, [GROUP])
    case = engine_core.evaluation_data(SID, provider.get_evaluation_data(SID), None)
    assert case.input and "vacation" in case.input.lower()
    assert case.expected_output is None and case.expected_assertion is None


def test_ground_truth_from_scenarios_pairs_sessions_positionally():
    scenarios = [
        {"scenario_id": "a", "turns": [{"input": "q1", "expected_response": "r1"}],
         "assertions": ["x"], "expected_trajectory": ["t"]},
        {"scenario_id": "b", "turns": [{"input": {"content": "q2"}}]},
    ]
    truth = engine_core.ground_truth_from_scenarios(scenarios, ["s1", "s2"])
    assert truth["s1"] == {"scenario_id": "a", "input": "q1", "expected_response": "r1",
                           "assertions": ["x"], "expected_trajectory": ["t"],
                           "turns": [{"input": "q1", "expected_response": "r1"}]}
    assert truth["s2"]["input"] == "q2" and truth["s2"]["expected_response"] is None


# ─── failures, retries, stop, progress ───────────────────────────────────────
def test_a_judge_exception_becomes_an_error_record_not_a_crash(ws):
    judge = FakeJudge(fail_with=RuntimeError("judge exploded"))
    records = evaluate(engine_with(judge=judge), ws)
    assert len(records) == 1
    assert records[0]["score"] is None and records[0]["label"] is None
    assert records[0]["error_type"] == "RuntimeError"
    assert "judge exploded" in records[0]["error_message"]
    assert rows()[0].error_type == "RuntimeError"


def test_throttling_is_retried_with_backoff_then_succeeds(ws, monkeypatch):
    from strands_evals.utils import ModelThrottledException

    attempts = {"n": 0}
    judge = FakeJudge()

    class Flaky:
        name = "Builtin.Helpfulness"

        def evaluate(self, case):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise ModelThrottledException("slow down")
            return evaluators_lib.build({"id": "Builtin.Helpfulness"}, judge).evaluate(case)

    from app.services.strands_eval import evaluators as evaluators_lib

    monkeypatch.setattr(engine_core.EvaluatorSet, "evaluator", lambda self, d: Flaky())
    eng = engine_with(judge=judge)
    records = evaluate(eng, ws)
    assert records[0]["score"] is not None and attempts["n"] == 3
    assert eng.slept == [2.0, 4.0]


def test_throttling_beyond_the_budget_is_an_error_record(ws, monkeypatch):
    from strands_evals.utils import ModelThrottledException

    class Always:
        name = "Builtin.Helpfulness"

        def evaluate(self, case):
            raise ModelThrottledException("still throttled")

    monkeypatch.setattr(engine_core.EvaluatorSet, "evaluator", lambda self, d: Always())
    eng = engine_with()
    records = evaluate(eng, ws)
    assert records[0]["error_type"] == "ModelThrottledException" and eng.slept == [2.0, 4.0, 8.0]


def test_stop_between_sessions_keeps_partial_records(ws):
    flags = {"stop": False}

    def progress(text):
        flags["stop"] = True  # the operator stops after the first session finished

    records = evaluate(engine_with(logs_with(SID, SID2)), ws, session_ids=[SID, SID2],
                       stop=lambda: flags["stop"], progress=progress)
    # concurrency submits both before the flag flips; the second session is still judged
    # or skipped — but nothing crashes and every judged session has its rows
    assert {r["session_id"] for r in records} <= {SID, SID2}
    assert len(rows()) == len(records) >= 1


def test_stop_before_the_first_session_judges_nothing(ws):
    records = evaluate(engine_with(logs_with(SID, SID2)), ws, session_ids=[SID, SID2],
                       stop=lambda: True)
    assert records == [] and rows() == []


def test_progress_reports_each_finished_session(ws):
    seen: list[str] = []
    evaluate(engine_with(logs_with(SID, SID2)), ws, session_ids=[SID, SID2],
             progress=seen.append)
    assert seen == ["evaluating 1/2", "evaluating 2/2"]


def test_a_missing_session_yields_an_error_record_per_evaluator(ws):
    records = evaluate(engine_with(FakeLogs({})), ws,
                       evaluator_ids=["Builtin.Helpfulness", "Builtin.Correctness"])
    assert [r["evaluator_id"] for r in records] == ["Builtin.Helpfulness", "Builtin.Correctness"]
    assert all(r["error_type"] == "SessionNotFoundError" for r in records)
    assert all(r["score"] is None for r in records)


def test_tool_call_evaluators_fan_out_one_record_per_output(ws, monkeypatch):
    from strands_evals.types.evaluation import EvaluationOutput

    class TwoCalls:
        name = "Builtin.ToolSelectionAccuracy"

        def evaluate(self, case):
            return [EvaluationOutput(score=1.0, test_pass=True, label="Yes", reason="a"),
                    EvaluationOutput(score=0.0, test_pass=False, label="No", reason="b")]

    monkeypatch.setattr(engine_core.EvaluatorSet, "evaluator", lambda self, d: TwoCalls())
    records = evaluate(engine_with(), ws, evaluator_ids=["Builtin.ToolSelectionAccuracy"])
    assert [r["score"] for r in records] == [1.0, 0.0]
    assert [r["level"] for r in records] == ["TOOL_CALL", "TOOL_CALL"]
    assert records[0]["id"] != records[1]["id"]  # ordinal 0 and 1
    assert len(rows()) == 2
    assert aggregate_scores(records) == [{"evaluatorId": "Builtin.ToolSelectionAccuracy",
                                          "score": 0.5, "count": 1, "n": 2,
                                          "unit": "tool_call"}]


def test_sessions_keep_their_order_under_concurrency(ws, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_EVAL_JUDGE_CONCURRENCY", "3")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        sids = [SID, SID2, "sess-cccccccccccccccccccccccccccccccccccccccc"]
        records = evaluate(engine_with(logs_with(*sids)), ws, session_ids=sids)
        assert [r["session_id"] for r in records] == sids
    finally:
        get_settings.cache_clear()


# ─── judge selection ─────────────────────────────────────────────────────────
def test_builtins_use_the_default_judge_model_unless_overridden(ws, monkeypatch):
    eng = engine_with()
    evaluate(eng, ws)
    assert eng.judge_calls == ["global.zai.glm-5.3"]
    eng = engine_with()
    evaluate(eng, ws, judge_model_id="global.moonshotai.kimi-k3")
    assert eng.judge_calls == ["global.moonshotai.kimi-k3"]


def test_a_custom_judge_row_runs_on_its_own_model_with_its_rubric(ws):
    with SessionLocal() as db:
        db.add(EvalEvaluator(id="cust_tone", workspace_id=DEFAULT_WORKSPACE_ID, name="tone",
                             level="TRACE", definition="judge",
                             model_id="global.moonshotai.kimi-k3",
                             instructions="Rate the tone of {assistant_turn}",
                             rating_scale=[{"value": 1.0, "label": "pass", "definition": "p"},
                                           {"value": 0.0, "label": "fail", "definition": "f"}]))
        db.commit()
    kimi = FakeJudge(overrides={"score": 1.0, "label": "pass", "test_pass": True,
                                "reason": "polite"})
    glm = FakeJudge()
    eng = engine_with(judges={"global.moonshotai.kimi-k3": kimi, "global.zai.glm-5.3": glm})
    records = evaluate(eng, ws, evaluator_ids=["cust_tone", "Builtin.Helpfulness"])
    assert sorted(eng.judge_calls) == ["global.moonshotai.kimi-k3", "global.zai.glm-5.3"]
    tone = next(r for r in records if r["evaluator_id"] == "cust_tone")
    assert tone["score"] == 1.0 and tone["label"] == "pass" and tone["explanation"] == "polite"
    assert any("Rate the tone" in json.dumps(c.get("messages", ""), default=str)
               for c in kimi.calls)


def test_a_derived_row_takes_its_bases_level(ws):
    with SessionLocal() as db:
        db.add(EvalEvaluator(id="cust_der", workspace_id=DEFAULT_WORKSPACE_ID, name="der",
                             level="SESSION", definition="derived",
                             base_evaluator_id="Builtin.GoalSuccessRate",
                             model_id="global.zai.glm-5.3", rating_scale=[]))
        db.commit()
    records = evaluate(engine_with(), ws, evaluator_ids=["cust_der"])
    assert records[0]["evaluator_id"] == "cust_der" and records[0]["level"] == "SESSION"
    assert records[0]["score"] in (0.0, 1.0)


def test_unknown_evaluator_ids_are_refused_before_any_judge_call(ws):
    eng = engine_with()
    with pytest.raises(AppError) as exc:
        evaluate(eng, ws, evaluator_ids=["cust_missing"])
    assert exc.value.code == "run.evaluator_not_found" and eng.judge_calls == []


def test_code_evaluators_are_error_records_while_disabled(ws):
    with SessionLocal() as db:
        db.add(EvalEvaluator(id="cust_code", workspace_id=DEFAULT_WORKSPACE_ID, name="code",
                             level="SESSION", definition="code", rating_scale=[],
                             lambda_arn="arn:aws-cn:lambda:cn-northwest-1:1:function:f",
                             lambda_timeout_s=30))
        db.commit()
    records = evaluate(engine_with(), ws, evaluator_ids=["cust_code"])
    assert records[0]["error_type"] == "RuntimeError"
    assert "code evaluators are disabled" in records[0]["error_message"]


def test_code_evaluators_invoke_the_injected_lambda_when_enabled(ws, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            db.add(EvalEvaluator(id="cust_code", workspace_id=DEFAULT_WORKSPACE_ID, name="code",
                                 level="SESSION", definition="code", rating_scale=[],
                                 lambda_arn="arn:aws-cn:lambda:cn-northwest-1:1:function:f",
                                 lambda_timeout_s=30))
            db.commit()

        class FakeLambda:
            calls: list = []

            def invoke(self, **kwargs):
                self.calls.append(kwargs)

                class _P:
                    def read(self):
                        return json.dumps({"label": "pass", "value": 1.0,
                                           "explanation": "ok"}).encode()
                return {"StatusCode": 200, "Payload": _P()}

        records = evaluate(engine_with(lambda_client=FakeLambda()), ws,
                           evaluator_ids=["cust_code"])
        assert records[0]["score"] == 1.0 and records[0]["label"] == "pass"
        assert FakeLambda.calls[0]["FunctionName"].endswith(":function:f")
        payload = json.loads(FakeLambda.calls[0]["Payload"])
        assert payload["evaluationInput"]["actual_trajectory"] == ["hr_lookup"]
    finally:
        get_settings.cache_clear()


# ─── insights / session ──────────────────────────────────────────────────────
def test_engine_session_returns_a_strands_session(ws):
    session = engine_with().session(ws, session_id=SID, log_groups=[GROUP])
    assert isinstance(session, Session) and session.session_id == SID


def test_diagnose_sessions_returns_the_three_trees(ws):
    judge = FakeJudge(by_tool={
        "SessionSummary": {"intent_label": "Leave balance", "intent": "asks for PTO left",
                           "summary_label": "HR lookup", "summary": "looked up HR and answered"},
    }, text=json.dumps({"errors": []}))
    out = engine_with(judge=judge).diagnose_sessions(
        ws, session_ids=[SID], log_groups=[GROUP], kinds=list(evaluators_local.INSIGHT_TYPES),
        judge_model_id=None,
    )
    assert set(out) >= {"failures", "userIntents", "executionSummaries"}
    assert out["failures"] == []
    intent = out["userIntents"][0]
    assert intent["name"] == "Leave balance" and intent["affectedSessionCount"] == 1
    assert intent["percentage"] == 100.0
    assert intent["affectedSessions"][0]["sessionId"] == SID
    assert intent["affectedSessions"][0]["userMessages"][0]
    summary = out["executionSummaries"][0]
    assert summary["name"] == "HR lookup"
    assert summary["affectedSessions"][0]["approachTaken"] == "looked up HR and answered"
    assert "12 vacation days" in summary["affectedSessions"][0]["finalOutcome"]


FAILURE_JSON = json.dumps({"errors": [{
    "location": "llm2", "category": ["Tool misuse"], "confidence": ["high"],
    "evidence": ["called the wrong tool"],
}]})
RCA_JSON = json.dumps({"root_causes": [{
    "failure_span_id": "llm2", "location": "llm2", "failure_causality": "PRIMARY_FAILURE",
    "failure_propagation_impact": ["QUALITY_DEGRADATION"],
    "failure_detection_timing": "IMMEDIATELY_AT_OCCURRENCE",
    "completion_status": "PARTIAL_SUCCESS",
    "root_cause_explanation": "ambiguous tool description",
    "fix_recommendation": {"fix_type": "TOOL_DESCRIPTION_FIX",
                           "recommendation": "describe hr_lookup precisely"},
}]})


def test_diagnose_sessions_clusters_failures_with_root_causes(ws):
    # failure detection parses JSON out of a plain-text reply; the root-cause analysis
    # forces the RCAStructuredOutput tool — each scripted through its own channel
    judge = FakeJudge(text=FAILURE_JSON, by_tool={"RCAStructuredOutput": json.loads(RCA_JSON)})
    out = engine_with(judge=judge).diagnose_sessions(
        ws, session_ids=[SID], log_groups=[GROUP], kinds=["Builtin.Insight.FailureAnalysis"],
        judge_model_id=None,
    )
    assert set(out) == {"failures", "failureDetection"}, out  # N17-obs449: + the detection counts
    cluster = out["failures"][0]
    assert cluster["category"] == cluster["name"] == "Tool misuse"
    assert cluster["description"] == "called the wrong tool"
    assert cluster["affectedSessionCount"] == 1 and cluster["percentage"] == 100.0
    assert cluster["affectedSessions"] == [{"sessionId": SID}]
    assert cluster["subCategories"] == [{"name": "TOOL_DESCRIPTION_FIX", "rootCauses": [
        {"name": "ambiguous tool description", "recommendation": "describe hr_lookup precisely"},
    ]}]
    assert cluster["fix_recommendation"] == "describe hr_lookup precisely"


def test_diagnose_sessions_subset_of_kinds_yields_only_those_trees(ws):
    judge = FakeJudge(by_tool={"SessionSummary": {
        "intent_label": "x", "intent": "y", "summary_label": "z", "summary": "w"}})
    out = engine_with(judge=judge).diagnose_sessions(
        ws, session_ids=[SID], log_groups=[GROUP], kinds=["Builtin.Insight.UserIntent"],
        judge_model_id=None,
    )
    assert set(out) == {"userIntents"}


# ─── N-SCN-bug2: judge output budget ─────────────────────────────────────────
from strands.types.exceptions import MaxTokensReachedException  # noqa: E402

MAX_TOKENS = MaxTokensReachedException("Model stopped generating due to maximum token limit.")


def _budget_engine(good_from: int, logs=None):
    """A judge that hits the output limit below ``good_from`` tokens; the first call (no
    explicit max_tokens) is the model-aware default (GLM 5.3 → 4096)."""
    calls: list[tuple] = []

    def judge_factory(model_id, max_tokens=None, **kw):
        calls.append((model_id, max_tokens))
        if (max_tokens or 4096) < good_from:
            return FakeJudge(fail_with=MAX_TOKENS)
        return FakeJudge(overrides={"score": "Very helpful", "reasoning": "fits now"})

    eng = StrandsEngine(logs_client=lambda _ws: logs or logs_with(),
                        lambda_client=lambda _ws: None, judge_factory=judge_factory,
                        sleep=lambda _s: None)
    eng.judge_calls = calls  # type: ignore[attr-defined]
    return eng


def test_score_now_recovers_from_one_max_tokens_on_a_doubled_budget(ws):
    """Counting run #2 (a): SCORE NOW `Builtin.Helpfulness` on a 45-span session raised
    MaxTokensReachedException at the judge default and was recorded as a terminal error."""
    eng = _budget_engine(good_from=8192)
    records = evaluate(eng, ws, source="ondemand", owner_id=SID, persist=False)
    assert len(records) == 1 and records[0]["error_type"] is None
    assert records[0]["score"] == pytest.approx(0.833) and records[0]["explanation"] == "fits now"
    # first attempt at the model-aware default (the factory's own), retry at 2× explicitly
    assert eng.judge_calls == [("global.zai.glm-5.3", None), ("global.zai.glm-5.3", 8192)]
    assert rows() == []


def test_a_judge_that_never_fits_is_an_error_record_naming_the_budget_tried(ws):
    eng = _budget_engine(good_from=10**9)
    records = evaluate(eng, ws)
    assert [c[1] for c in eng.judge_calls] == [None, 8192, 16384]  # 4096 → 8192 → cap
    assert records[0]["score"] is None
    assert records[0]["error_type"] == "JudgeBudgetExceeded"
    assert records[0]["error_message"] == (
        "judge Builtin.Helpfulness exceeded 16384 output tokens after 3 attempts"
        # N17-obs385: the rescue steps the error names
        "; the context had nothing to truncate; no judge_fallback model is configured")


def test_a_lifted_setting_is_the_floor_and_the_cap_bounds_the_retries(ws, monkeypatch):
    from app.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "eval_judge_max_tokens", 6000)
    monkeypatch.setattr(settings, "eval_judge_max_tokens_cap", 8000)
    eng = _budget_engine(good_from=10**9)
    records = evaluate(eng, ws)
    assert eng.judge_calls == [("global.zai.glm-5.3", None), ("global.zai.glm-5.3", 8000)]
    assert "exceeded 8000 output tokens after 2 attempts" in records[0]["error_message"]


class _TightJudge(FakeJudge):
    """Hits the output limit while its own max_tokens is under ``fits_at``."""

    def __init__(self, fits_at: int, **kw):
        super().__init__(**kw)
        self.fits_at = fits_at
        self.update_config(max_tokens=1024)
        self.budgets_seen: list[int] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        budget = int(self.get_config()["max_tokens"])
        self.budgets_seen.append(budget)
        if budget < self.fits_at:
            raise MAX_TOKENS
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


def test_diagnose_sessions_retries_the_summariser_with_a_doubled_budget_and_restores_it(ws):
    judge = _TightJudge(2048, by_tool={"SessionSummary": {
        "intent_label": "Leave balance", "intent": "asks for PTO left",
        "summary_label": "HR lookup", "summary": "looked up HR and answered"}})
    out = engine_with(judge=judge).diagnose_sessions(
        ws, session_ids=[SID], log_groups=[GROUP], kinds=[insights_lib.USER_INTENT],
        judge_model_id=None,
    )
    assert out["userIntents"][0]["name"] == "Leave balance" and "errors" not in out
    assert judge.budgets_seen == [1024, 2048]
    assert judge.get_config()["max_tokens"] == 1024  # the shared judge keeps its budget


def test_diagnose_sessions_names_the_budget_when_the_summariser_never_fits(ws):
    judge = _TightJudge(10**9)
    out = engine_with(judge=judge).diagnose_sessions(
        ws, session_ids=[SID], log_groups=[GROUP], kinds=[insights_lib.USER_INTENT],
        judge_model_id=None,
    )
    assert out["userIntents"] == []
    assert judge.budgets_seen == [1024, 2048, 4096]
    assert out["errors"][0]["error"] == (
        "Summary JudgeBudgetExceeded: summariser exceeded 4096 output tokens after 3 attempts")
