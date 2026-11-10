"""N12 E1-3b — `RuntimeExecutor`: InvokeAgentRuntime on the worker + S3 polling."""

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import jobs
from app.evaluation.runtime_executor import (
    DISPATCH_ARTIFACT,
    RuntimeExecutor,
    resolve_worker,
    worker_arn,
)
from app.models.ledger import Agent
from app.system_agents.presets import EVAL_WORKER, EVAL_WORKER_KEY
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeS3

WORKER = ("arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/"
          "launchpad_eval_worker_abc123-xyz")
# the worker a later install created — the preset row carries it, the resource map
# (written at bootstrap) still says WORKER
CURRENT_WORKER = ("arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/"
                  "launchpad_eval_worker_a1b2c3-EXAMPLE001")
BUCKET = "artifacts"


class FakeData:
    def __init__(self, ack=None, content_type="application/json"):
        self.calls: list[dict] = []
        self.stopped: list[dict] = []
        self.ack = ack if ack is not None else {"state": "ACCEPTED"}
        self.content_type = content_type

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        ack = dict(self.ack)
        ack.setdefault("job_id", json.loads(kwargs["payload"])["job_id"])
        body = json.dumps(ack)
        if self.content_type == "text/event-stream":
            body = f"data: {body}\n"

        encoded = body.encode()

        class _B:
            def read(self):
                return encoded

        return {"response": _B(), "contentType": self.content_type}

    def stop_runtime_session(self, **kwargs):
        self.stopped.append(kwargs)
        return {"runtimeSessionId": kwargs["runtimeSessionId"], "statusCode": 200}


def _ws(**resources):
    return ws_ctx({"artifacts_bucket": BUCKET, "eval_worker_runtime_arn": WORKER, **resources},
                  region="cn-northwest-1")


def _job(job_id="job1", attempt=1, job_type="batch_eval"):
    return jobs.JobSpec(job_id=job_id, job_type=job_type, attempt=attempt,
                        workspace={"id": "default", "account_id": "111122223333",
                                   "region": "cn-northwest-1"},
                        sink={"bucket": BUCKET, "prefix": jobs.job_prefix(job_id, attempt)},
                        spec={"run_id": "r1"})


def executor(data=None, s3=None, resolver=lambda ws: WORKER):
    """An executor whose worker lookup is pinned (the invoke / S3 mechanics under test);
    ``resolver=None`` uses the real ledger lookup (the N12-bug6 tests below)."""
    data = data or FakeData()
    s3 = s3 or FakeS3()
    ex = RuntimeExecutor(data_client_factory=lambda ws: data, s3_client_factory=lambda ws: s3,
                         worker_resolver=resolver)
    return ex, data, s3


def preset_row(status="active", arn=CURRENT_WORKER, workspace_id=DEFAULT_WORKSPACE_ID) -> str:
    """The eval-worker preset row as install / uninstall leave it."""
    db = SessionLocal()
    try:
        row = Agent(workspace_id=workspace_id, name=EVAL_WORKER.name, method="zip_runtime",
                    status=status, arn=arn, resource_id=arn.rsplit("/", 1)[-1] if arn else None,
                    spec={"name": EVAL_WORKER.name}, system_key=EVAL_WORKER_KEY)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_submit_invokes_the_worker_with_a_fresh_long_session_and_the_payload():
    ex, data, s3 = executor()
    assert ex.submit(_ws(), _job()) == "job1"
    call = data.calls[0]
    assert call["agentRuntimeArn"] == WORKER and call["qualifier"] == "DEFAULT"
    assert len(call["runtimeSessionId"]) >= 33
    assert call["runtimeSessionId"].startswith("evaljob-job1-a1-")
    assert call["runtimeUserId"] == "launchpad-eval"  # the worker's token → bearer exchange
    assert call["contentType"] == call["accept"] == "application/json"
    assert json.loads(call["payload"]) == _job().to_payload()
    dispatch = json.loads(s3.objects[(BUCKET, jobs.artifact_key("job1", 1, DISPATCH_ARTIFACT))])
    assert dispatch["runtime_session_id"] == call["runtimeSessionId"]
    assert dispatch["worker_arn"] == WORKER and dispatch["job_type"] == "batch_eval"


def test_each_attempt_gets_its_own_session():
    ex, data, s3 = executor()
    ex.submit(_ws(), _job(attempt=1))
    ex.submit(_ws(), _job(attempt=2))
    sids = [c["runtimeSessionId"] for c in data.calls]
    assert sids[0] != sids[1] and sids[1].startswith("evaljob-job1-a2-")
    assert ex.dispatch(_ws(), "job1")["runtime_session_id"] == sids[1]  # the latest attempt


def test_submit_accepts_an_sse_framed_ack():
    ex, data, s3 = executor(FakeData(content_type="text/event-stream"))
    assert ex.submit(_ws(), _job()) == "job1"


