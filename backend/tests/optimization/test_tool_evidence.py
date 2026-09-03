"""P1b — tool-description evidence and the two-component gepa_lite reflection:
content-log tool turns, span fallback, mentions, validation, per-type key
ownership and the router's widened source rule."""

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

import app.optimization.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.optimization.models import Experiment
from app.optimization.providers import evidence as ev
from app.optimization.providers import gepa_lite
from app.optimization.providers.base import (
    EvaluatorRecord,
    EvidenceStats,
    OptimizeRequest,
    SessionEvidence,
)
from app.services import observability as ob
from tests.conftest import ws_ctx

WS = ws_ctx()


# ─── helpers ─────────────────────────────────────────────────────────────────
def _content_record(ts: int, trace: str, msgs: list[tuple[str, str, list[dict]]]) -> dict:
    """A content-log record: msgs = [(kind, role, parts)]."""
    body: dict[str, Any] = {"input": {"messages": []}, "output": {"messages": []}}
    for kind, role, parts in msgs:
        body[kind]["messages"].append({"role": role, "content": json.dumps(parts)})
    return {"timeUnixNano": ts, "traceId": trace, "attributes": {"session.id": "s1"},
            "body": body}


class FakeFilterLogs:
    def __init__(self, records: list[dict]):
        self.records = records

    def filter_log_events(self, **kw):
        return {"events": [{"message": json.dumps(r)} for r in self.records]}


def _use(tid, name, inp):
    return {"toolUse": {"toolUseId": tid, "name": name, "input": inp}}


def _res(tid, text, status="success"):
    return {"toolResult": {"toolUseId": tid, "status": status, "content": [{"text": text}]}}


def _record(sid, evaluator, score, explanation="why", level="Session"):
    attrs = {"gen_ai.evaluation.name": evaluator, "session.id": sid,
             "aws.bedrock_agentcore.evaluation_level": level,
             "gen_ai.evaluation.score.value": score, "gen_ai.evaluation.explanation": explanation}
    return {"message": json.dumps({"name": "gen_ai.evaluation.result", "attributes": attrs})}


class FakeLogs:
    def __init__(self, events):
        self.events = events

    def get_log_events(self, **kw):
        if kw.get("nextToken"):
            return {"events": [], "nextForwardToken": kw["nextToken"]}
        return {"events": self.events, "nextForwardToken": "end"}


def _req(evidence, stats=None, components=("system_prompt", "tool_descriptions"),
         tools=None, max_chars=8000):
    stats = stats or EvidenceStats(
        sessions_scored=len(evidence), sessions_selected=len(evidence),
        sessions_with_transcript=len(evidence), records=len(evidence),
        sessions_with_tool_calls=sum(
            1 for e in evidence if any(t.get("role") == "tool_call" for t in e.turns)),
        tool_calls_seen=sum(
            1 for e in evidence for t in e.turns if t.get("role") == "tool_call"),
    )
    return OptimizeRequest(
        current_prompt="You are an HR helper.", agent={"id": "a1"}, trace_source={},
        evidence=evidence, stats=stats, model_id="m", workspace=WS, max_chars=max_chars,
        components=components,
        tools=tools if tools is not None else {"get_pto_balance": "PTO balance lookup",
                                                "lookup_hr_policy": "Policy lookup"},
    )


def _session(sid="s1", score=0.2, calls=(("t1", "get_pto_balance", {"employee_id": "E1"}),)):
    turns: list[dict[str, Any]] = [{"role": "user", "text": "How much PTO do I have?"}]
    for tid, name, inp in calls:
        turns.append({"role": "tool_call", "id": tid, "name": name,
                      "input": json.dumps(inp)})
        turns.append({"role": "tool_result", "id": tid, "name": name, "status": "success",
                      "text": "12 days"})
    turns.append({"role": "assistant", "text": "You have 12 days."})
    return SessionEvidence(
        session_id=sid, turns=turns, mean_score=score,
        records=[EvaluatorRecord("Builtin.ToolSelectionAccuracy", score, "No",
                                 "The agent called `get_pto_balance` although the user "
                                 "asked about policy.", level="Span")],
    )


