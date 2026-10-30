"""Tool-description evidence and the two-component gepa_lite reflection (N12 E2-4):
content-log tool turns, span fallback, mentions, validation, per-type key ownership and
the worker handler. Evidence records are ``eval_results`` rows (the experiment seams
left with the configuration experiment, N13-obs2)."""

import json
from typing import Any

from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import jobs
from app.evaluation.models import EvalResult, EvalRun
from app.models.ledger import Agent
from app.optimization.providers import evidence as ev
from app.optimization.providers import gepa_lite
from app.optimization.providers.base import (
    EvaluatorRecord,
    EvidenceStats,
    OptimizeRequest,
    SessionEvidence,
)
from app.services import observability as ob
from app.services.strands_eval import jobs_recommend
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor

WS = ws_ctx()


def _noop(_msg: str) -> None:
    """A progress sink."""
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


def _record(sid, evaluator, score, explanation="why", level="SESSION"):
    """One eval_results row as the evidence reads it."""
    return {"session_id": sid, "evaluator_id": evaluator, "score": score,
            "explanation": explanation, "label": None, "level": level, "error_message": None}


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
        evidence=evidence, stats=stats, model_id="global.zai.glm-5.3", workspace=WS,
        max_chars=max_chars, components=components,
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
                                 "asked about policy.", level="TOOL_CALL")],
    )