def test_rejected_reserved_kind_surfaces_the_protocol_code():
    ex, data, _ = executor(FakeData(ack={"state": "REJECTED", "code": "evaluation.kind_unsupported",
                                         "message": "reserved"}))
    with pytest.raises(AppError) as exc:
        # the backend refuses reserved kinds itself (select_executor); force the worker path
        job = _job()
        job.job_type = "batch_eval"
        ex.submit(_ws(), job)
    assert exc.value.code == "evaluation.kind_unsupported" and exc.value.status_code == 400


def test_an_empty_or_unknown_ack_is_a_worker_rejection():
    ex, _, _ = executor(FakeData(ack={"state": "WEIRD"}))
    with pytest.raises(AppError) as exc:
        ex.submit(_ws(), _job())
    assert exc.value.code == "evaluation.worker_rejected" and exc.value.status_code == 502


def test_reserved_kinds_never_reach_the_worker():
    ex, data, _ = executor()
    with pytest.raises(jobs.KindUnsupportedError):
        ex.submit(_ws(), _job(job_type="skill_train"))
    assert data.calls == []


def test_missing_bucket_is_503_worker_missing():
    ex, data, _ = executor()
    with pytest.raises(AppError) as exc:
        ex.submit(ws_ctx({"eval_worker_runtime_arn": WORKER}), _job())
    assert exc.value.code == "evaluation.worker_missing" and exc.value.status_code == 503
    assert exc.value.detail == {"missing": "artifacts_bucket"}
    assert data.calls == []


# ─── N12-bug6: the worker is resolved from the preset row on every submit ─────────
def test_a_stale_resource_map_arn_alone_is_not_an_installed_worker():
    """After UNINSTALL the resource map still carries the deleted Runtime's ARN; the
    submit must be refused (503) instead of dispatching to it."""
    ex, data, _ = executor(resolver=None)
    with pytest.raises(AppError) as exc:
        ex.submit(_ws(), _job())  # _ws() carries eval_worker_runtime_arn=WORKER
    assert exc.value.code == "evaluation.worker_missing" and exc.value.status_code == 503
    assert exc.value.detail == {"worker_status": "not_installed"}
    assert data.calls == []
    assert worker_arn(_ws()) is None and resolve_worker(_ws()).status == "not_installed"


def test_the_installed_preset_row_names_the_worker_not_the_resource_map():
    preset_row(status="active", arn=CURRENT_WORKER)
    ex, data, s3 = executor(resolver=None)
    assert resolve_worker(_ws()) == (CURRENT_WORKER, "active")
    assert ex.assert_ready(_ws()) == CURRENT_WORKER
    assert ex.submit(_ws(), _job()) == "job1"
    assert data.calls[0]["agentRuntimeArn"] == CURRENT_WORKER
    dispatch = json.loads(s3.objects[(BUCKET, jobs.artifact_key("job1", 1, DISPATCH_ARTIFACT))])
    assert dispatch["worker_arn"] == CURRENT_WORKER


@pytest.mark.parametrize("status", ["deploying", "uninstalling", "failed"])
def test_a_worker_that_is_not_active_is_not_ready(status):
    preset_row(status=status, arn=CURRENT_WORKER if status != "deploying" else None)
    ex, data, _ = executor(resolver=None)
    with pytest.raises(AppError) as exc:
        ex.assert_ready(_ws())
    assert exc.value.code == "evaluation.worker_missing" and exc.value.status_code == 503
    assert exc.value.detail == {"worker_status": status}
    with pytest.raises(AppError):
        ex.submit(_ws(), _job())
    assert data.calls == []


def test_a_deleted_row_counts_as_not_installed():
    preset_row(status="deleted", arn=WORKER)
    assert resolve_worker(_ws()) == (None, "not_installed")


def test_the_worker_of_another_workspace_does_not_count():
    preset_row(status="active", arn=CURRENT_WORKER, workspace_id="other-ws")
    assert resolve_worker(_ws()) == (None, "not_installed")


def _write(s3, key, payload):
    s3.objects[(BUCKET, key)] = (json.dumps(payload) if not isinstance(payload, str)
                                 else payload).encode()


def test_status_done_and_ingest_read_the_latest_attempts_artefacts():
    ex, _, s3 = executor()
    ws = _ws()
    assert ex.status(ws, "job1") is None and ex.done(ws, "job1") is None
    assert ex.ingest(ws, "job1") == []
    _write(s3, jobs.status_key("job1", 1), jobs.JobStatus(phase="waiting").to_dict())
    assert ex.status(ws, "job1").phase == "waiting" and ex.status(ws, "job1").state == "RUNNING"
    # attempt 2 supersedes attempt 1
    progress = {"done": 1, "total": 3, "message": "evaluating 1/3"}
    _write(s3, jobs.status_key("job1", 2),
           jobs.JobStatus(phase="evaluating", progress=progress).to_dict())
    records = [{"id": "a", "session_id": "s1", "evaluator_id": "e", "score": 1.0},
               {"id": "b", "session_id": "s2", "evaluator_id": "e", "score": 0.5}]
    _write(s3, jobs.results_key("job1", 2), "\n".join(json.dumps(r) for r in records) + "\n")
    status = ex.status(ws, "job1")
    assert status.phase == "evaluating" and status.progress["message"] == "evaluating 1/3"
    assert ex.ingest(ws, "job1") == records
    assert ex.ingest(ws, "job1", since=1) == records[1:]  # incremental, idempotent
    assert ex.ingest(ws, "job1", since=5) == []
    _write(s3, jobs.done_key("job1", 2), jobs.JobDone(state="COMPLETED", result_count=2,
                                                      summary={"scores": []}).to_dict())
    assert ex.done(ws, "job1").state == "COMPLETED" and ex.done(ws, "job1").result_count == 2
    assert ex.status(ws, "job1").state == "COMPLETED"