def _converse(replies: list[str]):
    calls: list[dict[str, Any]] = []
    it = iter(replies)

    def fn(model_id, system, messages, max_tokens):
        calls.append({"system": system, "user": messages[0]["text"]})
        return next(it), {"input_tokens": 1, "output_tokens": 1}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ─── content-log tool turns ──────────────────────────────────────────────────
def test_tool_turns_dedupe_order_and_result_join():
    recs = [
        _content_record(1, "tr1", [("output", "assistant",
                                    [_use("a", "get_pto_balance", {"e": 1})])]),
        # history repeats the call; the tool result arrives as input/tool AND echoed on output
        _content_record(2, "tr1", [
            ("input", "user", [{"text": "hi"}]),
            ("input", "tool", [_res("a", "12 days")]),
            ("output", "assistant", [_use("a", "get_pto_balance", {"e": 1}),
                                     _res("a", "12 days (echo)"),
                                     _use("b", "lookup_hr_policy", {"topic": "pto"})]),
        ]),
        _content_record(3, "tr1", [("input", "tool", [_res("b", "policy text", status="error")])]),
    ]
    turns = ob.eval_tool_turns_from_content_logs("/g", "s1", None, WS, logs=FakeFilterLogs(recs))
    roles = [(t["role"], t["id"]) for t in turns]
    assert roles == [("TOOL_CALL", "a"), ("TOOL_RESULT", "a"),
                     ("TOOL_CALL", "b"), ("TOOL_RESULT", "b")]
    assert turns[0]["name"] == "get_pto_balance" and turns[0]["input"] == {"e": 1}
    assert turns[1]["text"] == "12 days"  # the input/tool copy wins over the echo
    assert turns[3]["status"] == "error" and turns[3]["name"] == "lookup_hr_policy"
    assert turns[0]["trace_id"] == "tr1"


def test_tool_turns_ignore_plain_string_content_and_bad_json():
    recs = [{"timeUnixNano": 1, "traceId": "t", "attributes": {"session.id": "s1"},
             "body": {"input": {"messages": [{"role": "tool", "content": "plain"}]},
                      "output": {"messages": [{"role": "assistant", "content": "[not json"}]}}}]
    turns = ob.eval_tool_turns_from_content_logs("/g", "s1", None, WS, logs=FakeFilterLogs(recs))
    assert turns == []


def test_text_turns_unchanged_by_refactor():
    recs = [_content_record(1, "tr1", [("input", "user", [{"text": "q"}]),
                                        ("output", "assistant", [_use("a", "x", {})])]),
            {"timeUnixNano": 2, "traceId": "tr1", "attributes": {"session.id": "s1"},
             "body": {"output": {"messages": [{"role": "assistant",
                                              "content": {"message": "final",
                                                          "finish_reason": "end_turn"}}]}}}]
    turns = ob.eval_turns_from_content_logs("/g", "s1", None, WS, logs=FakeFilterLogs(recs))
    assert [(t["role"], t["text"]) for t in turns] == [("USER", "q"), ("ASSISTANT", "final")]


