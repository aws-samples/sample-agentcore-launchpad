"""The local evaluation workbench (``backend/scripts/eval_workbench.py``, evaluation-engine.md
§26): its pure parts — text matching, the cache and its Logs client, the ledger / Insights
parsing, the metrics, the report — and one offline end-to-end run over the committed
real-record fixtures through the cache layer and the PRODUCTION provider."""

from __future__ import annotations

import base64
import copy
import json
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SCRIPTS = BACKEND / "scripts"
FIXTURES = sorted((BACKEND / "tests" / "fixtures").glob("obs315_*.json"))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import eval_workbench  # noqa: E402
from evalwb import corpus, fetch, global_eval, judge, metrics, reconcile, report  # noqa: E402
from evalwb.cache import (  # noqa: E402,I001
    RUNTIME,
    SPANS,
    CachedLogs,
    RecordCache,
    SessionRecords,
    load_file,
)


def _entry(name: str) -> SessionRecords:
    return load_file(BACKEND / "tests" / "fixtures" / name)


PREAMBLE = "obs315_preamble_session.json"
TWO_TURN = "obs315_two_turn_session.json"
PAIPAI = "obs315_paipai_session.json"


# ── matching / normalisation ────────────────────────────────────────────────

def test_same_text_ignores_whitespace_and_block_joins():
    assert reconcile.same_text("已查到，\n给您 整理", "已查到，给您\n\n整理")
    assert not reconcile.same_text("我先查一下", "已查到，给您整理")
    assert not reconcile.same_text("", "answer")
    assert reconcile.same_text(None, "")


def test_same_text_is_a_prefix_match_only_when_the_console_truncated():
    origin = "x" * reconcile.CONSOLE_TEXT_CAP
    assert reconcile.same_text(origin + " and the rest", origin)
    assert not reconcile.same_text("abc and more", "abc")


def test_tool_oracle_prefers_the_agent_span_over_the_mcp_client_span():
    entry = _entry(PREAMBLE)
    (turn,) = reconcile.origin_turns(entry)
    assert turn.completed
    assert sorted(turn.tools) == ["kb_search", "launchpad_gw_hr-database___get_employee"]
    # ADOT's MCP-client span under a gateway tool is also tagged execute_tool (live records)
    tool = "launchpad_gw_hr-database___get_employee"
    gateway = next(x for x in entry.records
                   if x["group"] == SPANS and reconcile.tool_name(x["record"]) == tool)
    mcp = copy.deepcopy(gateway)
    mcp["record"].update(name="mcp tools/call hr-database___get_employee",
                         scope={"name": "amazon.opentelemetry.distro.instrumentation.mcp"})
    mcp["record"]["attributes"]["gen_ai.tool.name"] = "hr-database___get_employee"
    mcp["record"]["spanId"] = "ffffffffffffffff"
    entry.records.append(mcp)
    (turn,) = reconcile.origin_turns(entry)
    assert sorted(turn.tools) == ["kb_search", tool]
    assert [r.status for r in reconcile.reconcile_session(entry, "both").rows] == ["match"]


def test_a_called_a2a_peers_spans_are_not_the_sessions_tool_calls():
    # live shape (probe-multiagent): the peer's execute_tool span carries the propagated
    # trace + session.id but the peer runtime's service.name
    entry = _entry(PREAMBLE)
    own = next(x for x in entry.records if x["group"] == SPANS
               and reconcile.tool_name(x["record"]) == "kb_search")
    peer = copy.deepcopy(own)
    peer["record"].update(name="execute_tool calculator", spanId="eeeeeeeeeeeeeeee")
    peer["record"]["attributes"]["gen_ai.tool.name"] = "calculator"
    peer["record"].setdefault("resource", {"attributes": {}})["attributes"][
        "service.name"] = "probe_a2a_peer_725d62.DEFAULT"
    entry.records.append(peer)
    assert reconcile.own_services(entry)  # the runtime group's own service
    (turn,) = reconcile.origin_turns(entry)
    assert "calculator" not in turn.tools
    assert "eeeeeeeeeeeeeeee" not in global_eval.tool_spans(entry)


# ── cache layer ─────────────────────────────────────────────────────────────

def test_cache_round_trip_and_safe_file_names(tmp_path):
    cache = RecordCache(tmp_path / "c")
    entry = SessionRecords("sess#1:a@b", "/g", [{"timestamp": 1, "group": RUNTIME,
                                                "record": {"x": 1}}], meta={"window": [1, 2]})
    path = cache.save(entry)
    assert path.parent.name == "sessions" and "#" not in path.name and ":" not in path.name
    loaded = cache.load("sess#1:a@b")
    assert loaded.to_json() == entry.to_json() and loaded.meta == {"window": [1, 2]}
    assert cache.load("sess_1_a_b") is None  # the sanitised twin is a different session
    assert (tmp_path / "c").stat().st_mode & 0o777 == 0o700
    cache.save_json("runs", "default-r1", {"run": {"id": "r1"}})
    assert cache.load_json("runs", "default-r1") == {"run": {"id": "r1"}}
    assert cache.load_json("runs", "nope") is None


