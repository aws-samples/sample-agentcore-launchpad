"""N12 E1-5 — SCORE NOW: `POST /api/observability/sessions/{id}/evaluate` on the engine.

The only in-process evaluation path (CA-8): one session, ≤ 5 evaluators, `persist=False`,
the session's log groups discovered from its own span records, the baseline result row
keys. Hermetic: a `FakeEngine` in the engine slot, a `FakeLogs` answering the group query.
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import engine
from app.evaluation.models import EvalEvaluator
from app.services import observability
from tests.evaluation.fakes import FakeEngine, FakeLogs

SID = "sess-" + "a" * 40
GROUP = "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"


@pytest.fixture
def fake_engine(monkeypatch):
    previous = engine.engine_or_none()
    fake = FakeEngine(scores={"Builtin.Helpfulness": 0.833, "Builtin.Correctness": 1.0})
    engine.set_engine(fake)
    logs = FakeLogs({"stats count(*) as records by resource.attributes.aws.log.group.names": [
        {"log_group": GROUP, "records": "7"}, {"log_group": "aws/spans", "records": "7"}]})
    monkeypatch.setattr(observability, "logs_client", lambda ws: logs)
    yield fake, logs
    engine.set_engine(previous)


def test_score_now_returns_the_baseline_shape(client, fake_engine):
    fake, logs = fake_engine
    res = client.post(f"/api/observability/sessions/{SID}/evaluate", json={
        "evaluator_ids": ["Builtin.Helpfulness", "Builtin.Correctness", "Builtin.Helpfulness"],
    })
    assert res.status_code == 200, res.text
    body = res.json()
    # the baseline keys + the CN-additive extraction reconciliation (eval accuracy item 1)
    assert set(body) == {"session_id", "range", "span_count", "results", "extraction_check"}
    assert body["session_id"] == SID and body["range"] == "24h" and body["span_count"] == 14
    assert [r["evaluator_id"] for r in body["results"]] == ["Builtin.Helpfulness",
                                                            "Builtin.Correctness"]
    row = body["results"][0]
    assert set(row) == {"evaluator_id", "evaluator_name", "evaluator_arn", "value", "label",
                        "explanation", "span_context", "token_usage", "error_code",
                        "error_message"}
    assert row["value"] == 0.833 and row["label"] == "pass" and row["explanation"] == "scripted"
    assert row["evaluator_name"] == "Helpfulness"
    assert row["evaluator_arn"].startswith("arn:aws-cn:bedrock-agentcore:")
    assert row["evaluator_arn"].endswith("evaluator/Builtin.Helpfulness")
    assert row["error_code"] is None and row["error_message"] is None
    # the engine call: ondemand, one session, NOT persisted, de-duplicated evaluators
    (call,) = fake.calls
    assert call["source"] == "ondemand" and call["owner_id"] == SID
    assert call["session_ids"] == [SID] and call["persist"] is False
    assert call["evaluator_ids"] == ["Builtin.Helpfulness", "Builtin.Correctness"]
    assert call["log_groups"] == [GROUP, "aws/spans"]
    # the group discovery ran over the last 24 h with the session filter
    (query,) = logs.started
    assert f'attributes.session.id = "{SID}"' in query["queryString"]
    assert query["endTime"] - query["startTime"] == 24 * 3600
    assert "logGroupName" not in query  # a SOURCE query — runtime groups + aws/spans


def test_range_narrows_the_window(client, fake_engine):
    _fake, logs = fake_engine
    res = client.post(f"/api/observability/sessions/{SID}/evaluate", json={
        "evaluator_ids": ["Builtin.Helpfulness"], "range": "1h"})
    assert res.status_code == 200 and res.json()["range"] == "1h"
    assert logs.started[0]["endTime"] - logs.started[0]["startTime"] == 3600


def test_more_than_five_evaluators_is_422(client, fake_engine):
    fake, _logs = fake_engine
    ids = ["Builtin.Helpfulness", "Builtin.Correctness", "Builtin.Faithfulness",
           "Builtin.Coherence", "Builtin.Conciseness", "Builtin.Refusal"]
    res = client.post(f"/api/observability/sessions/{SID}/evaluate", json={"evaluator_ids": ids})
    assert res.status_code == 422
    assert fake.calls == []
    res = client.post(f"/api/observability/sessions/{SID}/evaluate", json={"evaluator_ids": []})
    assert res.status_code == 422


def test_service_refuses_more_than_five_distinct_ids(fake_engine):
    from app.core.errors import AppError
    from tests.conftest import ws_ctx

    ids = ["Builtin.Helpfulness", "Builtin.Correctness", "Builtin.Faithfulness",
           "Builtin.Coherence", "Builtin.Conciseness", "Builtin.Refusal"]
    with pytest.raises(AppError) as exc:
        observability.evaluate_session(SID, "24h", ids, ws_ctx(id=DEFAULT_WORKSPACE_ID))
    assert exc.value.code == "observability.too_many_evaluators"


def test_unknown_session_id_shape_is_422_and_no_spans_is_409(client, fake_engine, monkeypatch):
    fake, _logs = fake_engine
    assert client.post("/api/observability/sessions/short/evaluate",
                       json={"evaluator_ids": ["Builtin.Helpfulness"]}).status_code == 422
    monkeypatch.setattr(observability, "logs_client", lambda ws: FakeLogs({}))
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert res.status_code == 409
    assert res.json()["code"] == "observability.session_spans_missing"
    assert res.json()["detail"]["hint"]
    assert fake.calls == []


def test_another_principals_private_session_is_404(client, fake_engine, monkeypatch):
    from app.assistant.sessions import PrivateSessions

    monkeypatch.setattr(PrivateSessions, "visible", lambda self, session_id: False)
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert res.status_code == 404
    assert res.json()["code"] == "observability.session_not_found"


def test_unknown_evaluator_is_refused_before_any_span_read(client, fake_engine):
    fake, logs = fake_engine
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["cust_missing"]})
    assert res.status_code == 422 and res.json()["code"] == "run.evaluator_not_found"
    assert logs.started == [] and fake.calls == []


def test_a_ground_truth_judge_cannot_score_a_live_session(client, fake_engine):
    fake, logs = fake_engine
    with SessionLocal() as db:
        db.add(EvalEvaluator(id="cust_gt", workspace_id=DEFAULT_WORKSPACE_ID, name="gt",
                             level="TRACE", definition="judge", model_id="global.zai.glm-5.3",
                             instructions="Compare {assistant_turn} with {expected_response}",
                             rating_scale=[]))
        db.add(EvalEvaluator(id="cust_live", workspace_id=DEFAULT_WORKSPACE_ID, name="live",
                             level="TRACE", definition="judge", model_id="global.zai.glm-5.3",
                             instructions="Rate the tone of {assistant_turn}", rating_scale=[]))
        db.commit()
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["cust_gt"]})
    assert res.status_code == 422
    assert res.json()["code"] == "observability.evaluator_needs_ground_truth"
    assert res.json()["detail"] == {"evaluators": {"cust_gt": ["expected_response"]}}
    assert fake.calls == [] and logs.started == []
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["cust_live", "Builtin.Helpfulness"]})
    assert res.status_code == 200
    assert [r["evaluator_id"] for r in res.json()["results"]] == ["cust_live",
                                                                  "Builtin.Helpfulness"]
    assert res.json()["results"][0]["evaluator_name"] == "cust_live"


def test_a_failed_judgement_is_an_error_row_not_a_5xx(client, fake_engine):
    fake, _logs = fake_engine

    def evaluate_sessions(ws, **kwargs):
        return [{"session_id": SID, "trace_id": "t" * 32, "evaluator_id": "Builtin.Helpfulness",
                 "level": "TRACE", "score": None, "label": None, "explanation": None,
                 "error_type": "RuntimeError", "error_message": "judge timed out"}]

    fake.evaluate_sessions = evaluate_sessions  # type: ignore[method-assign]
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert res.status_code == 200
    row = res.json()["results"][0]
    assert row["value"] is None and row["error_code"] == "RuntimeError"
    assert row["error_message"] == "judge timed out"
    assert row["span_context"] == {"traceId": "t" * 32}


def test_runtime_logs_mode_evaluates_from_the_runtime_group_only(client, fake_engine, monkeypatch):
    from app.core.config import get_settings

    fake, _logs = fake_engine
    monkeypatch.setenv("LAUNCHPAD_OBSERVABILITY_SPANS_SOURCE", "runtime_logs")
    get_settings.cache_clear()
    try:
        res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                          json={"evaluator_ids": ["Builtin.Helpfulness"]})
        assert res.status_code == 200
        assert fake.calls[0]["log_groups"] == [GROUP]
    finally:
        get_settings.cache_clear()


def test_without_an_engine_the_feature_is_unavailable(client, fake_engine):
    engine.set_engine(None)
    res = client.post(f"/api/observability/sessions/{SID}/evaluate",
                      json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert res.status_code == 503 and res.json()["code"] == "feature.unavailable"