# ─── evidence merge / cap / spans / mentions ─────────────────────────────────
def test_collect_evidence_merges_tool_turns_by_time_and_caps():
    logs = FakeLogs([_record("s1", "Builtin.Helpfulness", 0.5)])
    text = lambda sid: [{"role": "USER", "text": "q", "at": "2026-01-01T00:00:00"},  # noqa: E731
                        {"role": "ASSISTANT", "text": "a", "at": "2026-01-01T00:00:09"}]
    raw = []
    for i in range(25):
        raw.append({"role": "TOOL_CALL", "id": f"c{i}", "name": "get_pto_balance",
                    "input": {"i": i, "blob": "x" * 900}, "at": f"2026-01-01T00:00:0{min(i, 8)}"})
        raw.append({"role": "TOOL_RESULT", "id": f"c{i}", "name": "get_pto_balance",
                    "status": "success", "text": "ok", "at": f"2026-01-01T00:00:0{min(i, 8)}"})
    evidence, stats = ev.collect_evidence(
        workspace=WS, log_group="/g", log_stream="run-x", transcript=text, max_sessions=30,
        logs=logs, tool_turns=lambda sid: raw, tool_spans=lambda sids: {},
    )
    turns = evidence[0].turns
    assert turns[0]["role"] == "user"
    # the omission marker trails everything; the last real turn is the assistant
    assert turns[-1]["role"] == "note" and turns[-2]["role"] == "assistant"
    calls = [t for t in turns if t["role"] == "tool_call"]
    assert len(calls) == ev.MAX_TOOL_TURNS
    assert "omitted" in calls[0]["input"]  # per-call input truncation
    assert any(t["role"] == "note" and "5 more tool calls omitted" in t["text"] for t in turns)
    assert stats.sessions_with_tool_calls == 1 and stats.tool_calls_seen == ev.MAX_TOOL_TURNS
    assert stats.observed_tools["get_pto_balance"]["calls"] == ev.MAX_TOOL_TURNS


def test_collect_evidence_span_fallback_and_observed_tools():
    logs = FakeLogs([_record("s1", "e", 0.1), _record("s2", "e", 0.9)])
    asked: list[list[str]] = []

    def spans(sids):
        asked.append(sids)
        return {"s1": [{"id": "c1", "name": "kb_search", "status": "success",
                        "description": "Search the KB"},
                       {"id": "c2", "name": "kb_search", "status": "error", "description": None}],
                "s2": [{"id": "c3", "name": "skills", "status": "success",
                        "description": "Skills"}]}

    evidence, stats = ev.collect_evidence(
        workspace=WS, log_group="/g", log_stream="run-x", transcript=lambda sid: [],
        max_sessions=30, logs=logs,
        tool_turns=lambda sid: [] if sid == "s1" else [
            {"role": "TOOL_CALL", "id": "c3", "name": "skills", "input": {}}],
        tool_spans=spans,
    )
    assert asked == [["s1", "s2"]]  # one query for every selected session
    s1 = next(e for e in evidence if e.session_id == "s1")
    assert [t["source"] for t in s1.turns] == ["span", "span"]  # content logs had none
    assert s1.turns[0]["input"] is None
    s2 = next(e for e in evidence if e.session_id == "s2")
    assert s2.turns[0].get("source") is None  # content-log turn preferred
    assert stats.observed_tools["kb_search"] == {"calls": 2, "errors": 1,
                                                 "description_seen": "Search the KB"}
    assert stats.observed_tools["skills"]["calls"] == 1
    assert stats.sessions_with_tool_calls == 2 and stats.tool_calls_seen == 3


def test_collect_evidence_tool_helpers_fail_soft():
    logs = FakeLogs([_record("s1", "e", 0.1)])

    def boom(*_a):
        raise RuntimeError("cw down")

    evidence, stats = ev.collect_evidence(
        workspace=WS, log_group="/g", log_stream="run-x", transcript=lambda sid: [],
        max_sessions=30, logs=logs, tool_turns=boom, tool_spans=boom,
    )
    assert evidence[0].turns == [] and stats.tool_calls_seen == 0


def test_tool_mentions_word_boundaries():
    names = ["get_pto_balance", "lookup_hr_policy", "pto"]
    text = "The agent called `get_pto_balance` (not lookup_hr_policy_v2) for the PTO question."
    assert ev.tool_mentions(text, names) == ["get_pto_balance"]
    assert ev.tool_mentions(None, names) == []
    assert ev.tool_mentions("used lookup_hr_policy.", names) == ["lookup_hr_policy"]


def test_tool_spans_query_shape():
    q = ev.tool_spans_query(["s1", "s2"])
    assert 'attributes.session.id in ["s1", "s2"]' in q
    assert "gen_ai.tool.description" in q and "SOURCE logGroups" in q