def test_a_fixture_is_a_cache_entry():
    entry = _entry(PREAMBLE)
    assert entry.session_id.startswith("257f17b16cbb") and entry.log_group
    assert entry.counts[RUNTIME] > 0 and entry.counts[SPANS] > 0


def _records() -> SessionRecords:
    def rec(ts, group, trace, span, **extra):
        body = {"traceId": trace, "spanId": span,
                "attributes": {"session.id": "s-0000001"}, **extra}
        return {"timestamp": ts, "group": group, "record": body}

    return SessionRecords("s-0000001", "/aws/rt-DEFAULT", [
        rec(3, SPANS, "t1", "b", parentSpanId="a", startTimeUnixNano=3),
        rec(1, RUNTIME, "t1", "a", body={"input": {}}),
        rec(2, SPANS, "t2", "c", startTimeUnixNano=2),
    ])


def _query(logs: CachedLogs, groups: list[str], query: str) -> list[list[dict[str, str]]]:
    qid = logs.start_query(logGroupNames=groups, startTime=0, endTime=1, queryString=query)
    out = logs.get_query_results(queryId=qid["queryId"])
    assert out["status"] == "Complete"
    return out["results"]


def test_cached_logs_answers_the_providers_queries_like_cloudwatch():
    entry = _records()
    logs = CachedLogs({entry.session_id: entry})
    content = "fields @message | filter attributes.session.id = 's-0000001' | sort @timestamp"
    both = [json.loads(r[0]["value"])["spanId"] for r in _query(logs, ["/x", SPANS], content)]
    assert both == ["a", "c", "b"]  # both groups, @timestamp order
    assert [json.loads(r[0]["value"])["spanId"] for r in _query(logs, ["/x"], content)] == ["a"]
    hierarchy = _query(logs, [SPANS], "fields spanId, parentSpanId, traceId | filter "
                                      "attributes.session.id = 's-0000001'")
    assert [{f["field"]: f["value"] for f in row} for row in hierarchy] == [
        {"spanId": "c", "traceId": "t2"}, {"spanId": "b", "parentSpanId": "a", "traceId": "t1"}]
    assert _query(logs, ["/x", SPANS], content.replace("s-0000001", "other")) == []


def test_cached_logs_filter_log_events_and_trace_narrowing():
    entry = _records()
    logs = CachedLogs({entry.session_id: entry})
    events = logs.filter_log_events(logGroupName="/aws/rt-DEFAULT", filterPattern='"s-0000001"')
    assert [json.loads(e["message"])["spanId"] for e in events["events"]] == ["a"]
    assert logs.filter_log_events(logGroupName="/other", filterPattern='"s-0000001"') == {
        "events": []}
    narrowed = CachedLogs({entry.session_id: entry}, trace="t2")
    content = "fields @message | filter attributes.session.id = 's-0000001'"
    assert [json.loads(r[0]["value"])["spanId"]
            for r in _query(narrowed, ["/x", SPANS], content)] == ["c"]


# ── fetch parsing (no AWS) ──────────────────────────────────────────────────

def test_insights_rows_become_cache_records_per_session():
    rows = [
        [{"field": "@timestamp", "value": "2026-10-23 14:13:25.784"},
         {"field": "@message", "value": json.dumps({"attributes": {"session.id": "s-1"}})}],
        [{"field": "@timestamp", "value": "2026-10-23 14:13:26.000"},
         {"field": "@message", "value": "not json"}],
        [{"field": "@timestamp", "value": "2026-10-23 14:13:27.000"},
         {"field": "@message", "value": json.dumps({"no": "session"})}],
    ]
    out = fetch.rows_to_records(rows, SPANS)
    assert list(out) == ["s-1"]
    assert out["s-1"][0]["timestamp"] == 1792764805784 and out["s-1"][0]["group"] == SPANS


def test_session_query_filters_like_the_provider_and_refuses_quotes():
    assert "attributes.session.id = 'abc'" in fetch.session_query(["abc"])
    assert "in ['a', 'b']" in fetch.session_query(["b", "a"])
    with pytest.raises(ValueError):
        fetch.session_query(["x' or 1=1"])


def test_host_payload_round_trip_and_truncation():
    payload = {"run": {"id": "r1", "log_source": None}, "agent": {"resource_id": "rt_ab-XYZ"}}
    line = fetch._MARK + base64.b64encode(zlib.compress(json.dumps(payload).encode())).decode()
    assert fetch.parse_host_output(f"noise\n{line}\n") == payload
    with pytest.raises(RuntimeError, match="truncated"):
        fetch.parse_host_output(line[:-40])
    with pytest.raises(RuntimeError, match="no run payload"):
        fetch.parse_host_output("Traceback …")
    assert fetch.run_log_group(payload) == "/aws/bedrock-agentcore/runtimes/rt_ab-XYZ-DEFAULT"
    payload["run"]["log_source"] = {"log_group_names": [SPANS, "/aws/custom"]}
    assert fetch.run_log_group(payload) == "/aws/custom"


