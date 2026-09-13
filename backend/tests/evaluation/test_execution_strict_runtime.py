"""Strict Runtime text evidence for procedure steps (SE-046, correction 3).

Raw wire bodies go through the REAL decoder (`stream_runtime_events` /
`invoke_runtime_text`) and the real runner (`execute_run`), with only the boto3
client and the telemetry wait doubled. Ordinary callers (Chat / public API /
legacy replays) keep the lenient default and their exact call shape.
"""

import copy
import io
import json
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import execution as ex
from app.evaluation.models import EvalRun
from app.services.agentcore import runtime as rt
from tests.conftest import ws_ctx

ARN = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/rt-1"
SID = "s" * 40

NEG_SCENARIO = {
    "scenario_id": "neg",
    "turns": [{"input": "prompt-0"}],
    "metadata": {"launchpad_execution": {"version": 1, "steps": [
        {"turn": 0, "actor": "A", "session": "s"}],
        "checks": [{"id": "no_leak", "type": "not_contains", "turn": 0, "text": "amber"}]}},
}


def client_with(body: bytes, content_type: str = "application/json") -> MagicMock:
    client = MagicMock()
    client.invoke_agent_runtime.return_value = {
        "response": io.BytesIO(body), "contentType": content_type}
    return client


def strict(body: bytes, content_type: str = "application/json") -> str:
    return rt.invoke_runtime_text(client_with(body, content_type), ARN, "p",
                                  session_id=SID, strict_text=True)["text"]


def lenient(body: bytes, content_type: str = "application/json") -> str:
    return rt.invoke_runtime_text(client_with(body, content_type), ARN, "p", session_id=SID)["text"]


def sse(*payloads: object) -> bytes:
    return "".join(f"data: {json.dumps(p)}\n\n" for p in payloads).encode()


# ─── decoder: strict mode ────────────────────────────────────────────────────
@pytest.mark.parametrize("body", [
    b'{"result":null}', b'{"event":"complete","result":null}', b"{}", b"",
    b'{"result":""}', b'{"result":"   "}', b"null",
    sse({"event": "heartbeat"}, {"event": "tool", "name": "calc", "id": "t1"}),
    sse({"contentBlockStart": {"start": {"toolUse": {"name": "calc", "toolUseId": "t1"}}}}),
    sse({"event": "delta", "text": None}, {"event": "complete", "result": None}),
])
def test_strict_null_or_absent_text_is_no_answer(body):
    text = strict(body)
    assert ex.response_text({"text": text}) is None  # missing evidence, never "None"


@pytest.mark.parametrize("body, field", [
    (b'{"event":"delta","text":42}', "text"),
    (b'{"event":"delta","text":true}', "text"),
    (b'{"event":"delta","text":["a"]}', "text"),
    (b'{"event":"delta","text":{"a":1}}', "text"),
    (b'{"event":"complete","result":42}', "result"),
    (b'{"result":false}', "result"),
    (b'{"result":[1,2]}', "result"),
    (b'{"result":{"text":"nested"}}', "result"),
    (b"42", "body"),  # JSON number body
    (b"true", "body"),
    (b"[1]", "body"),
    (sse({"contentBlockDelta": {"delta": {"text": 17}}}), "contentBlockDelta.delta.text"),
    # valid partial text, then a wrong-typed chunk: still a protocol error
    (sse({"event": "delta", "text": "am"}, {"event": "delta", "text": 42}), "text"),
    (sse({"contentBlockDelta": {"delta": {"text": "am"}}},
         {"contentBlockDelta": {"delta": {"text": ["ber"]}}}), "contentBlockDelta.delta.text"),
])
def test_strict_wrong_typed_text_is_a_protocol_error(body, field):
    with pytest.raises(rt.RuntimeTextProtocolError) as exc:
        strict(body)
    message = str(exc.value)
    assert f"'{field}'" in message and "42" not in message and "nested" not in message


@pytest.mark.parametrize("body, content_type, expected", [
    (b'{"result":"None"}', "application/json", "None"),
    (b'{"result":"42"}', "application/json", "42"),
    (b'{"result":"false"}', "application/json", "false"),
    (b'{"result":"{\\"a\\": 1}"}', "application/json", '{"a": 1}'),
    (b'{"event":"complete","result":"None"}', "application/json", "None"),
    (b"42", "text/plain; charset=utf-8", "42"),  # plain text, not a JSON number
    (b"None", "text/plain", "None"),
    (b"true", "text/plain", "true"),
    (b"hello there", "text/plain", "hello there"),
    (b'"42"', "application/json", "42"),  # JSON string body
    (sse({"event": "delta", "text": "am"}, {"event": "delta", "text": "ber"}), "text/event-stream",
     "amber"),
    (sse({"event": "delta", "text": "amber"}, {"event": "complete", "result": None}),
     "text/event-stream", "amber"),
    (sse({"event": "delta", "text": "amber"}, {"event": "complete", "result": ""}),
     "text/event-stream", "amber"),
    (sse({"event": "delta", "text": "amber"}, {"event": "complete"}), "text/event-stream", "amber"),
    # deltas + duplicated full result → deduplicated, as before
    (sse({"event": "delta", "text": "amber"}, {"event": "complete", "result": "amber"}),
     "text/event-stream", "amber"),
    (sse({"contentBlockDelta": {"delta": {"text": "am"}}},
         {"contentBlockDelta": {"delta": {"text": "ber"}}}), "text/event-stream", "amber"),
    (sse({"event": "heartbeat"}, {"event": "delta", "text": "amber"}), "text/event-stream",
     "amber"),
])
def test_strict_keeps_legitimate_text_verbatim(body, content_type, expected):
    assert strict(body, content_type) == expected