# ─── rendering ───────────────────────────────────────────────────────────────
def test_reflective_dataset_renders_tool_calls_unused_and_mentions():
    text = gepa_lite.render_reflective_dataset(
        [_session()], own_tools=["get_pto_balance", "lookup_hr_policy"], with_tools=True,
    )
    assert "Tool calls:" in text
    assert '1. get_pto_balance({"employee_id": "E1"}) → success: 12 days' in text
    assert "Owned tools NOT called: lookup_hr_policy" in text
    assert "[tool-call judgement; mentions: get_pto_balance]" in text
    plain = gepa_lite.render_reflective_dataset([_session()])
    assert "Tool calls:" not in plain and "mentions" not in plain


def test_tools_header_lists_owned_and_context_tools():
    req = _req([_session()])
    req.stats.observed_tools = {
        "get_pto_balance": {"calls": 3, "errors": 1, "description_seen": "seen"},
        "kb_search": {"calls": 2, "errors": 0, "description_seen": "Search the KB"},
    }
    body = gepa_lite.build_user_message(req)
    assert "## Tools the agent owns" in body
    assert "`get_pto_balance` · called 3×, 1 errors: PTO balance lookup" in body
    assert "`lookup_hr_policy` · never called: Policy lookup" in body
    assert "## Other tools seen in traces (context only, NOT editable)" in body
    assert "`kb_search` · called 2×: Search the KB" in body
    prompt_only = gepa_lite.build_user_message(_req([_session()], components=("system_prompt",)))
    assert "Tools the agent owns" not in prompt_only


# ─── validation ──────────────────────────────────────────────────────────────
def test_validate_tool_descriptions_matrix():
    current = {"a": "Alpha desc", "b": "Beta desc"}
    kept, dropped = gepa_lite.validate_tool_descriptions(
        {"a": "Alpha, improved", "b": " Beta desc ", "zzz": "unknown", "c": "",
         "a2": "x" * 2000}, current)
    assert kept == {"a": "Alpha, improved"}
    # "c" and "a2" are not owned tools either — unknown is checked first
    assert dropped == {"unknown": 3, "empty": 0, "too_long": 0, "unchanged": 1}
    kept, dropped = gepa_lite.validate_tool_descriptions({"a": "y" * 1025, "b": None}, current)
    assert kept == {} and dropped["too_long"] == 1 and dropped["empty"] == 1
    assert gepa_lite.validate_tool_descriptions("nope", current) == ({}, {
        "unknown": 0, "empty": 0, "too_long": 0, "unchanged": 0})


# ─── provider outcomes ───────────────────────────────────────────────────────
def test_both_components_one_call():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([json.dumps({
        "diagnosis": "d", "changes": ["c"], "revised_prompt": "P2",
        "tool_descriptions": {"get_pto_balance": "Return the PTO balance for an employee id.",
                              "unknown_tool": "x", "lookup_hr_policy": "Policy lookup"},
        "tool_changes": ["clarify when to call get_pto_balance"],
    })])
    res = prov.optimize(_req([_session()]), svc._noop, converse=converse)
    assert len(converse.calls) == 1
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert res.tool_status == "COMPLETED"
    assert res.tool_descriptions == {
        "get_pto_balance": "Return the PTO balance for an employee id."}
    assert res.meta["tool_descriptions_proposed"] == 3
    assert res.meta["tool_descriptions_dropped"] == {"unknown": 1, "empty": 0,
                                                     "too_long": 0, "unchanged": 1}
    assert res.tool_explanation == "- clarify when to call get_pto_balance"
    assert "covers BOTH the instruction AND" in converse.calls[0]["system"]
    assert "Tools the agent owns" in converse.calls[0]["user"]


def test_tools_only_freezes_the_prompt():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([json.dumps({"revised_prompt": "ignored",
                                      "tool_descriptions": {"get_pto_balance": "new"}})])
    res = prov.optimize(_req([_session()], components=("tool_descriptions",)),
                        svc._noop, converse=converse)
    assert res.recommended_prompt is None and res.status == "FAILED"
    assert "frozen" in (res.error or "")
    assert res.tool_status == "COMPLETED" and res.tool_descriptions == {"get_pto_balance": "new"}
    assert "INSTRUCTION IS FROZEN" in converse.calls[0]["system"]