def test_fetch_window_follows_the_run_creation_time():
    start, end = fetch.fetch_window("2026-10-23 14:13:15.578886", 168)
    assert start.isoformat().startswith("2026-10-23T14:03:15")
    assert (end - start).total_seconds() <= 26 * 3600


# ── reconciliation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", sorted(reconcile.MODES))
def test_reconcile_real_preamble_session_matches_the_console(mode):
    result = reconcile.reconcile_session(_entry(PREAMBLE), mode)
    assert [r.status for r in result.rows] == ["match"]
    assert result.rows[0].origin_answer.startswith("已查到，给您整理如下")
    assert result.order_ok and result.error is None


def test_reconcile_flags_a_preamble_extraction(monkeypatch):
    # the pre-obs315 library mapper, through the workbench: the regression it exists to catch
    from strands_evals.mappers import CloudWatchSessionMapper

    from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider

    def old_provider(entry, sessions, mode):
        return LaunchpadSpansProvider(CachedLogs(sessions), [SPANS_LOG_GROUP, entry.log_group],
                                      mapper=CloudWatchSessionMapper())

    monkeypatch.setattr(reconcile, "provider_for", old_provider)
    result = reconcile.reconcile_session(_entry(PREAMBLE), "both")
    assert [r.status for r in result.rows] == ["MISMATCH"]
    assert "我先查一下差旅标准" in result.rows[0].extracted_answer
    summary = reconcile.m1_summary([result])
    assert (summary["matched"], summary["denominator"]) == (0, 1)


def test_reconcile_counts_a_leaked_failed_turn(monkeypatch):
    entry = _entry(PAIPAI)
    real = reconcile.extracted_turns

    def leaking(task_output):
        turns = real(task_output)
        turns[3].answer = turns[1].answer  # turn 4 scored on turn 2's text (MT1)
        turns[3].tools = []
        return turns

    monkeypatch.setattr(reconcile, "extracted_turns", leaking)
    result = reconcile.reconcile_session(entry, "both")
    statuses = [r.status for r in result.rows]
    assert statuses[0] == "match" and statuses[3] == "LEAK"
    assert result.leaks == 1 and reconcile.m1_summary([result])["leaks"] == 1


def test_reconcile_a_failed_turn_without_an_agent_span_is_not_an_error(monkeypatch):
    # the pre-fix mapper dropped a failed turn's agent span: nothing is judged for it, which
    # is what Global does too (SpanStatusErrorException) — not an extraction error
    real = reconcile.extracted_turns

    def unjudged(task_output):
        turns = real(task_output)
        turns[3].answer, turns[3].prompt, turns[3].tools = None, "", []
        return turns

    monkeypatch.setattr(reconcile, "extracted_turns", unjudged)
    row = reconcile.reconcile_session(_entry(PAIPAI), "both").rows[3]
    assert (row.status, row.prompt_ok, row.completed) == ("failed_turn", None, False)


def test_reconcile_missing_turn_and_provider_error(monkeypatch):
    entry = _entry(TWO_TURN)
    real = reconcile.extracted_turns
    monkeypatch.setattr(reconcile, "extracted_turns", lambda out: real(out)[:1])
    result = reconcile.reconcile_session(entry, "both")
    assert [r.status for r in result.rows] == ["match", "MISSING"]

    empty = SessionRecords(entry.session_id, entry.log_group, [])
    failed = reconcile.reconcile_session(empty, "both")
    assert failed.error and "SessionNotFound" in failed.error and failed.rows == []


def test_corpus_tags_and_error_turns(tmp_path):
    entry = _entry(PAIPAI)
    result = reconcile.reconcile_session(entry, "both")
    prompts = [r.origin_prompt for r in result.rows]
    turns = [{"prompt": prompts[0], "delta_text": "ok", "error": None},
             {"prompt": prompts[1], "error": {"message": "SdkStreamError: x",
                                             "detail": {"error_type": "ReadTimeoutError"}}},
             {"prompt": prompts[2], "error": "runtime timeout"},
             {"prompt": "never reached the logs", "error": "boom"}]
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps({
        "session_id": entry.session_id, "agent_id": "a1", "agent": "probe-x",
        "start": "2026-10-23T16:20:20.421+00:00", "end": "2026-10-23T16:21:00+00:00",
        "tags": {"turns": 4, "ending": "runtime_timeout", "smoke": True}, "turns": turns}) + "\n")
    (session,) = corpus.load(path)
    start, end = corpus.window([session])
    assert (end - start).total_seconds() == 40 * 60 + 39.579
    tags = corpus.tag_breakdown([result], [session])
    assert set(tags) == {"agent", "turns", "ending"}  # smoke is not a dimension
    assert tags["ending"]["runtime_timeout"]["sessions"] == 1
    rows = corpus.error_turns([result], [session])
    assert [(r["turn"], r["in_logs"]) for r in rows] == [(2, True), (3, True), (4, False)]
    # judged = extracted AND not excluded by the code's own failed-turn rule (obs373)
    assert [r["judged"] for r in rows[:2]] == [not r["excluded_by_engine"] for r in rows[:2]]
    assert not rows[2]["judged"] and not rows[2]["excluded_by_engine"]
    assert rows[0]["error"].startswith("ReadTimeoutError")
    summary = corpus.error_summary(rows)
    assert (summary["error_turns"], summary["in_logs"]) == (3, 2)
    assert summary["judged"] == sum(r["judged"] for r in rows)


