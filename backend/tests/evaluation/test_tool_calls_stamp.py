"""N17-obs552 — every result record names the tool calls of the unit it judged.

The result drawer's 工具调用 field read ``tool_name`` only, which is stamped on TOOL_CALL rows
(N17-obs324): a TRACE / SESSION row showed ``—`` while the same unit's trajectory carried
``search_tickets`` / ``get_ticket`` spans. The engine now stamps ``tool_calls``
(``[{name, count}]``, ``engine_core.tool_call_counts``) on every stamp ``scoring_units``
yields, ``unit_records`` carries it onto judged and error records alike, the hub ingests it
(``eval_results.tool_calls``) and ``normalize_result_record`` exposes it (additive).
"""

from __future__ import annotations

from datetime import UTC, datetime

from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import (
    AgentInvocationSpan,
    Session,
    SpanInfo,
    ToolCall,
    ToolExecutionSpan,
    ToolResult,
    Trace,
)

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import results_store
from app.services.strands_eval import engine_core

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def _info(trace_id: str, span_id: str) -> SpanInfo:
    return SpanInfo(session_id="s-1", span_id=span_id, trace_id=trace_id,
                    start_time=NOW, end_time=NOW)


def _tool(trace_id: str, span_id: str, name: str) -> ToolExecutionSpan:
    return ToolExecutionSpan(span_info=_info(trace_id, span_id),
                             tool_call=ToolCall(name=name, arguments={"q": "INC-1001"}),
                             tool_result=ToolResult(content="ok"))


def _agent(trace_id: str, prompt: str, answer: str) -> AgentInvocationSpan:
    return AgentInvocationSpan(span_info=_info(trace_id, f"{trace_id}-agent"),
                               user_prompt=prompt, agent_response=answer, available_tools=[])


def _case() -> EvaluationData:
    session = Session(session_id="s-1", traces=[
        Trace(trace_id="t-1", session_id="s-1", spans=[
            _tool("t-1", "sp-1", "search_tickets"), _tool("t-1", "sp-2", "search_tickets"),
            _tool("t-1", "sp-3", "get_ticket"), _agent("t-1", "查 INC-1001", "找到了"),
        ]),
        Trace(trace_id="t-2", session_id="s-1", spans=[_agent("t-2", "谢谢", "不客气")]),
    ])
    return EvaluationData(input="查 INC-1001", actual_output="不客气", actual_trajectory=session)


T1_CALLS = [{"name": "search_tickets", "count": 2}, {"name": "get_ticket", "count": 1}]


def test_tool_call_counts_lists_the_turn_or_the_session_in_first_seen_order():
    session = _case().actual_trajectory
    assert engine_core.tool_call_counts(session, "t-1") == T1_CALLS
    assert engine_core.tool_call_counts(session, "t-2") == []
    assert engine_core.tool_call_counts(session) == T1_CALLS
    assert engine_core.tool_call_counts(None) is None  # no strands Session: nothing to list


def test_trace_and_session_stamps_carry_the_unit_s_tool_calls():
    case = _case()
    turns = engine_core.turn_cases(case)
    trace_stamps = [stamp for _c, stamp in engine_core.scoring_units("TRACE", case, turns, "t-1")]
    assert [s["tool_calls"] for s in trace_stamps] == [T1_CALLS, []]
    (_c, session_stamp), = engine_core.scoring_units("SESSION", case, turns, "t-1")
    assert session_stamp["tool_calls"] == T1_CALLS
    tool_stamps = [s for _c, s in engine_core.scoring_units("TOOL_CALL", case, turns, "t-1")]
    assert tool_stamps and all(s["tool_calls"] == T1_CALLS for s in tool_stamps)


def test_unit_records_stamp_judged_and_error_rows_alike():
    case = _case()
    turns = engine_core.turn_cases(case)
    units = engine_core.scoring_units("TRACE", case, turns, "t-1")
    judged = [
        rec for unit_case, stamp in units
        for rec in engine_core.unit_records(
            lambda _c: [EvaluationOutput(score=1.0, label="Yes", reason="ok")],
            unit_case, stamp, job_id="j-1", session_id="s-1",
            evaluator_id="Builtin.Helpfulness", level="TRACE")
    ]
    assert [(r["trace_id"], r["tool_calls"]) for r in judged] == [("t-1", T1_CALLS), ("t-2", [])]

    def boom(_case):
        raise RuntimeError("judge down")

    unit_case, stamp = units[0]
    (err,) = engine_core.unit_records(boom, unit_case, stamp, job_id="j-1", session_id="s-1",
                                      evaluator_id="Builtin.Helpfulness", level="TRACE")
    assert err["error_type"] == "RuntimeError" and err["tool_calls"] == T1_CALLS


def test_ingest_keeps_tool_calls_and_the_result_row_exposes_them():
    case = _case()
    turns = engine_core.turn_cases(case)
    records = [
        rec for unit_case, stamp in engine_core.scoring_units("TRACE", case, turns, "t-1")
        for rec in engine_core.unit_records(
            lambda _c: [EvaluationOutput(score=1.0, label="Yes", reason="ok")],
            unit_case, stamp, job_id="run-tc1", session_id="s-1",
            evaluator_id="Builtin.Helpfulness", level="TRACE")
    ]
    # a row from a worker stamped before the field: the key is absent, the column stays NULL
    records.append({**records[0], "id": "older-row", "trace_id": "t-9"})
    del records[-1]["tool_calls"]
    db = SessionLocal()
    try:
        assert results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run",
                                     owner_id="run-tc1", records=records) == 3
        stored = {r["trace_id"]: r for r in results_store.read_run_records(db, "run-tc1")}
    finally:
        db.close()
    assert stored["t-1"]["tool_calls"] == T1_CALLS
    assert stored["t-2"]["tool_calls"] == []
    assert stored["t-9"]["tool_calls"] is None
    row = results_store.normalize_result_record(stored["t-1"])
    assert row["tool_calls"] == T1_CALLS and row["tool_name"] is None