def test_tools_empty_result_is_completed_not_failed():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([json.dumps({"revised_prompt": "P2", "tool_descriptions": {}})])
    res = prov.optimize(_req([_session()]), svc._noop, converse=converse)
    assert res.tool_status == "COMPLETED" and res.tool_descriptions == {}


def test_no_tool_calls_settles_tool_side_and_prompt_proceeds():
    prov = gepa_lite.GepaLiteProvider()
    sess = _session(calls=())
    converse = _converse([json.dumps({"revised_prompt": "P2"})])
    res = prov.optimize(_req([sess]), svc._noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert res.tool_status == "no-tool-calls" and "no tool calls" in res.tool_error
    assert res.tool_descriptions == {}
    # the model was asked for the instruction only
    assert "INSTRUCTION ONLY" in converse.calls[0]["system"]
    # tools-only with no calls → no model call at all
    converse = _converse([])
    res = prov.optimize(_req([sess], components=("tool_descriptions",)), svc._noop,
                        converse=converse)
    assert res.status == "FAILED" and res.tool_status == "no-tool-calls"
    assert converse.calls == []


def test_no_discovered_tools_is_no_tools():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([json.dumps({"revised_prompt": "P2"})])
    res = prov.optimize(_req([_session()], tools={}), svc._noop, converse=converse)
    assert res.tool_status == "no-tools" and res.recommended_prompt == "P2"


def test_client_error_fails_both_components():
    prov = gepa_lite.GepaLiteProvider()

    def converse(*_a):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                          "Converse")

    res = prov.optimize(_req([_session()]), svc._noop, converse=converse)
    assert res.status == "FAILED" and res.error == "AccessDeniedException: nope"
    assert res.tool_status == "error" and res.tool_error == res.error
    assert res.recommended_prompt is None and res.tool_descriptions == {}


# ─── service seam ────────────────────────────────────────────────────────────
def _mk_run(**kw) -> str:
    db = SessionLocal()
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a1", agent_name="agent",
                  status="completed", batch_eval_id="be-1", **kw)
    db.add(run)
    db.commit()
    rid = run.id
    db.close()
    return rid


def test_service_writes_both_components_with_attribution():
    rid = _mk_run()
    logs = FakeLogs([_record("s1", "Builtin.ToolSelectionAccuracy", 0.0, level="Span"),
                     _record("s1", "Builtin.Helpfulness", 0.5, level="Trace")])
    converse = _converse([json.dumps({
        "diagnosis": "d", "revised_prompt": "P2",
        "tool_descriptions": {"get_pto_balance": "better"}, "tool_changes": ["x"]})])
    out = svc._third_party_prompt_recommendation(
        "e1", {"id": "a1", "system_prompt": "cur"}, WS, svc._noop,
        provider_id="gepa_lite", model_id="m",
        source={"run_id": rid, "results_log_group": "/g", "results_log_stream": "run-x"},
        transcript=lambda sid: [{"role": "user", "text": "q"}], logs=logs, converse=converse,
        components=("system_prompt", "tool_descriptions"),
        tools={"get_pto_balance": "old"},
        tool_turns=lambda sid: [{"role": "TOOL_CALL", "id": "c", "name": "get_pto_balance",
                                 "input": {"e": 1}}],
        tool_spans=lambda sids: {},
    )
    assert out["system_prompt_status"] == "COMPLETED" and out["recommended_prompt"] == "P2"
    assert out["provider"] == "gepa_lite" and out["tool_provider"] == "gepa_lite"
    assert out["tool_provider_model_id"] == "m"
    assert out["tool_status"] == "COMPLETED"
    assert out["tool_descriptions"] == {"get_pto_balance": "better"}
    assert out["analyzed_tools"] == {"get_pto_balance": "old"}
    assert out["tool_explanation"] == "- x"
    assert out["tool_provider_meta"]["tool_calls_seen"] == 1
    assert "changes" not in out["tool_provider_meta"]  # prompt meta stays with the prompt