# ── metrics ─────────────────────────────────────────────────────────────────

def _rec(evaluator, session, level, score, label, trace=None, span=None, rep=0, error=None):
    return {"evaluator_id": evaluator, "session_id": session, "level": level, "score": score,
            "label": label, "trace_id": trace, "span_id": span, "rep": rep, "error_type": error}


def test_metrics_units_compare_and_coverage():
    tool_spans = {"s1": ["t1:sp1", "t1:sp2"]}
    traces = {"s1": ["t1"], "s2": ["t2"]}
    ours = [_rec("H", "s1", "TRACE", 1.0, "Yes", trace="t1"),
            _rec("H", "s2", "TRACE", None, None, trace="t9", error="Boom"),  # not a unit
            _rec("T", "s1", "TOOL_CALL", 1.0, "Yes"), _rec("T", "s1", "TOOL_CALL", 0.0, "No"),
            _rec("G", "s1", "SESSION", 0.5, "Partly")]
    ref = [_rec("H", "s1", "TRACE", 0.5, "yes ", trace="t1"),
           _rec("H", "s2", "TRACE", 1.0, "Yes", trace="t2"),
           _rec("T", "s1", "TOOL_CALL", 1.0, "Yes", trace="t1", span="sp1"),
           _rec("T", "s1", "TOOL_CALL", 1.0, "Yes", span="sp2"),  # no trace: the one sp2
           _rec("G", "s1", "SESSION", 0.5, "Partly")]
    got = metrics.compare(metrics.units(ours, tool_spans), metrics.units(ref, tool_spans),
                          agent_traces=traces, tool_spans=tool_spans, sessions=["s1", "s2"])
    h = got["H"]
    assert (h["ours"], h["reference"], h["matched"], h["coverage_vs_reference"]) == (1, 2, 1, 0.5)
    assert (h["label_agree"], h["labelled"], h["mad"], h["bias"]) == (1, 1, 0.5, 0.5)
    assert (h["m4_scored"], h["m4_turns"], h["m4"]) == (1, 2, 0.5)  # 1 of 2 agent turns
    assert h["turn_mean_gap"] == 0.5
    assert (got["T"]["matched"], got["T"]["label_agree"], got["T"]["mad"]) == (2, 1, 0.5)
    assert (got["T"]["m4_turns"], got["G"]["m4_turns"]) == (2, 2)
    assert got["G"]["coverage_vs_reference"] == 1.0 and got["G"]["mad"] == 0.0
    only = metrics.compare(metrics.units(ours, tool_spans), metrics.units(ref, tool_spans), ["G"])
    assert list(only) == ["G"]
    scoped = metrics.units([_rec("T", "s1", "TOOL_CALL", 1, "Yes", span="trace:sp2")])
    assert list(scoped) == [("T", "s1", "trace:sp2")]


def test_metrics_tool_units_are_trace_scoped_when_span_ids_repeat():
    # the corpus 3cdd38248181 shape (2 turns: run_sql + list_stations, then run_sql x2) with a
    # snapshot-restored microVM redrawing turn 1's span ids in turn 2 (N17-obs315 MT2)
    tool_spans = {"s": ["ta:aa", "ta:bb", "tb:aa", "tb:bb"]}
    ours = [_rec("T", "s", "TOOL_CALL", 1.0, "Yes", trace="ta", span="aa"),
            _rec("T", "s", "TOOL_CALL", 1.0, "Yes", trace="ta", span="bb"),
            _rec("T", "s", "TOOL_CALL", 0.0, "No", trace="tb", span="aa"),
            _rec("T", "s", "TOOL_CALL", 1.0, "Yes", trace="tb", span="bb")]
    ref = [{**r, "score": 1.0 - r["score"] if r["trace_id"] == "ta" else r["score"],
            "label": {"Yes": "No", "No": "Yes"}[r["label"]] if r["trace_id"] == "ta"
            else r["label"]} for r in ours]
    mine = metrics.units(ours, tool_spans)
    assert sorted(u[2] for u in mine) == tool_spans["s"]  # 4 units, none collapsed
    got = metrics.compare(mine, metrics.units(ref, tool_spans), tool_spans=tool_spans,
                          sessions=["s"])["T"]
    # each call pairs with ITS Global row: turn a disagrees, turn b agrees
    assert (got["matched"], got["label_agree"], got["mad"]) == (4, 2, 0.5)
    assert (got["m4_scored"], got["m4_turns"]) == (4, 4)
    # a bare repeated span id without a trace cannot be placed — it is never paired by guess
    assert list(metrics.units([_rec("T", "s", "TOOL_CALL", 1.0, "Yes", span="aa")], tool_spans)) \
        == [("T", "s", ":aa")]


