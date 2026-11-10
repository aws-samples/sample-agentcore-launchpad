"""N17-obs327 — a code (Lambda) evaluator receives Global AgentCore's envelope.

Global (code-based-evaluators.html) invokes the Lambda with ``{schemaVersion, evaluatorId,
evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs,
evaluationTarget}``. These tests replay a REAL redacted session (``eval_corpus``, 3 turns, a
gateway tool call in each) through the production spans provider and engine and invoke an
in-process Lambda written only against the Global docs (``global_code_evaluator.py``, the
AgentCore SDK's ``@custom_code_based_evaluator``) — it must run unchanged. The reviewed CN
handler (``lambda_runtime/handler.py``) keeps working on the same envelope.
"""

from __future__ import annotations

import json
import types
from typing import Any

import pytest

from app.assistant.lambda_runtime import handler as cn_handler
from app.evaluation import jobs
from app.services.strands_eval import jobs_batch, jobs_online
from tests.evaluation import global_code_evaluator
from tests.evaluation.code_eval_support import (
    CORPUS,
    GROUPS,
    SID,
    TOOL,
    TURN_TRACES,
    InProcessLambda,
)
from tests.evaluation.code_eval_support import definition as _definition
from tests.evaluation.code_eval_support import run_corpus as _run
from tests.evaluation.corpus_support import GroupLogs


@pytest.fixture(autouse=True)
def _clear_seen():
    global_code_evaluator.SEEN.clear()


def test_a_lambda_written_for_global_scores_every_turn_on_real_records():
    client = InProcessLambda(global_code_evaluator.handler)
    records = _run("TRACE", client)
    assert [r["trace_id"] for r in records] == TURN_TRACES
    assert [(r["label"], r["score"], r["error_type"]) for r in records] == [("Pass", 1.0, None)] * 3
    assert [r["explanation"] for r in records] == [f"{t}: 1 tool call(s)" for t in TURN_TRACES]
    seen = global_code_evaluator.SEEN
    assert [s.target_trace_id for s in seen] == TURN_TRACES
    assert {(s.evaluation_level, s.evaluator_id, s.evaluator_name, s.schema_version)
            for s in seen} == {("TRACE", "ev_code01", "tool_counter", "1.0")}
    assert all(s.target_span_id is None for s in seen)


def test_session_spans_are_the_raw_adot_documents_of_the_session():
    client = InProcessLambda(global_code_evaluator.handler)
    _run("SESSION", client)
    spans = client.events[0]["evaluationInput"]["sessionSpans"]
    raw = [x["record"] for x in CORPUS["records"]]
    # every span record and every content (body) record, ids as ADOT wrote them
    assert len(spans) == len(raw)
    assert sorted(json.dumps(s, sort_keys=True) for s in spans) == sorted(
        json.dumps(r, sort_keys=True) for r in raw)
    assert all(":" not in s["spanId"] for s in spans)
    assert {s["traceId"] for s in spans} == set(TURN_TRACES)


def test_session_level_target_is_none_and_references_carry_every_turn():
    client = InProcessLambda(global_code_evaluator.handler)
    truth = {"assertions": ["looks the employee up", "states the balance"],
             "expected_trajectory": [TOOL, TOOL, TOOL],
             "turns": [{"input": "a", "expected_response": "r0"},
                       {"input": "b", "expected_response": None},
                       {"input": "c", "expected_response": "r2"}]}
    records = _run("SESSION", client, truth)
    event = client.events[0]
    assert event["evaluationLevel"] == "SESSION" and event["evaluationTarget"] is None
    assert event["evaluationReferenceInputs"] == [
        {"context": {"spanContext": {"sessionId": SID}},
         "assertions": [{"text": "looks the employee up"}, {"text": "states the balance"}],
         "expectedTrajectory": {"toolNames": [TOOL, TOOL, TOOL]}},
        {"context": {"spanContext": {"sessionId": SID, "traceId": TURN_TRACES[0]}},
         "expectedResponse": {"text": "r0"}},
        {"context": {"spanContext": {"sessionId": SID, "traceId": TURN_TRACES[2]}},
         "expectedResponse": {"text": "r2"}},
    ]
    assert [(r["label"], r["score"]) for r in records] == [("Pass", 1.0)]