def test_service_tools_only_leaves_prompt_keys_alone():
    rid = _mk_run()
    logs = FakeLogs([_record("s1", "e", 0.0, level="Span")])
    converse = _converse([json.dumps({"tool_descriptions": {"t": "new"}})])
    out = svc._third_party_prompt_recommendation(
        "e1", {"id": "a1", "system_prompt": "cur"}, WS, svc._noop,
        provider_id="gepa_lite", model_id="m",
        source={"run_id": rid, "results_log_group": "/g", "results_log_stream": "run-x"},
        transcript=lambda sid: [], logs=logs, converse=converse,
        components=("tool_descriptions",), tools={"t": "old"},
        tool_turns=lambda sid: [{"role": "TOOL_CALL", "id": "c", "name": "t", "input": {}}],
        tool_spans=lambda sids: {},
    )
    for key in ("provider", "provider_model_id", "provider_meta", "system_prompt_status",
                "recommended_prompt"):
        assert key not in out
    assert out["tool_status"] == "COMPLETED" and out["tool_descriptions"] == {"t": "new"}


def test_service_no_results_stream_fails_requested_components_only():
    out = svc._third_party_prompt_recommendation(
        "e1", {"id": "a1", "system_prompt": "cur"}, WS, svc._noop,
        provider_id="gepa_lite", model_id="m", source={"run_id": "r"},
        components=("tool_descriptions",), tools={"t": "old"},
    )
    assert out["tool_status"] == "error" and "results log stream" in out["tool_error"]
    assert "system_prompt_status" not in out


def test_stage_recommend_routes_supported_types_to_provider(monkeypatch):
    captured: dict = {}

    def fake_provider_path(*a, **k):
        captured.update(k)
        return {"provider": "gepa_lite", "system_prompt_status": "COMPLETED",
                "recommended_prompt": "p", "tool_provider": "gepa_lite",
                "tool_status": "COMPLETED", "tool_descriptions": {}}

    def boom(*a, **k):
        raise AssertionError("AgentCore generators must not run")

    monkeypatch.setattr(svc, "_third_party_prompt_recommendation", fake_provider_path)
    monkeypatch.setattr(svc.ac, "start_system_prompt_recommendation", boom)
    monkeypatch.setattr(svc.ac, "start_tool_description_recommendation", boom)
    monkeypatch.setattr(svc, "_run_tool_recommendation", boom)
    monkeypatch.setattr(svc, "data_client", lambda ws: object())
    agent = {"resource_id": "rid", "runtime_name": "rt", "system_prompt": "cur",
             "tools": {"t": "d"}}
    out = svc.stage_recommend("e1", agent, WS, provider="gepa_lite", source={"run_id": "r"})
    assert captured["components"] == ("system_prompt", "tool_descriptions")
    assert captured["tools"] == {"t": "d"}
    assert out["tool_provider"] == "gepa_lite"


def test_stage_recommend_default_provider_runs_both_agentcore_generators(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(svc, "_third_party_prompt_recommendation",
                        lambda *a, **k: called.append("provider"))
    monkeypatch.setattr(svc, "data_client", lambda ws: object())
    monkeypatch.setattr(svc.ac, "start_system_prompt_recommendation",
                        lambda *a, **k: called.append("sp") or {"recommendationId": "r"})
    monkeypatch.setattr(svc.ac, "poll_recommendation", lambda *a, **k: {
        "status": "COMPLETED", "recommendationResult": {
            "systemPromptRecommendationResult": {"recommendedSystemPrompt": "x"}}})
    monkeypatch.setattr(svc, "_run_tool_recommendation",
                        lambda *a, **k: (called.append("td") or ({"t": "d2"}, "COMPLETED", "")))
    agent = {"resource_id": "rid", "runtime_name": "rt", "system_prompt": "cur",
             "tools": {"t": "d"}}
    out = svc.stage_recommend("e1", agent, WS)
    assert called == ["sp", "td"]
    assert "provider" not in out and "tool_provider" not in out


def _mk_exp(**kw):
    db = SessionLocal()
    exp = Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="EXP", agent_id="a1",
                     agent_name="agent", **kw)
    db.add(exp)
    db.commit()
    db.refresh(exp)
    db.close()
    return exp