def test_metrics_a_per_session_trace_row_is_the_last_turn_it_judged():
    # production judges the LAST agent turn and stamps the FIRST trace id (obs322)
    rows = [_rec("H", "s1", "TRACE", 1.0, "Yes", trace="t1")]
    traces = {"s1": ["t1", "t2", "t3"]}
    units = metrics.units(rows, agent_traces=traces)
    assert list(units) == [("H", "s1", "t3")] and units[("H", "s1", "t3")]["_stamped_elsewhere"]
    per_turn = [_rec("H", "s1", "TRACE", 1.0, "Yes", trace=t) for t in ("t1", "t2", "t3")]
    assert sorted(u[2] for u in metrics.units(per_turn, agent_traces=traces)) == [
        "t1", "t2", "t3"]
    ref = [_rec("H", "s1", "TRACE", v, "Yes", trace=t) for t, v in (("t1", 0.0), ("t2", 0.5),
                                                                     ("t3", 1.0))]
    row = metrics.compare(units, metrics.units(ref), agent_traces=traces)["H"]
    assert (row["m4"], row["stamped_elsewhere"], row["turn_mean_gap"]) == (0.3333, 1, 0.5)


def test_metrics_global_rerun_variance_and_errors():
    records = [_rec("H", "s1", "TRACE", 1.0, "Yes", trace="t1", rep=0),
               _rec("H", "s1", "TRACE", 0.5, "Partly", trace="t1", rep=1),
               _rec("H", "s1", "TRACE", 1.0, "Yes", trace="t1", rep=2),
               _rec("H", "s1", "TRACE", 1.0, "Yes", trace="t2", rep=0),
               _rec("H", "s1", "TRACE", 1.0, "Yes", trace="t2", rep=1),
               _rec("H", "s1", "TRACE", 1.0, "yes", trace="t2", rep=2),
               _rec("H", "s1", "TRACE", None, None, trace="t3", rep=0,
                    error="SpanStatusErrorException")]
    assert metrics.rerun_variance(records, 3) == {"H": {"units": 2, "identical": 1,
                                                        "label_rate": 0.5, "mean_std": 0.1179}}
    assert metrics.rerun_variance(records, 1) == {}
    assert metrics.error_counts(records) == {"H": {"SpanStatusErrorException": 1}}


# ── Global request building (no AWS) ────────────────────────────────────────

