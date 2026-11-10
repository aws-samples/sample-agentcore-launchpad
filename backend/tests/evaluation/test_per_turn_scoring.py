"""N17-obs324 / obs322 — TRACE evaluators score every turn, as Global AgentCore Evaluations.

Global scores a TRACE evaluator once per turn: ``{context}`` is the earlier turns plus the
current turn's prompt and tool calls, ``{assistant_turn}`` the current answer, the row names
the turn's trace. The strands builtins read only the LAST turn of the trajectory they get, so
before this change a session got one TRACE row (its last turn, stamped with the FIRST trace
id), a custom TRACE judge saw every answer, and multi-turn ground truth was joined.

Real sessions (redacted): hr_helpdesk_wb006 (8 turns, 5 with tool calls, refusals) and
hr_helpdesk_wb007 (5 turns), in both query modes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.evaluation import results_store
from app.services.strands_eval import engine_core
from app.services.strands_eval.results import aggregate_scores
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_final_answer_output import _GroupLogs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SESSIONS = {
    "wb006": json.loads((FIXTURES / "obs324_wb006_8turn_session.json").read_text("utf-8")),
    "wb007": json.loads((FIXTURES / "obs324_wb007_5turn_session.json").read_text("utf-8")),
}
CASES = [(name, mode) for name in sorted(SESSIONS) for mode in ("both", "runtime_only")]


def _provider(name: str, mode: str) -> tuple[LaunchpadSpansProvider, str]:
    fx = SESSIONS[name]
    groups = [fx["log_group"], SPANS_LOG_GROUP] if mode == "both" else [fx["log_group"]]
    return LaunchpadSpansProvider(_GroupLogs(fx["records"]), groups), fx["session_id"]


def _turns(provider: LaunchpadSpansProvider, sid: str) -> list[dict[str, Any]]:
    from strands_evals.types.trace import AgentInvocationSpan, ToolExecutionSpan

    out = []
    for trace in provider.get_evaluation_data(sid)["trajectory"].traces:
        agent = next(s for s in trace.spans if isinstance(s, AgentInvocationSpan))
        out.append({"trace_id": trace.trace_id, "prompt": agent.user_prompt,
                    "answer": agent.agent_response,
                    "tools": [s for s in trace.spans if isinstance(s, ToolExecutionSpan)]})
    return out


def _evaluate(name: str, mode: str, definitions: list[dict[str, Any]], *,
              truth: dict[str, Any] | None = None, judge: FakeJudge | None = None):
    provider, sid = _provider(name, mode)
    judge = judge or FakeJudge()
    evaluators = engine_core.EvaluatorSet(definitions, lambda *_a, **_k: judge)
    records = engine_core.evaluate_session(provider, sid, evaluators, truth, job_id="job-1",
                                           sleep=lambda _s: None)
    return records, judge, _turns(provider, sid), sid


def _prompt_texts(judge: FakeJudge) -> list[str]:
    return [json.dumps(call["messages"], ensure_ascii=False) for call in judge.calls]


def _key(text: str) -> str:
    """A stable, JSON-escape-free probe of an answer (its first line, CJK-safe)."""
    return text.strip().splitlines()[0][:24].replace('"', "").replace("\\", "")


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_trace_builtins_score_every_turn_on_its_own_answer_with_its_trace_id(name, mode):
    records, judge, turns, _ = _evaluate(name, mode, [{"id": "Builtin.Helpfulness"}])
    assert len(records) == len(turns) == {"wb006": 8, "wb007": 5}[name]
    assert [r["trace_id"] for r in records] == [t["trace_id"] for t in turns]
    assert len({r["id"] for r in records}) == len(records)
    assert all(r["level"] == "TRACE" and r["error_type"] is None for r in records)
    prompts = _prompt_texts(judge)
    assert len(prompts) == len(turns)
    for k, prompt in enumerate(prompts):
        # turn k's judge reads turn k's answer, and never a later turn's prompt
        assert _key(turns[k]["answer"]) in prompt
        for later in turns[k + 1:]:
            assert later["prompt"][:20] not in prompt
    agg = aggregate_scores(records)
    assert agg[0]["n"] == len(turns) and agg[0]["count"] == 1 and agg[0]["unit"] == "turn"


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_per_turn_cases_carry_the_turns_own_input_output_and_expected(name, mode):
    provider, sid = _provider(name, mode)
    turns = _turns(provider, sid)
    scenario = {"scenario_id": "mt", "assertions": ["A-scenario"],
                "turns": [{"input": t["prompt"], "expected_response": f"EXPECTED-T{k + 1}"}
                          for k, t in enumerate(turns)]}
    truth = engine_core.ground_truth_from_scenarios([scenario], [sid])[sid]
    case = engine_core.evaluation_data(sid, provider.get_evaluation_data(sid), truth)
    cases = engine_core.turn_cases(case, truth)
    assert [tid for tid, _ in cases] == [t["trace_id"] for t in turns]
    for k, (_tid, turn_case) in enumerate(cases):
        assert turn_case.input == turns[k]["prompt"]
        assert turn_case.actual_output == turns[k]["answer"]
        assert turn_case.expected_output == f"EXPECTED-T{k + 1}"
        assert len(turn_case.actual_trajectory.traces) == k + 1
        # the scenario-wide assertions stay with the session case (and the final turn)
        assert turn_case.expected_assertion == ("A-scenario" if k == len(turns) - 1 else None)
    assert case.expected_output == "\n".join(f"EXPECTED-T{k + 1}" for k in range(len(turns)))


CUSTOM_TRACE = {"id": "cust-trace", "definition": "judge", "level": "TRACE",
                "instructions": "CONTEXT<<{context}>> ANSWER<<{assistant_turn}>> "
                                "USER<<{user_message}>>",
                "rating_scale": [{"label": "pass", "value": 1.0}, {"label": "fail", "value": 0.0}]}


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_custom_trace_context_stops_before_the_judged_turns_answer(name, mode):
    records, judge, turns, _ = _evaluate(name, mode, [CUSTOM_TRACE])
    assert len(records) == len(turns)
    for k, prompt in enumerate(_prompt_texts(judge)):
        context = prompt.split("CONTEXT<<", 1)[1].split(">> ANSWER<<", 1)[0]
        answer = prompt.split(">> ANSWER<<", 1)[1].split(">> USER<<", 1)[0]
        user = prompt.split(">> USER<<", 1)[1].split(">>", 1)[0]
        assert _key(turns[k]["answer"]) in answer
        assert turns[k]["prompt"][:20].replace('"', "") in user.replace('\\"', "")
        for earlier in turns[:k]:  # earlier turns in full
            assert _key(earlier["answer"]) in context
        assert turns[k]["prompt"][:12] in context  # the judged turn's prompt …
        for call in turns[k]["tools"]:  # … and its tool calls
            assert f"Tool call: {call.tool_call.name}" in context
        assert f"Assistant: {_key(turns[k]['answer'])}" not in context  # … not its answer
        for later in turns[k + 1:]:
            assert later["prompt"][:20] not in context


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_session_evaluators_keep_the_whole_session_once(name, mode):
    truth = {"assertions": ["A-scenario"], "turns": []}
    records, judge, turns, _ = _evaluate(name, mode, [{"id": "Builtin.GoalSuccessRate"}],
                                         truth=truth)
    assert len(records) == 1 and records[0]["level"] == "SESSION"
    prompt = _prompt_texts(judge)[0]
    assert all(_key(t["answer"]) in prompt for t in turns if t["answer"].strip())
    assert "A-scenario" in prompt
    assert aggregate_scores(records)[0]["unit"] == "session"


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_tool_call_rows_name_their_trace_span_and_tool(name, mode):
    records, _judge, turns, _ = _evaluate(name, mode, [{"id": "Builtin.ToolSelectionAccuracy"}])
    calls = [(t["trace_id"], call) for t in turns for call in t["tools"]]
    assert len(records) == len(calls) > 0
    for record, (trace_id, call) in zip(records, calls, strict=True):
        assert record["trace_id"] == trace_id
        assert record["tool_name"] == call.tool_call.name
        assert record["span_id"] == call.span_info.span_id.rsplit(":", 1)[-1]
        assert ":" not in record["span_id"]
    agg = aggregate_scores(records)[0]
    assert agg["unit"] == "tool_call" and agg["n"] == len(calls) and agg["count"] == 1


def test_a_single_turn_session_keeps_its_record_id():
    # turn 0 keeps ordinal 0, so a single-turn session's TRACE row id is unchanged
    from app.evaluation.jobs import record_id
    from tests.evaluation.test_final_answer_output import REAL, _real

    provider = LaunchpadSpansProvider(_GroupLogs(REAL["records"]),
                                      [REAL["log_group"], SPANS_LOG_GROUP])
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.Helpfulness"}],
                                          lambda *_a, **_k: FakeJudge())
    records = engine_core.evaluate_session(provider, REAL["session_id"], evaluators, None,
                                           job_id="job-1", sleep=lambda _s: None)
    assert [r["id"] for r in records] == [
        record_id("job-1", REAL["session_id"], "Builtin.Helpfulness", 0)]
    trace = _real(REAL["records"], "both")["trajectory"].traces[0].trace_id
    assert records[0]["trace_id"] == trace


def test_tool_rows_persist_their_span_and_tool_name():
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal

    record = {"id": "rec-tool-1", "session_id": "s-1", "trace_id": "t-1", "span_id": "sp-1",
              "tool_name": "kb_search", "evaluator_id": "Builtin.ToolSelectionAccuracy",
              "level": "TOOL_CALL", "score": 1.0, "label": "Yes", "explanation": "ok",
              "error_type": None, "error_message": None}
    with SessionLocal() as db:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id="run-span",
                              records=[record])
        rows = results_store.read_run_records(db, "run-span")
    assert [(r["trace_id"], r["span_id"], r["tool_name"]) for r in rows] == [
        ("t-1", "sp-1", "kb_search")]


# ── N17-obs373: a failed turn is not scored (Global: SpanStatusErrorException) ──────

PAIPAI = json.loads((FIXTURES / "obs315_paipai_session.json").read_text("utf-8"))


def _paipai(mode: str) -> tuple[LaunchpadSpansProvider, str]:
    groups = ([PAIPAI["log_group"], SPANS_LOG_GROUP] if mode == "both"
              else [PAIPAI["log_group"]])
    return LaunchpadSpansProvider(_GroupLogs(PAIPAI["records"]), groups), PAIPAI["session_id"]


def test_turns_whose_agent_span_failed_are_error_rows_never_judged():
    # 3cdd: turn 1 answered; turns 2–4 ended with invoke_agent status ERROR (a model read
    # timeout after the tools, then two runtime timeouts) — no score for them, no judge call
    provider, sid = _paipai("both")
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.Helpfulness"}],
                                          lambda *_a, **_k: judge)
    records = engine_core.evaluate_session(provider, sid, evaluators, None, job_id="job-1",
                                           sleep=lambda _s: None)
    assert [r["error_type"] for r in records] == [None] + ["span_status_error"] * 3
    assert [r["score"] is None for r in records] == [False, True, True, True]
    assert len(judge.calls) == 1  # only turn 1 reached the judge
    assert len({r["trace_id"] for r in records}) == 4
    agg = aggregate_scores(records)[0]
    assert agg["n"] == 1 and agg["failed_turns"] == 3 and agg["unit"] == "turn"


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_session_evaluators_do_not_score_a_session_with_failed_turns(mode):
    # N17-obs384: 3cdd has 3 failed turns — Global answers SpanStatusErrorException for the
    # session; a SESSION evaluator writes one error row naming them and is never called
    provider, sid = _paipai(mode)
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet(
        [{"id": "Builtin.GoalSuccessRate"}, {"id": "Builtin.TrajectoryAnyOrderMatch"}],
        lambda *_a, **_k: judge)
    records = engine_core.evaluate_session(provider, sid, evaluators,
                                           {"assertions": ["answers the station question"],
                                            "expected_trajectory": ["x"]},
                                           job_id="job-1", sleep=lambda _s: None)
    assert [(r["evaluator_id"], r["level"], r["error_type"]) for r in records] == [
        ("Builtin.GoalSuccessRate", "SESSION", "span_status_error"),
        ("Builtin.TrajectoryAnyOrderMatch", "SESSION", "span_status_error")]
    assert judge.calls == []
    assert "3 of its turns failed" in records[0]["error_message"]
    for trace in [t["trace_id"] for t in _turns(provider, sid)[1:]]:
        assert trace in records[0]["error_message"]
    assert aggregate_scores(records) == []


def test_session_evaluators_score_a_session_without_failed_turns():
    records, judge, _turns_, _ = _evaluate("wb007", "both", [{"id": "Builtin.GoalSuccessRate"}],
                                           truth={"assertions": ["A"], "turns": []})
    assert [r["error_type"] for r in records] == [None] and len(judge.calls) == 1


def test_without_span_records_a_turn_the_agent_never_returned_from_is_failed():
    # N17-obs382: runtime_logs mode has no span status — a turn whose records hold no
    # invoke_agent result (no plain-text gen_ai.choice) failed; the same turns as "both"
    provider, sid = _paipai("runtime_only")
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.Helpfulness"}],
                                          lambda *_a, **_k: judge)
    records = engine_core.evaluate_session(provider, sid, evaluators, None, job_id="job-1",
                                           sleep=lambda _s: None)
    assert [r["error_type"] for r in records] == [None] + ["span_status_error"] * 3
    assert len(judge.calls) == 1
    from strands_evals.types.trace import AgentInvocationSpan

    agents = [s for t in provider.get_evaluation_data(sid)["trajectory"].traces
              for s in t.spans if isinstance(s, AgentInvocationSpan)]
    assert [(a.metadata or {}).get("span_status_source") for a in agents] == [
        None, "runtime_logs", "runtime_logs", "runtime_logs"]


# ── per-turn rows carry their turn's extraction reconciliation (obs324 × item 1) ──────


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_each_turn_row_carries_its_turns_extraction_status(mode):
    from app.services.strands_eval import extraction_check

    provider, sid = _provider("wb007", mode)
    answers = [t["answer"] for t in _turns(provider, sid)]
    wrong = list(answers)
    wrong[2] = "not what the agent said"
    reference = extraction_check.reference(extraction_check.SOURCE_REPLAY, wrong[:4])
    checks: dict[str, Any] = {}
    evaluators = engine_core.EvaluatorSet(
        [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.GoalSuccessRate"}],
        lambda *_a, **_k: FakeJudge())
    records = engine_core.evaluate_session(provider, sid, evaluators, None, job_id="job-1",
                                           sleep=lambda _s: None, reference=reference,
                                           on_check=lambda s, c: checks.update({s: c}))
    trace_rows = [r for r in records if r["level"] == "TRACE"]
    assert [r["extraction_status"] for r in trace_rows] == [
        "ok", "ok", "mismatch", "ok", "unverifiable"]
    session_row = next(r for r in records if r["level"] == "SESSION")
    assert session_row["extraction_status"] == checks[sid]["status"] == "mismatch"


def test_extraction_status_persists_on_the_row():
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal

    record = {"id": "rec-x-1", "session_id": "s-1", "trace_id": "t-1",
              "evaluator_id": "Builtin.Helpfulness", "level": "TRACE", "score": 1.0,
              "label": "Yes", "explanation": "ok", "error_type": None, "error_message": None,
              "extraction_status": "mismatch"}
    with SessionLocal() as db:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id="run-xs",
                              records=[record])
        rows = results_store.read_run_records(db, "run-xs")
    assert [r["extraction_status"] for r in rows] == ["mismatch"]


def test_turn_expected_response_is_the_expected_output_correctness_reads():
    # obs326 (r17-bh, ExpectedResponseCorrectnessEvaluator) judges against expected_output:
    # the per-turn case must carry turn k's own reference there
    provider, sid = _provider("wb007", "both")
    turns = _turns(provider, sid)
    scenario = {"scenario_id": "mt", "turns": [
        {"input": t["prompt"], "expected_response": f"REF-{k}"} for k, t in enumerate(turns)]}
    truth = engine_core.ground_truth_from_scenarios([scenario], [sid])[sid]
    seen: list[tuple[str, str | None]] = []

    class ReadsExpected:
        name = "Builtin.Correctness"

        def evaluate(self, case):
            from strands_evals.types.evaluation import EvaluationOutput

            seen.append((case.actual_output, case.expected_output))
            return [EvaluationOutput(score=1.0, test_pass=True, label="ok", reason="r")]

    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.Correctness"}],
                                          lambda *_a, **_k: FakeJudge())
    evaluators._built["Builtin.Correctness"] = ReadsExpected()
    engine_core.evaluate_session(provider, sid, evaluators, truth, job_id="job-1",
                                 sleep=lambda _s: None)
    assert seen == [(t["answer"], f"REF-{k}") for k, t in enumerate(turns)]


# ── N17-obs383: a tool call inside a failed turn is not scored ───────────────────────


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_tool_calls_inside_a_failed_turn_are_error_rows(mode):
    # 3cdd: turn 1 (ok) ran run_sql + list_stations; turn 2 (agent span ERROR) ran run_sql
    # twice — Global answers SpanStatusErrorException for those two calls
    provider, sid = _paipai(mode)
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.ToolSelectionAccuracy"}],
                                          lambda *_a, **_k: FakeJudge())
    records = engine_core.evaluate_session(provider, sid, evaluators, None, job_id="job-1",
                                           sleep=lambda _s: None)
    turns = _turns(provider, sid)
    assert [r["trace_id"] for r in records] == [turns[0]["trace_id"]] * 2 + [
        turns[1]["trace_id"]] * 2
    assert [r["error_type"] for r in records] == [None, None, "span_status_error",
                                                  "span_status_error"]
    assert all(r["tool_name"] and r["span_id"] for r in records)
    assert len({r["id"] for r in records}) == 4
    agg = aggregate_scores(records)[0]
    assert agg["n"] == 2 and agg["failed_turns"] == 2 and agg["unit"] == "tool_call"


def test_a_session_whose_every_call_is_in_failed_turns_never_reaches_the_judge():
    provider, sid = _paipai("both")
    data = provider.get_evaluation_data(sid)
    case = engine_core.evaluation_data(sid, data, None)
    turns = engine_core.turn_cases(case)
    # drop turn 1 (the only ok turn with calls): every remaining call is in a failed turn
    case.actual_trajectory.traces = case.actual_trajectory.traces[1:]
    turns = turns[1:]
    [(_unit_case, stamp)] = engine_core.scoring_units("TOOL_CALL", case, turns, None)
    judge_calls: list[Any] = []
    records = engine_core.unit_records(lambda c: judge_calls.append(c) or [], case, stamp,
                                       job_id="j", session_id=sid,
                                       evaluator_id="Builtin.ToolSelectionAccuracy",
                                       level="TOOL_CALL")
    assert judge_calls == []
    assert [r["error_type"] for r in records] == ["span_status_error"] * 2


# ── N17-obs332: custom TOOL_CALL judges run once per call ────────────────────────────

CUSTOM_TOOL = {"id": "cust-tool", "definition": "judge", "level": "TOOL_CALL",
               "instructions": "CONTEXT<<{context}>> CALL<<{tool_turn}>> USER<<{user_message}>>",
               "rating_scale": [{"label": "pass", "value": 1.0}, {"label": "fail", "value": 0.0}]}


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_custom_tool_judge_runs_per_call_with_context_up_to_the_call(mode):
    records, judge, turns, _ = _evaluate("wb006", mode, [CUSTOM_TOOL],
                                         judge=FakeJudge(overrides={"label": "pass"}))
    assert all(r["error_type"] is None for r in records)
    calls = [(k, j, t["trace_id"], call) for k, t in enumerate(turns)
             for j, call in enumerate(t["tools"])]
    assert len(records) == len(calls) == len(judge.calls) > 3
    prompts = _prompt_texts(judge)
    for record, prompt, (k, j, trace_id, call) in zip(records, prompts, calls, strict=True):
        assert record["trace_id"] == trace_id and record["tool_name"] == call.tool_call.name
        assert record["span_id"] == call.span_info.span_id.rsplit(":", 1)[-1]
        context = prompt.split("CONTEXT<<", 1)[1].split(">> CALL<<", 1)[0]
        tool_turn = prompt.split(">> CALL<<", 1)[1].split(">> USER<<", 1)[0]
        assert tool_turn.startswith(call.tool_call.name)
        # the turn's earlier calls are in the context, the judged one and later ones are not
        assert context.count("Tool call: ") == sum(len(t["tools"]) for t in turns[:k]) + j
        assert turns[k]["prompt"][:12] in context
        assert f"Assistant: {_key(turns[k]['answer'])}" not in context
        for earlier in turns[:k]:
            assert _key(earlier["answer"]) in context
        for later in turns[k + 1:]:
            assert later["prompt"][:20] not in context
    assert len({r["id"] for r in records}) == len(records)
    assert aggregate_scores(records)[0]["n"] == len(calls)


def test_custom_tool_judge_reading_skill_placeholders_judges_skill_calls_only():
    from tests.evaluation import corpus_support as cs

    corpus = cs.load_session("7574fd2ea83d")
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]),
                                      [corpus["log_group"], SPANS_LOG_GROUP])
    judge = FakeJudge()
    definition = {**CUSTOM_TOOL, "id": "cust-skill",
                  "instructions": "SKILL<<{invoked_skill}>> BODY<<{skill_content}>> "
                                  "CALL<<{tool_turn}>>"}
    evaluators = engine_core.EvaluatorSet([definition], lambda *_a, **_k: judge)
    records = engine_core.evaluate_session(provider, corpus["session_id"], evaluators, None,
                                           job_id="job-1", sleep=lambda _s: None)
    assert len(records) == 1 == len(judge.calls)  # 5 calls, 1 of them the skill load
    prompt = _prompt_texts(judge)[0]
    assert "SKILL<<expense-report-writer>>" in prompt
    assert "BODY<<(not available)>>" not in prompt


def test_custom_tool_judge_on_a_failed_turn_call_is_an_error_row():
    provider, sid = _paipai("both")
    judge = FakeJudge(overrides={"label": "pass"})
    evaluators = engine_core.EvaluatorSet([CUSTOM_TOOL], lambda *_a, **_k: judge)
    records = engine_core.evaluate_session(provider, sid, evaluators, None, job_id="job-1",
                                           sleep=lambda _s: None)
    assert [r["error_type"] for r in records] == [None, None, "span_status_error",
                                                  "span_status_error"]
    assert len(judge.calls) == 2


def test_the_results_api_row_names_the_turn_call_reconciliation_and_judge():
    # N17-obs317: every result row exposes trace_id / span_id / tool_name / extraction_status
    # and the judge under the run-level names (`judge` stays their alias)
    judge = {"model_id": "global.zai.glm-5.3", "temperature": 0.0, "temperature_applied": True}
    row = results_store.normalize_result_record({
        "evaluator_id": "Builtin.ToolSelectionAccuracy", "level": "TOOL_CALL", "score": 1.0,
        "trace_id": "t-1", "span_id": "sp-1", "tool_name": "kb_search",
        "extraction_status": "ok", "judge": judge})
    assert (row["trace_id"], row["span_id"], row["tool_name"], row["extraction_status"]) == (
        "t-1", "sp-1", "kb_search", "ok")
    assert row["judge_model_id"] == "global.zai.glm-5.3"
    assert row["judge_inference"] == row["judge"] == judge


# ── N17-obs323: a retried scenario scores only its last attempt ───────────────────────


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_a_retried_scenario_scores_only_the_last_attempts_traces(mode):
    # f0469605c5f0: attempt 1 died on internalServerException after its tool call, attempt 2
    # answered — same pre-assigned session id, so the spans hold both "turns"
    from datetime import UTC

    from tests.evaluation import corpus_support as cs

    corpus = cs.load_session("f0469605c5f0")
    groups = ([corpus["log_group"], SPANS_LOG_GROUP] if mode == "both"
              else [corpus["log_group"]])
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]), groups)
    sid = corpus["session_id"]
    full = provider.get_evaluation_data(sid)
    assert len(full["trajectory"].traces) == 2
    second = full["trajectory"].traces[1]
    start = min(s.span_info.start_time for s in second.spans)
    cutoff_ms = int(start.replace(tzinfo=start.tzinfo or UTC).timestamp() * 1000)
    trimmed = engine_core.since_attempt(full, cutoff_ms)
    assert [t.trace_id for t in trimmed["trajectory"].traces] == [second.trace_id]
    assert trimmed["output"] == full["output"]
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.Helpfulness"}],
                                          lambda *_a, **_k: judge)
    records = engine_core.evaluate_sessions(provider, [sid], evaluators, None, job_id="j",
                                            sleep=lambda _s: None,
                                            not_before={sid: cutoff_ms})
    assert [r["trace_id"] for r in records] == [second.trace_id] and len(judge.calls) == 1
    assert engine_core.since_attempt(full, None) is full