def test_stale_detection_uses_the_status_timestamp():
    from datetime import UTC, datetime, timedelta

    ex, _, s3 = executor()
    old = jobs.JobStatus(phase="evaluating")
    old.updated_at = (datetime.now(UTC) - timedelta(seconds=900)).isoformat()
    _write(s3, jobs.status_key("job1", 1), old.to_dict())
    status = ex.status(_ws(), "job1")
    assert jobs.is_stale(status, now=datetime.now(UTC), stale_s=600)
    assert not jobs.is_stale(status, now=datetime.now(UTC), stale_s=1800)
    assert jobs.is_stale(None, now=datetime.now(UTC), stale_s=600)


def test_cancel_writes_the_marker_and_stop_session_ends_the_dispatched_session():
    ex, data, s3 = executor()
    ws = _ws()
    ex.submit(ws, _job())
    ex.cancel(ws, "job1")
    assert s3.objects[(BUCKET, jobs.cancel_key("job1"))] == b"cancel"
    assert ex.stop_session(ws, "job1") is True
    stop = data.stopped[0]
    assert stop["agentRuntimeArn"] == WORKER
    assert stop["runtimeSessionId"] == data.calls[0]["runtimeSessionId"]


def test_stop_session_without_a_dispatch_or_with_a_gone_session_is_false():
    from botocore.exceptions import ClientError

    ex, data, s3 = executor()
    ws = _ws()
    assert ex.stop_session(ws, "never-dispatched") is False
    ex.submit(ws, _job())

    def gone(**kwargs):
        raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "StopRuntimeSession")

    data.stop_runtime_session = gone
    assert ex.stop_session(ws, "job1") is False


def test_invocations_and_artifacts_are_readable():
    ex, _, s3 = executor()
    ws = _ws()
    _write(s3, jobs.invocations_key("job1", 1),
           json.dumps({"session_id": "s1", "status": "ok"}) + "\n")
    _write(s3, jobs.artifact_key("job1", 1, "insights.json"), {"failures": []})
    assert ex.invocations(ws, "job1") == [{"session_id": "s1", "status": "ok"}]
    assert ex.artifact(ws, "job1", "insights.json") == {"failures": []}
    assert ex.artifact(ws, "job1", "missing.json") is None


def test_select_executor_hands_out_the_registered_runtime_executor(monkeypatch):
    from app.evaluation import executor as executor_mod

    ex, _, _ = executor()
    monkeypatch.setattr(executor_mod, "_runtime_executor", ex)

    class S:
        eval_executor = "runtime"
        run_mode = "prod"

    assert executor_mod.select_executor(S(), job_type="batch_eval") is ex


def test_invocations_satisfies_the_traffic_run_hook():
    """W-E3's ``optimization.traffic._invocations`` calls ``executor.invocations(ws, job_id)``
    positionally and expects ``invocations.jsonl`` rows (or ``None``). Both executors must
    accept exactly that shape."""
    from app.evaluation.executor import InProcessExecutor, MemoryStore
    from app.optimization import traffic

    ex, _, s3 = executor()
    ws = _ws()
    _write(s3, jobs.invocations_key("j1", 1),
           json.dumps({"session_id": "s1", "status": "ok"}) + "\n"
           + json.dumps({"session_id": "s2", "status": "error"}) + "\n")
    rows = traffic._invocations(ex, ws, "j1", records=[])
    assert [r["session_id"] for r in rows] == ["s1", "s2"]
    # The in-process executor answers through the same StoreReader. ``[]`` is authoritative
    # ("nothing was invoked", so every planned assignment is reconciled away) — the hook
    # never returns ``None``, so traffic's records fallback is only for hook-less executors.
    store = MemoryStore()
    local = InProcessExecutor(store, runner=lambda job, sink: None)
    assert traffic._invocations(local, ws, "nope", records=[{"session_id": "s9"}]) == []
    store.put(jobs.invocations_key("j2", 1),
              (json.dumps({"session_id": "s3", "status": "ok"}) + "\n").encode())
    assert traffic._invocations(local, ws, "j2", records=[]) == [
        {"session_id": "s3", "status": "ok"}]