def _converse(replies: list[str]):
    calls: list[dict[str, Any]] = []
    it = iter(replies)

    def fn(model_id, system, messages, max_tokens):
        calls.append({"system": system, "user": messages[0]["text"]})
        return next(it), {"input_tokens": 1, "output_tokens": 1}

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# ─── content-log tool turns (restored observability helpers) ─────────────────
def test_tool_turns_dedupe_order_and_result_join():
    recs_ = [
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
    turns = ob.eval_tool_turns_from_content_logs("/g", "s1", None, WS, logs=FakeFilterLogs(recs_))
    roles = [(t["role"], t["id"]) for t in turns]
    assert roles == [("TOOL_CALL", "a"), ("TOOL_RESULT", "a"),
                     ("TOOL_CALL", "b"), ("TOOL_RESULT", "b")]
    assert turns[0]["name"] == "get_pto_balance" and turns[0]["input"] == {"e": 1}
    assert turns[1]["text"] == "12 days"  # the input/tool copy wins over the echo
    assert turns[3]["status"] == "error" and turns[3]["name"] == "lookup_hr_policy"
    assert turns[0]["trace_id"] == "tr1"


def test_tool_turns_ignore_plain_string_content_and_bad_json():
    recs_ = [{"timeUnixNano": 1, "traceId": "t", "attributes": {"session.id": "s1"},
              "body": {"input": {"messages": [{"role": "tool", "content": "plain"}]},
                       "output": {"messages": [{"role": "assistant", "content": "[not json"}]}}}]
    turns = ob.eval_tool_turns_from_content_logs("/g", "s1", None, WS, logs=FakeFilterLogs(recs_))
    assert turns == []


def test_text_turns_unchanged_by_refactor():
    recs_ = [_content_record(1, "tr1", [("input", "user", [{"text": "q"}]),
                                         ("output", "assistant", [_use("a", "x", {})])]),
             {"timeUnixNano": 2, "traceId": "tr1", "attributes": {"session.id": "s1"},
              "body": {"output": {"messages": [{"role": "assistant",
                                               "content": {"message": "final",
                                                           "finish_reason": "end_turn"}}]}}}]
    turns = ob.eval_turns_from_content_logs("/g", "s1", None, WS, logs=FakeFilterLogs(recs_))
    assert [(t["role"], t["text"]) for t in turns] == [("USER", "q"), ("ASSISTANT", "final")]


# ─── evidence merge / cap / spans / mentions ─────────────────────────────────
def test_collect_evidence_merges_tool_turns_by_time_and_caps():
    text = lambda sid: [{"role": "USER", "text": "q", "at": "2026-01-01T00:00:00"},  # noqa: E731
                        {"role": "ASSISTANT", "text": "a", "at": "2026-01-01T00:00:09"}]
    raw = []
    for i in range(25):
        raw.append({"role": "TOOL_CALL", "id": f"c{i}", "name": "get_pto_balance",
                    "input": {"i": i, "blob": "x" * 900}, "at": f"2026-01-01T00:00:0{min(i, 8)}"})
        raw.append({"role": "TOOL_RESULT", "id": f"c{i}", "name": "get_pto_balance",
                    "status": "success", "text": "ok", "at": f"2026-01-01T00:00:0{min(i, 8)}"})
    evidence, stats = ev.collect_evidence(
        records=[_record("s1", "Builtin.Helpfulness", 0.5)], transcript=text, max_sessions=30,
        tool_turns=lambda sid: raw, tool_spans=lambda sids: {},
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
    asked: list[list[str]] = []

    def spans(sids):
        asked.append(sids)
        return {"s1": [{"id": "c1", "name": "kb_search", "status": "success",
                        "description": "Search the KB"},
                       {"id": "c2", "name": "kb_search", "status": "error", "description": None}],
                "s2": [{"id": "c3", "name": "skills", "status": "success",
                        "description": "Skills"}]}

    evidence, stats = ev.collect_evidence(
        records=[_record("s1", "e", 0.1), _record("s2", "e", 0.9)],
        transcript=lambda sid: [], max_sessions=30,
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
    def boom(*_a):
        raise RuntimeError("cw down")

    evidence, stats = ev.collect_evidence(
        records=[_record("s1", "e", 0.1)], transcript=lambda sid: [], max_sessions=30,
        tool_turns=boom, tool_spans=boom,
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
    res = prov.optimize(_req([_session()]), _noop, converse=converse)
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
                        _noop, converse=converse)
    assert res.recommended_prompt is None and res.status == "FAILED"
    assert "frozen" in (res.error or "")
    assert res.tool_status == "COMPLETED" and res.tool_descriptions == {"get_pto_balance": "new"}
    assert "INSTRUCTION IS FROZEN" in converse.calls[0]["system"]


def test_tools_empty_result_is_completed_not_failed():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([json.dumps({"revised_prompt": "P2", "tool_descriptions": {}})])
    res = prov.optimize(_req([_session()]), _noop, converse=converse)
    assert res.tool_status == "COMPLETED" and res.tool_descriptions == {}


def test_no_tool_calls_settles_tool_side_and_prompt_proceeds():
    prov = gepa_lite.GepaLiteProvider()
    sess = _session(calls=())
    converse = _converse([json.dumps({"revised_prompt": "P2"})])
    res = prov.optimize(_req([sess]), _noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert res.tool_status == "no-tool-calls" and "no tool calls" in res.tool_error
    assert res.tool_descriptions == {}
    # the model was asked for the instruction only
    assert "INSTRUCTION ONLY" in converse.calls[0]["system"]
    # tools-only with no calls → no model call at all
    converse = _converse([])
    res = prov.optimize(_req([sess], components=("tool_descriptions",)), _noop,
                        converse=converse)
    assert res.status == "FAILED" and res.tool_status == "no-tool-calls"
    assert converse.calls == []


def test_no_discovered_tools_is_no_tools():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse([json.dumps({"revised_prompt": "P2"})])
    res = prov.optimize(_req([_session()], tools={}), _noop, converse=converse)
    assert res.tool_status == "no-tools" and res.recommended_prompt == "P2"


def test_client_error_fails_both_components():
    prov = gepa_lite.GepaLiteProvider()

    def converse(*_a):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                          "Converse")

    res = prov.optimize(_req([_session()]), _noop, converse=converse)
    assert res.status == "FAILED" and res.error == "AccessDeniedException: nope"
    assert res.tool_status == "error" and res.tool_error == res.error
    assert res.recommended_prompt is None and res.tool_descriptions == {}


# ─── the run-scoped seam (recommendations.provider_recommendation) ───────────
def _mk_run(**kw) -> str:
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="tool-agent", method="zip_runtime",
                  status="active", resource_id="rt-1", spec={})
    db.add(agent)
    db.flush()
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name="agent",
                  status="completed", session_ids=["s1"], **kw)
    db.add(run)
    db.commit()
    rid = run.id
    db.close()
    return rid


def _results(run_id: str, records: list[dict[str, Any]]) -> None:
    db = SessionLocal()
    for i, rec in enumerate(records):
        db.add(EvalResult(
            id=jobs.record_id(run_id, rec["session_id"], rec["evaluator_id"], i),
            workspace_id=DEFAULT_WORKSPACE_ID, source="run", run_id=run_id,
            session_id=rec["session_id"], evaluator_id=rec["evaluator_id"], level=rec["level"],
            score=rec["score"], explanation=rec["explanation"],
        ))
    db.commit()
    db.close()


def _run_to_completion(executor: FakeExecutor, converse):
    """``sleep`` double: run the submitted job's reflection with the scripted converse."""

    def sleep(_s):
        job = executor.submitted[-1]
        spec = job.spec
        result = jobs_recommend.reflect(
            converse=converse, model_id=spec["model_id"], current_prompt=spec["current_prompt"],
            evidence=spec["evidence"], stats=spec["stats"], tools=spec["tools"],
            components=tuple(spec["components"]), extra_feedback=spec["extra_feedback"],
            max_chars=spec["max_chars"], max_tokens=spec["max_tokens"],
        )
        executor.complete(job.job_id, [], summary={"recommendation": result})

    return sleep


# ─── experiment artefact key ownership (W-E3's act_recommend, stage mocked) ──
# ─── check-agent additions ───────────────────────────────────────────────────
def test_precheck_settled_tool_side_survives_a_model_failure():
    """The pre-check made no model call for the tool side, so a later model
    failure on the prompt must not relabel it as `error`."""
    prov = gepa_lite.GepaLiteProvider()

    def converse(*_a):
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}},
                          "Converse")

    res = prov.optimize(_req([_session(calls=())]), _noop, converse=converse)
    assert res.status == "FAILED" and res.error == "ThrottlingException: slow"
    assert res.tool_status == "no-tool-calls" and "no tool calls" in res.tool_error
    assert res.tool_descriptions == {}