def test_trace_level_references_are_the_session_entry_plus_the_target_turn():
    client = InProcessLambda(global_code_evaluator.handler)
    truth = {"assertions": ["polite"], "expected_trajectory": None,
             "turns": [{"input": "a", "expected_response": "r0"},
                       {"input": "b", "expected_response": "r1"},
                       {"input": "c", "expected_response": "r2"}]}
    _run("TRACE", client, truth)
    session_ref = {"context": {"spanContext": {"sessionId": SID}},
                   "assertions": [{"text": "polite"}]}
    for n, event in enumerate(client.events):
        assert event["evaluationTarget"] == {"traceIds": [TURN_TRACES[n]]}
        assert event["evaluationReferenceInputs"] == [
            session_ref,
            {"context": {"spanContext": {"sessionId": SID, "traceId": TURN_TRACES[n]}},
             "expectedResponse": {"text": f"r{n}"}},
        ]


def test_the_reviewed_cn_handler_resolves_its_packaged_evaluator_by_name(monkeypatch):
    """The package's ``rules.json`` is keyed by the evaluator NAME; the row id the worker
    used to send as ``evaluator`` named nothing in a package (UNKNOWN_EVALUATOR)."""
    rules = {"version": 1, "evaluators": {
        "tool_counter": {"checks": [
            {"id": "one_lookup", "type": "tool_count", "tool": TOOL, "min": 1, "max": 1}]},
        "other": {"checks": [{"id": "never", "type": "output_contains", "text": "zzz"}]}}}
    monkeypatch.setattr(cn_handler, "load_rules", lambda: rules)
    client = InProcessLambda(cn_handler.lambda_handler)
    records = _run("TRACE", client)
    assert [(r["label"], r["score"], r["error_type"]) for r in records] == [("PASS", 1.0, None)] * 3
    for event in client.events:
        assert event["evaluator"] == "tool_counter"  # what an already-deployed handler reads
        assert event["evaluationInput"]["actual_trajectory"] == [TOOL]  # the turn's own call
    # a Global-shaped event (no CN ``evaluator`` key) resolves by ``evaluatorName``
    event = {k: v for k, v in client.events[0].items() if k != "evaluator"}
    assert cn_handler.lambda_handler(event, None)["label"] == "PASS"


# ── the worker paths (batch / online / SCORE NOW) hand the same envelope ─────────────────

class Sink:
    def __init__(self) -> None:
        self.job = types.SimpleNamespace(job_id="j-code")
        self.rows: list[dict] = []
        self.finished: tuple | None = None

    def cancelled(self) -> bool:
        return False

    def status(self, *_a: Any, **_kw: Any) -> None:
        pass

    def invocation(self, _record: Any) -> None:
        pass

    def results(self, records: list[dict]) -> None:
        self.rows.extend(records)

    def result(self, record: dict) -> None:
        self.rows.append(record)

    def finish(self, state: str, error: Any = None, summary: Any = None) -> None:
        self.finished = (state, error, summary or {})


def test_the_batch_worker_sends_session_documents_and_the_dataset_truth():
    client = InProcessLambda(global_code_evaluator.handler)
    spec = {
        "run_id": "run-code", "target": {"runtime_arn": "rt", "protocol": "HTTP"},
        "sessions": [{"session_id": SID, "scenario_id": "s1", "replay": False}],
        "replay": False,
        "wait": {"seconds": 0, "log_groups": GROUPS, "content_log_group": GROUPS[0],
                 "spans_source": "both", "lookback_hours": 48},
        "evaluator_defs": [_definition("SESSION")],
        "ground_truth": {SID: {"assertions": [], "expected_trajectory": [TOOL] * 3,
                               "turns": []}},
        "judge_model_id": "judge",
    }
    env = jobs_batch.WorkerEnv(logs_client=GroupLogs(CORPUS["records"]), data_client=None,
                               judge_factory=lambda m, **kw: None, lambda_client=client,
                               code_evaluators_enabled=True, sleep=lambda s: None)
    sink = Sink()
    jobs_batch.run_job(jobs.JobSpec(job_id="j-code", job_type="batch_eval", attempt=1,
                                    workspace={}, sink={}, spec=spec), sink, env)
    assert sink.finished[0] == "COMPLETED", sink.finished
    assert [(r["label"], r["score"]) for r in sink.rows] == [("Pass", 1.0)]
    event = client.events[0]
    assert len(event["evaluationInput"]["sessionSpans"]) == len(CORPUS["records"])
    assert event["evaluationReferenceInputs"] == [
        {"context": {"spanContext": {"sessionId": SID}},
         "expectedTrajectory": {"toolNames": [TOOL] * 3}}]