def _reload(exp_id):
    db = SessionLocal()
    try:
        return db.get(Experiment, exp_id)
    finally:
        db.close()


def test_regenerating_one_type_keeps_the_other_types_attribution(monkeypatch):
    monkeypatch.setattr(svc, "_spawn", lambda target: target())
    monkeypatch.setattr(svc, "_agent_meta", lambda exp, ws: {"system_prompt": "cur", "tools": {}})
    monkeypatch.setattr(svc, "stage_recommend", lambda *a, **k: {
        "system_prompt_status": "COMPLETED", "recommended_prompt": "aws", "explanation": ""})
    exp = _mk_exp(artifacts={"agent_meta": {}, "recommend": {
        "provider": "gepa_lite", "provider_model_id": "m", "provider_meta": {},
        "recommended_prompt": "gepa", "system_prompt_status": "COMPLETED",
        "tool_provider": "gepa_lite", "tool_provider_model_id": "m", "tool_provider_meta": {},
        "tool_status": "COMPLETED", "tool_descriptions": {"t": "new"},
        "analyzed_tools": {"t": "old"}}})
    svc.act_recommend(exp.id, svc._noop, types=["system_prompt"])
    rec = _reload(exp.id).artifacts["recommend"]
    assert "provider" not in rec and rec["recommended_prompt"] == "aws"
    assert rec["tool_provider"] == "gepa_lite" and rec["tool_descriptions"] == {"t": "new"}
    # and the other way round
    monkeypatch.setattr(svc, "stage_recommend", lambda *a, **k: {
        "tool_status": "COMPLETED", "tool_descriptions": {}, "analyzed_tools": {"t": "old"}})
    svc.act_recommend(exp.id, svc._noop, types=["tool_descriptions"])
    rec = _reload(exp.id).artifacts["recommend"]
    assert "tool_provider" not in rec and rec["recommended_prompt"] == "aws"


def test_attribution_from_either_component():
    assert svc.recommendation_attribution({"tool_provider": "gepa_lite",
                                           "tool_provider_model_id": "m"}) == "gepa_lite · m"
    assert svc.recommendation_attribution({"provider": "gepa_lite", "provider_model_id": "a",
                                           "tool_provider": "gepa_lite",
                                           "tool_provider_model_id": "b"}) == "gepa_lite · a"


@pytest.mark.parametrize("types", [["tool_descriptions"], None])
def test_router_source_rule_covers_tool_descriptions_for_gepa_lite(client, monkeypatch, types):
    monkeypatch.setattr(svc, "run_action", lambda *a, **k: pytest.fail("dispatched"))
    exp = _mk_exp(artifacts={"agent_meta": {"system_prompt": "cur"}})
    body: dict[str, Any] = {"action": "recommend", "recommend_provider": "gepa_lite"}
    if types:
        body["recommend_types"] = types
    res = client.post(f"/api/experiments/{exp.id}/action", json=body)
    assert res.status_code == 422
    assert res.json()["code"] == "experiment.provider_requires_source"


# ─── check-agent additions ───────────────────────────────────────────────────
def test_precheck_settled_tool_side_survives_a_model_failure():
    """The pre-check made no model call for the tool side, so a later model
    failure on the prompt must not relabel it as `error`."""
    prov = gepa_lite.GepaLiteProvider()

    def converse(*_a):
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}},
                          "Converse")

    res = prov.optimize(_req([_session(calls=())]), svc._noop, converse=converse)
    assert res.status == "FAILED" and res.error == "ThrottlingException: slow"
    assert res.tool_status == "no-tool-calls" and "no tool calls" in res.tool_error
    assert res.tool_descriptions == {}