class _Evaluate:
    def __init__(self, fail: bool = False) -> None:
        self.calls, self.fail = 0, fail

    def evaluate(self, **request: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("ThrottlingException")
        return {"evaluationResults": [{
            "evaluatorId": request["evaluatorId"], "value": 1.0, "label": "Yes",
            "context": {"spanContext": {"sessionId": "s", "traceId": t}}}
            for t in request.get("evaluationTarget", {}).get("traceIds", [])]}


def test_global_requests_target_every_turn_and_carry_the_truth():
    entry = _entry(TWO_TURN)
    spans = global_eval.session_spans(entry)
    assert spans and all(r.get("scope") and r.get("traceId") for r in spans)
    ends = [int(r.get("endTimeUnixNano") or 0) for r in spans]
    assert ends == sorted(ends)
    traces = global_eval.agent_traces(entry)
    assert len(traces) == 2
    truth = {"expected_response": "E", "assertions": ["A"], "expected_trajectory": ["kb"]}
    (trace_req,) = global_eval.requests_for(entry, "Builtin.Correctness", "TRACE", truth)
    assert trace_req["evaluationTarget"] == {"traceIds": traces}
    session_ref, trace_ref = trace_req["evaluationReferenceInputs"]  # the SDK's shape
    assert session_ref == {"context": {"spanContext": {"sessionId": entry.session_id}},
                           "assertions": [{"text": "A"}],
                           "expectedTrajectory": {"toolNames": ["kb"]}}
    assert trace_ref["context"]["spanContext"]["traceId"] == traces[-1]
    (session_req,) = global_eval.requests_for(entry, "Builtin.GoalSuccessRate", "SESSION", truth)
    assert "evaluationTarget" not in session_req
    assert session_req["evaluationReferenceInputs"] == trace_req["evaluationReferenceInputs"]
    none_expected = global_eval.reference_inputs(
        "s", {"expected_trajectory": [], "expected_trajectory_defined": True}, traces)
    assert none_expected == [{"context": {"spanContext": {"sessionId": "s"}},
                              "expectedTrajectory": {"toolNames": []}}]
    assert global_eval.reference_inputs("s", {"expected_trajectory": None}, traces) == []
    assert global_eval.reference_inputs("s", {}, traces) == []
    # the engine's obs376 rule: an explicit [] (no flag from judge.ground_truth) is "no tools"
    assert global_eval.reference_inputs("s", {"expected_trajectory": []}, traces) == [
        {"context": {"spanContext": {"sessionId": "s"}}, "expectedTrajectory": {"toolNames": []}}]
    tool_reqs = global_eval.requests_for(entry, "Builtin.ToolSelectionAccuracy", "TOOL_CALL", None)
    assert [len(r["evaluationTarget"]["spanIds"]) for r in tool_reqs] == [
        len(global_eval.tool_spans(entry))]


def test_global_references_carry_per_turn_expected_responses():
    truth = {"expected_response": "A\nB", "turns": [{"input": "q1", "expected_response": "A"},
                                                      {"input": "q2", "expected_response": "B"}]}
    refs = global_eval.reference_inputs("s", truth, ["t1", "t2"])
    assert [(r["context"]["spanContext"]["traceId"], r["expectedResponse"]["text"])
            for r in refs] == [("t1", "A"), ("t2", "B")]
    # turns that do not line up with the agent traces: the joined truth on the last trace
    refs = global_eval.reference_inputs("s", truth, ["t1", "t2", "t3"])
    assert [(r["context"]["spanContext"]["traceId"], r["expectedResponse"]["text"])
            for r in refs] == [("t3", "A\nB")]


def test_global_responses_are_cached_but_failures_are_not(tmp_path):
    cache = RecordCache(tmp_path)
    entry = _entry(TWO_TURN)
    client = _Evaluate()
    records, skipped = global_eval.run(client, cache, [entry],
                                       ["Builtin.Helpfulness", "cust_x"], {}, reps=2,
                                       log=lambda _m: None)
    assert client.calls == 2 and skipped[0]["id"] == "cust_x"
    assert {r["rep"] for r in records} == {0, 1} and len(records) == 4
    global_eval.run(client, cache, [entry], ["Builtin.Helpfulness"], {}, reps=2,
                    log=lambda _m: None)
    assert client.calls == 2  # served from .eval-cache/global
    failing = _Evaluate(fail=True)
    out, _ = global_eval.run(failing, RecordCache(tmp_path / "f"), [entry],
                             ["Builtin.Refusal"], {}, log=lambda _m: None)
    assert out[0]["error_type"] == "RequestFailed"
    global_eval.run(failing, RecordCache(tmp_path / "f"), [entry], ["Builtin.Refusal"], {},
                    log=lambda _m: None)
    assert failing.calls == 2


def test_global_a2a_workaround_sends_own_service_spans_and_marks_rows(tmp_path):
    entry = _entry(TWO_TURN)
    own = next(x for x in entry.records if x["group"] == SPANS)
    peer = copy.deepcopy(own)
    peer["record"].update(spanId="dddddddddddddddd", name="execute_tool calculator")
    peer["record"].setdefault("resource", {"attributes": {}})["attributes"][
        "service.name"] = "probe_a2a_peer_725d62.DEFAULT"
    entry.records.append(peer)
    ids = lambda spans: {r["spanId"] for r in spans}  # noqa: E731
    assert global_eval.foreign_spans(entry) == 1
    assert "dddddddddddddddd" in ids(global_eval.session_spans(entry))  # the SDK payload
    assert "dddddddddddddddd" not in ids(global_eval.session_spans(entry, own_only=True))
    assert len(global_eval.session_spans(entry, own_only=True)) == len(
        global_eval.session_spans(entry)) - 1
    records, _ = global_eval.run(_Evaluate(), RecordCache(tmp_path), [entry],
                                 ["Builtin.Helpfulness"], {}, own_spans=True,
                                 log=lambda _m: None)
    assert records and all(r["own_spans_only"] for r in records)
    plain, _ = global_eval.run(_Evaluate(), RecordCache(tmp_path / "p"), [_entry(TWO_TURN)],
                               ["Builtin.Helpfulness"], {}, own_spans=True, log=lambda _m: None)
    assert not any(r["own_spans_only"] for r in plain)  # no peer spans: SDK payload unchanged


def test_global_results_file_hook(tmp_path):
    path = tmp_path / "g.jsonl"
    path.write_text(json.dumps({"evaluatorId": "Builtin.ToolSelectionAccuracy", "rep": 1,
                                "evaluationResults": [{
                                    "evaluatorId": "Builtin.ToolSelectionAccuracy",
                                    "value": 0.0, "label": "No",
                                    "context": {"spanContext": {"sessionId": "s",
                                                                "spanId": "sp"}}}]}) + "\n\n")
    (rec,) = global_eval.load_results(str(path))
    assert (rec["level"], rec["span_id"], rec["rep"], rec["score"]) == ("TOOL_CALL", "sp", 1, 0.0)


# ── judge plumbing (no model call) ──────────────────────────────────────────

def test_judge_definitions_ground_truth_and_scrub():
    defs, skipped = judge.definitions(
        ["Builtin.Helpfulness", "Builtin.TrajectoryAnyOrderMatch", "cust_a", "cust_code",
         "cust_gone"],
        [{"id": "cust_a", "name": "A", "definition": "judge", "level": "TRACE",
          "instructions": "Rate {assistant_turn}", "rating_scale": [], "model_id": None,
          "base_evaluator_id": None, "lambda_arn": None, "lambda_timeout_s": None},
         {"id": "cust_code", "name": "C", "definition": "code", "level": "TRACE",
          "instructions": None, "rating_scale": [], "model_id": None,
          "base_evaluator_id": None, "lambda_arn": "arn:x", "lambda_timeout_s": 5}])
    assert [d["id"] for d in defs] == ["Builtin.Helpfulness", "Builtin.TrajectoryAnyOrderMatch",
                                       "cust_a"]
    assert defs[2]["instructions"] == "Rate {assistant_turn}"
    assert {s["id"] for s in skipped} == {"cust_code", "cust_gone"}
    payload = {"run": {"scenario_map": {"s1": {"scenario_id": "sc1"}, "s2": {}}},
               "dataset": {"items": [{"scenario_id": "sc1", "turns": [
                   {"input": "q", "expected_response": "a"}], "assertions": ["ok"]}]}}
    truth = judge.ground_truth(payload, ["s1", "s2"])
    assert list(truth) == ["s1"] and truth["s1"]["expected_response"] == "a"
    assert judge.ground_truth(None, ["s1"]) == {}
    judge._SECRETS.append("sk-secret-value")
    try:
        assert judge.scrub("key=sk-secret-value!") == "key=***!" and judge.scrub(3) == 3
    finally:
        judge._SECRETS.remove("sk-secret-value")


# ── report ──────────────────────────────────────────────────────────────────

def _report(m1_rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"matched": 1, "denominator": 1, "rate": 1.0, "completed": 1, "failed_turns": 0,
               "failed_wrong": 0, "leaks": 0, "extra": 0, "sessions": 1, "sessions_ok": 1}
    out = {"generated_at": "2026-10-23T00:00:00+00:00", "round": "1",
           "code": {"sha": "149e23642989abcd", "dirty": False, "backend": "/b"},
           "input": {"run": "fb8a7e230b11", "host": "default", "sessions": ["s1"],
                     "log_group": "/g", "source": "cache", "cache": "/c"},
           "m1": {"both": {"summary": summary, "sessions": [
               {"session_id": "s1" * 8, "order_ok": True, "error": None, "rows": m1_rows}]}},
           "comparisons": {"local_vs_global": {"Builtin.Helpfulness": {
               "level": "TRACE", "ours": 7, "reference": 7, "matched": 6,
               "coverage_vs_reference": 0.857, "labelled": 6, "label_agree": 3,
               "label_rate": 0.5, "scored": 6, "mad": 0.111, "bias": 0.05, "m4_scored": 6,
               "m4_turns": 7, "m4": 0.857, "turn_mean_gap": None, "stamped_elsewhere": 1}}}}
    out["ledger_row"] = report.ledger_row(out)
    return out


def test_report_ledger_row_has_the_ledger_columns():
    row = _report([])["ledger_row"]
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert len(cells) == 12
    assert cells[:3] == ["1", "run fb8a7e230b11 (1 sessions, default)", "eval-wb @149e2364"]
    assert cells[5:9] == ["1/1 both", "Helpfulness 3/6", "Helpfulness 0.11", "Helpfulness 6/7"]


def test_report_table_escapes_cells_and_write_scrubs_secrets(tmp_path):
    row = {"turn": 1, "trace_id": "6adb6b831eba", "status": "match", "prompt_ok": True,
           "tools_ok": False, "extracted_answer": "a|b\nc sk-secret-value",
           "origin_answer": "x" * 200, "extracted_tools": ["kb"], "origin_tools": []}
    data = _report([row])
    table = report.m1_table(data["m1"]["both"]["sessions"])
    assert "a\\|b c" in table and "1 / 0 DIFF" in table and "…" in table
    judge._SECRETS.append("sk-secret-value")
    try:
        md, js = report.write(data, tmp_path / "r")
    finally:
        judge._SECRETS.remove("sk-secret-value")
    assert "sk-secret-value" not in md.read_text() + js.read_text()
    assert "## M1 extraction vs origin logs — mode `both`" in md.read_text()
    assert json.loads(js.read_text())["ledger_row"] == data["ledger_row"]


# ── end to end, offline, over the committed real records ────────────────────

def test_workbench_end_to_end_on_the_real_record_fixtures(tmp_path, capsys):
    rc = eval_workbench.main([
        "--fixtures", *map(str, FIXTURES), "--offline", "--cache-dir", str(tmp_path / "cache"),
        "--modes", "both,runtime_only", "--report-dir", str(tmp_path / "report"),
        "--round", "t"])
    assert rc == 0
    out = json.loads((tmp_path / "report" / "report.json").read_text(encoding="utf-8"))
    assert set(out["m1"]) == {"both", "runtime_only"}
    for block in out["m1"].values():
        by_session = {s["session_id"][:12]: s for s in block["sessions"]}
        assert set(by_session) == {"257f17b16cbb", "35f49b824328", "3cdd38248181"}
        for sid in ("257f17b16cbb", "35f49b824328"):  # completed turns: all match
            assert [r["status"] for r in by_session[sid]["rows"]] == ["match"] * len(
                by_session[sid]["rows"])
        paipai = by_session["3cdd38248181"]["rows"]
        assert paipai[0]["status"] == "match" and paipai[0]["completed"]
        assert all(not r["completed"] and r["status"] != "LEAK" for r in paipai[1:])
        assert block["summary"]["completed"] == 4 and block["summary"]["leaks"] == 0
        assert block["summary"]["matched"] == 4
    assert out["code"]["backend"] == str(BACKEND)
    assert (tmp_path / "report" / "report.md").is_file()
    assert "M1 [both] 4/" in capsys.readouterr().out


class _Ssm:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
        self.calls.append(Name)
        return {"Parameter": {"Value": " sk-offline-judge-key \n"}}


class _CnSession:
    def __init__(self) -> None:
        self.ssm, self.clients = _Ssm(), []

    def client(self, name: str) -> Any:
        self.clients.append(name)
        assert name == "ssm", f"--offline must not open {name}"
        return self.ssm


def test_offline_judge_still_reads_the_bedrock_key_from_cn_ssm(tmp_path, monkeypatch):
    # --offline = no Logs / ledger fetch; the judges' key read still needs the CN session
    from app.services import aws_clients
    from app.services.model_credential import provider_row

    for name in {aws_clients.GLOBAL_BEDROCK_BEARER_ENV, provider_row("bedrock")["api_key_env"]}:
        monkeypatch.setenv(name, "before")  # restored by monkeypatch after the test
    opened: list[_CnSession] = []
    monkeypatch.setattr(fetch, "cn_session", lambda: opened.append(_CnSession()) or opened[-1])
    try:
        rc = eval_workbench.main([
            "--fixtures", str(BACKEND / "tests" / "fixtures" / TWO_TURN), "--offline", "--judge",
            "--evaluators", "Builtin.TrajectoryAnyOrderMatch", "--cache-dir",
            str(tmp_path / "c"), "--report-dir", str(tmp_path / "r")])
    finally:
        judge._SECRETS[:] = [x for x in judge._SECRETS if x != "sk-offline-judge-key"]
    assert rc == 0
    assert len(opened) == 1 and opened[0].clients == ["ssm"]  # lazily, SSM only
    assert opened[0].ssm.calls == [judge.BEDROCK_KEY_PARAMETER]
    import os
    assert os.environ[aws_clients.GLOBAL_BEDROCK_BEARER_ENV] == "sk-offline-judge-key"
    out = json.loads((tmp_path / "r" / "report.json").read_text(encoding="utf-8"))
    assert out["judge"]["evaluators"] == ["Builtin.TrajectoryAnyOrderMatch"]
    assert out["judge"]["records"]


def test_offline_without_judge_never_opens_a_cn_session(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "cn_session", lambda: pytest.fail("no CN session expected"))
    rc = eval_workbench.main(["--fixtures", str(BACKEND / "tests" / "fixtures" / PREAMBLE),
                              "--offline", "--cache-dir", str(tmp_path / "c"),
                              "--report-dir", str(tmp_path / "r")])
    assert rc == 0


def test_workbench_offline_without_cache_refuses(tmp_path, capsys):
    rc = eval_workbench.main(["--sessions", "abcdef123456", "--log-group", "/g", "--offline",
                              "--cache-dir", str(tmp_path)])
    assert rc == 2 and "not cached" in capsys.readouterr().err


def test_workbench_args_validation():
    with pytest.raises(SystemExit):
        eval_workbench.parse_args([])
    with pytest.raises(SystemExit):
        eval_workbench.parse_args(["--sessions", "a"])
    args = eval_workbench.parse_args(["--run", "r1", "--global", "--sessions", "a,b", "c"])
    assert args.judge and args.sessions == ["a", "b", "c"] and args.modes == ["both"]


def test_cache_entries_are_not_mutated_by_a_run():
    entry = _entry(PREAMBLE)
    before = copy.deepcopy(entry.records)
    reconcile.reconcile_session(entry, "both")
    assert entry.records == before