def test_the_online_worker_sends_session_documents():
    client = InProcessLambda(global_code_evaluator.handler)
    job = types.SimpleNamespace(job_id="oe-cfg-1", spec={
        "config_id": "cfg", "mode": "scores", "sessions": [{"session_id": SID}],
        "log_groups": GROUPS, "spans_source": "both", "lookback_hours": 24,
        "evaluator_defs": [_definition("TRACE")], "judge_model_id": "judge"})
    sink = Sink()
    jobs_online.run(job, sink, logs_client=GroupLogs(CORPUS["records"]),
                    judge_factory=lambda m, **kw: None, lambda_client=client,
                    sleep=lambda s: None)
    assert [(r["trace_id"], r["label"]) for r in sink.rows] == [(t, "Pass") for t in TURN_TRACES]
    assert all(len(e["evaluationInput"]["sessionSpans"]) == len(CORPUS["records"])
               for e in client.events)
    assert [e["evaluatorName"] for e in client.events] == ["tool_counter"] * 3


def test_online_definitions_carry_the_evaluator_name():
    from app.core.db import SessionLocal
    from app.evaluation.models import EvalEvaluator
    from app.evaluation.online_scheduler import evaluator_defs

    with SessionLocal() as db:
        db.add(EvalEvaluator(id="ev_named1", workspace_id="ws-x", name="tool_counter",
                             level="TRACE", definition="code", rating_scale=[],
                             lambda_arn="arn:aws-cn:lambda:cn-northwest-1:1:function:f",
                             lambda_timeout_s=30))
        db.commit()
        assert evaluator_defs(db, "ws-x", ["ev_named1"])[0]["name"] == "tool_counter"


def test_score_now_and_the_in_process_engine_send_the_same_envelope(monkeypatch):
    from app.core.config import get_settings
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.evaluation.models import EvalEvaluator
    from app.evaluation.strands_engine import StrandsEngine
    from tests.conftest import ws_ctx

    monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "1")
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            db.add(EvalEvaluator(id="ev_code01", workspace_id=DEFAULT_WORKSPACE_ID,
                                 name="tool_counter", level="TRACE", definition="code",
                                 rating_scale=[], lambda_arn=_definition("TRACE")["lambda_arn"],
                                 lambda_timeout_s=30))
            db.commit()
        client = InProcessLambda(global_code_evaluator.handler)
        engine = StrandsEngine(logs_client=lambda _ws: GroupLogs(CORPUS["records"]),
                               lambda_client=lambda _ws: client,
                               judge_factory=lambda _m, **_kw: None, sleep=lambda _s: None)
        records = engine.evaluate_sessions(
            ws_ctx(id=DEFAULT_WORKSPACE_ID, region="cn-northwest-1"), source="run",
            owner_id="run1", session_ids=[SID], log_groups=GROUPS, evaluator_ids=["ev_code01"],
            ground_truth=None, judge_model_id=None, stop=None, progress=None, persist=False)
    finally:
        get_settings.cache_clear()
    assert [(r["trace_id"], r["label"]) for r in records] == [(t, "Pass") for t in TURN_TRACES]
    assert [e["evaluationTarget"] for e in client.events] == [
        {"traceIds": [t]} for t in TURN_TRACES]
    assert all(len(e["evaluationInput"]["sessionSpans"]) == len(CORPUS["records"])
               for e in client.events)