def test_unparseable_json_twice_fails_both_components():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(["not json", "still not json"])
    res = prov.optimize(_req([_session()]), svc._noop, converse=converse)
    assert len(converse.calls) == 2
    assert res.status == "FAILED" and "no parseable JSON" in (res.error or "")
    assert res.tool_status == "error" and res.tool_error == res.error
    assert res.recommended_prompt is None and res.tool_descriptions == {}


def test_stage_recommend_provider_component_ignores_recommend_tools(monkeypatch):
    """R5: the discovered set is authoritative for a provider; `recommend_tools`
    keeps its meaning only on the AgentCore branch."""
    captured: dict = {}
    monkeypatch.setattr(svc, "_third_party_prompt_recommendation",
                        lambda *a, **k: captured.update(k) or {})
    monkeypatch.setattr(svc, "data_client", lambda ws: object())
    agent = {"resource_id": "rid", "runtime_name": "rt", "system_prompt": "cur",
             "tools": {"own": "d"}}
    svc.stage_recommend("e1", agent, WS, provider="gepa_lite", source={"run_id": "r"},
                        tools={"caller_supplied": "x"})
    assert captured["tools"] == {"own": "d"}


def test_tool_explanation_is_owned_by_the_tool_type(monkeypatch):
    monkeypatch.setattr(svc, "_spawn", lambda target: target())
    monkeypatch.setattr(svc, "_agent_meta", lambda exp, ws: {"system_prompt": "cur", "tools": {}})
    monkeypatch.setattr(svc, "stage_recommend", lambda *a, **k: {
        "tool_status": "COMPLETED", "tool_descriptions": {}, "analyzed_tools": {"t": "old"}})
    exp = _mk_exp(artifacts={"agent_meta": {}, "recommend": {
        "tool_provider": "gepa_lite", "tool_status": "COMPLETED",
        "tool_descriptions": {"t": "new"}, "tool_explanation": "- stale note"}})
    svc.act_recommend(exp.id, svc._noop, types=["tool_descriptions"])
    assert "tool_explanation" not in _reload(exp.id).artifacts["recommend"]


def test_collect_evidence_normalizes_transcript_role_case():
    """session_transcript yields USER/ASSISTANT; the renderer filters on
    user/assistant. P1 dropped the whole conversation because of this — the
    reflection only ever saw judge explanations. Roles are normalised now."""
    logs = FakeLogs([_record("s1", "e", 0.5)])
    evidence, _ = ev.collect_evidence(
        workspace=WS, log_group="/g", log_stream="run-x", max_sessions=30, logs=logs,
        transcript=lambda sid: [{"role": "USER", "text": "the question"},
                                {"role": "ASSISTANT", "text": "the answer"}],
    )
    text = gepa_lite.render_reflective_dataset(evidence)
    assert "Inputs:\n  - the question" in text
    assert "Generated Outputs:\n  - the answer" in text


def test_truncated_reflection_retries_with_a_bigger_budget():
    prov = gepa_lite.GepaLiteProvider()
    calls: list[int] = []
    replies = iter(['{"diagnosis": "cut off', json.dumps({"revised_prompt": "P2"})])

    def converse(model_id, system, messages, max_tokens):
        calls.append(max_tokens)
        text = next(replies)
        stop = "max_tokens" if len(calls) == 1 else "end_turn"
        return text, {"input_tokens": 1, "output_tokens": 1, "stop_reason": stop}

    req = _req([_session()], components=("system_prompt",))
    req.max_tokens = 4096
    res = prov.optimize(req, svc._noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert calls == [4096, 8192]  # the retry doubles the budget


def test_truncated_twice_reports_the_token_limit():
    prov = gepa_lite.GepaLiteProvider()

    def converse(model_id, system, messages, max_tokens):
        return '{"diagnosis": "cut', {"input_tokens": 1, "output_tokens": max_tokens,
                                      "stop_reason": "max_tokens"}

    res = prov.optimize(_req([_session()]), svc._noop, converse=converse)
    assert res.status == "FAILED" and "truncated by the token limit" in res.error
    assert "raise prompt_opt_max_tokens" in res.error
    assert res.tool_status == "error"