def test_unparseable_json_twice_fails_both_components():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(["not json", "still not json"])
    res = prov.optimize(_req([_session()]), _noop, converse=converse)
    assert len(converse.calls) == 2
    assert res.status == "FAILED" and "no parseable JSON" in (res.error or "")
    assert res.tool_status == "error" and res.tool_error == res.error
    assert res.recommended_prompt is None and res.tool_descriptions == {}


def test_collect_evidence_normalizes_transcript_role_case():
    """session_transcript yields USER/ASSISTANT; the renderer filters on
    user/assistant. Roles are normalised so the reflection sees the conversation."""
    evidence, _ = ev.collect_evidence(
        records=[_record("s1", "e", 0.5)], max_sessions=30,
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
    res = prov.optimize(req, _noop, converse=converse)
    assert res.status == "COMPLETED" and res.recommended_prompt == "P2"
    assert calls == [4096, 8192]  # the retry doubles the budget


def test_truncated_twice_reports_the_token_limit():
    prov = gepa_lite.GepaLiteProvider()

    def converse(model_id, system, messages, max_tokens):
        return '{"diagnosis": "cut', {"input_tokens": 1, "output_tokens": max_tokens,
                                      "stop_reason": "max_tokens"}

    res = prov.optimize(_req([_session()]), _noop, converse=converse)
    assert res.status == "FAILED" and "truncated by the token limit" in res.error
    assert "raise prompt_opt_max_tokens" in res.error
    assert res.tool_status == "error"


def test_parse_reflection_accepts_raw_newlines_inside_strings():
    raw = '{"diagnosis": "line one\nline two", "revised_prompt": "a\n\tb"}'  # literal control chars
    obj = gepa_lite.parse_reflection(raw)
    assert obj == {"diagnosis": "line one\nline two", "revised_prompt": "a\n\tb"}
    assert gepa_lite.parse_reflection("no json here") is None
    assert "no JSON object" in gepa_lite.describe_unparseable("just prose")
    assert "JSON error" in gepa_lite.describe_unparseable('{"a": 1,, "b"}')


def test_unparseable_reply_reason_carries_the_json_error():
    prov = gepa_lite.GepaLiteProvider()
    converse = _converse(['{"diagnosis": "x" "revised_prompt": 1}', '{"a": [}'])
    res = prov.optimize(_req([_session()], components=("system_prompt",)), _noop,
                        converse=converse)
    assert res.status == "FAILED"
    assert res.error.startswith("reflection model returned no parseable JSON — JSON error:")