# ─── decoder: default callers unchanged ──────────────────────────────────────
@pytest.mark.parametrize("body, expected", [
    (b'{"result":null}', "None"),
    (b'{"event":"complete","result":null}', "None"),
    (b'{"event":"delta","text":42}', "42"),
    (b"42", "42"),
    (b'{"result":"amber"}', "amber"),
    (sse({"event": "delta", "text": "am"}, {"event": "delta", "text": "ber"}), "amber"),
])
def test_default_decoding_is_unchanged_for_ordinary_callers(body, expected):
    assert lenient(body) == expected


def test_default_call_shape_has_no_strict_flag():
    """Chat / public API / legacy replays call invoke_runtime_text exactly as
    before — the strict flag is opt-in and off by default."""
    import inspect

    assert inspect.signature(rt.invoke_runtime_text).parameters["strict_text"].default is False
    assert inspect.signature(rt.stream_runtime_events).parameters["strict_text"].default is False


# ─── actual runner over the real decoder ─────────────────────────────────────
class FakeCloud:
    """boto3 data-plane double answering raw bodies; records call shapes."""

    def __init__(self, bodies, content_type="application/json"):
        self.bodies = list(bodies)
        self.content_type = content_type
        self.calls: list[dict] = []
        self.batches: list[dict] = []

    def invoke_agent_runtime(self, **kw):
        payload = json.loads(kw["payload"])
        self.calls.append({"prompt": payload["prompt"], "actor_id": payload.get("actor_id"),
                           "session_id": kw["runtimeSessionId"]})
        body = self.bodies[min(len(self.calls) - 1, len(self.bodies) - 1)]
        return {"response": io.BytesIO(body), "contentType": self.content_type}

    def start_batch_evaluation(self, **kw):
        self.batches.append(kw)
        return {"batchEvaluationId": "fake-batch"}

    def get_batch_evaluation(self, **kw):
        return {"status": "COMPLETED", "evaluationResults": {"evaluatorSummaries": []}}


def lowlevel_run(items, cloud, monkeypatch) -> EvalRun:
    db = SessionLocal()
    row = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a" * 32, agent_name="offline",
                  evaluators=["Builtin.Correctness"], status="queued")
    db.add(row)
    db.commit()
    rid = row.id
    db.close()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: cloud)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", MagicMock())
    svc.execute_run(rid, workspace=ws_ctx(), agent_arn=ARN, method="zip_runtime",
                    service_name="offline.DEFAULT", log_group="/fake", items=items,
                    evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
                    agent_id="a" * 32)
    db = SessionLocal()
    try:
        return db.get(EvalRun, rid)
    finally:
        db.close()


@pytest.mark.parametrize("body", [b'{"result":null}', b'{"event":"complete","result":null}',
                                  b"{}", b'{"result":"  "}'])
def test_runner_null_result_is_missing_evidence(body, monkeypatch):
    cloud = FakeCloud([body])
    run = lowlevel_run([copy.deepcopy(NEG_SCENARIO)], cloud, monkeypatch)
    assert run.status == "completed"  # invoke succeeded, only the evidence is missing
    blob = run.execution
    assert blob["steps"][0]["status"] == "empty"
    assert blob["checks"][0]["outcome"] == "error" and blob["check_status"] == "error"


@pytest.mark.parametrize("body", [b'{"event":"delta","text":42}', b'{"result":false}', b"42",
                                  sse({"event": "delta", "text": "am"},
                                      {"event": "delta", "text": 42})])
def test_runner_wrong_typed_text_fails_the_step_and_no_batch(body, monkeypatch):
    cloud = FakeCloud([body])
    run = lowlevel_run([copy.deepcopy(NEG_SCENARIO)], cloud, monkeypatch)
    assert run.status == "failed" and "non-text" in run.error
    assert cloud.batches == []
    blob = run.execution
    assert blob["steps"][0]["status"] == "failed" and "non-text" in blob["steps"][0]["error"]
    assert blob["checks"][0]["outcome"] == "error" and blob["check_status"] == "error"
    assert cloud.calls[0]["actor_id"].startswith("a" * 32 + "__eval__")


def test_runner_legitimate_textual_none_and_plain_42_stay_text(monkeypatch):
    for body, ctype in ((b'{"result":"None"}', "application/json"), (b"42", "text/plain")):
        cloud = FakeCloud([body], content_type=ctype)
        run = lowlevel_run([copy.deepcopy(NEG_SCENARIO)], cloud, monkeypatch)
        assert run.status == "completed", run.error
        assert run.execution["steps"][0]["status"] == "ok"
        assert run.execution["checks"][0]["outcome"] == "pass"
        assert len(cloud.batches) == 1


def test_runner_ordinary_scenario_next_to_procedure_stays_lenient(monkeypatch):
    """The plain scenario in a mixed dataset still decodes leniently and is
    invoked with the bare default actor and no strict flag."""
    plain = {"scenario_id": "plain", "turns": [{"input": "hello"}]}
    cloud = FakeCloud([b'{"result":null}', b'{"result":"amber"}'])
    run = lowlevel_run([plain, copy.deepcopy(NEG_SCENARIO)], cloud, monkeypatch)
    assert run.status == "completed", run.error
    assert cloud.calls[0]["actor_id"] == "default"
    assert len(run.session_ids) == 2 and run.execution["checks"][0]["outcome"] == "fail"
