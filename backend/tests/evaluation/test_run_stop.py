"""Operator stop for evaluation runs (SE-009).

Three exits, one route: StopBatchEvaluation once a batch exists on AWS, a
local queue cancel while the run is still pending, and an in-process stop flag
while the dataset is being replayed (no batch yet). AWS STOPPED becomes the
terminal ledger status ``stopped`` — never ``failed`` — with the scores the
batch had already produced.
"""

import threading
import time
from unittest.mock import MagicMock

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import agentcore_eval as ac
from app.evaluation.models import EvalRun
from app.evaluation.queue import EvalRunQueue
from tests.conftest import ws_ctx

PARTIAL_RESULT = {
    "status": "STOPPED",
    "evaluationResults": {
        "evaluatorSummaries": [
            {"evaluatorId": "Builtin.Correctness", "statistics": {"averageScore": 0.75}},
            {"evaluatorId": "Builtin.Helpfulness", "statistics": {}},  # never judged
        ]
    },
}


def make_run(**fields) -> str:
    db = SessionLocal()
    try:
        defaults = dict(
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id="a1",
            agent_name="eval-target",
            mode="evaluators",
            evaluators=["Builtin.Correctness", "Builtin.Helpfulness"],
            session_ids=["s1", "s2"],
        )
        run = EvalRun(**{**defaults, **fields})
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def get_run(run_id: str) -> EvalRun:
    db = SessionLocal()
    try:
        return db.get(EvalRun, run_id)
    finally:
        db.close()


def wait_status(run_id: str, target: str, timeout: float = 5.0) -> EvalRun:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = get_run(run_id)
        if run.status == target:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {target}: {get_run(run_id).status}")


# ─── route: stop with a batch on AWS ─────────────────────────────────────────
def test_eval_stop_evaluating_run_calls_stop_batch_evaluation(client, monkeypatch):
    data = MagicMock()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    run_id = make_run(status="evaluating", batch_eval_id="be-stop-1")

    res = client.post(f"/api/eval/runs/{run_id}/stop")

    assert res.status_code == 202, res.text
    data.stop_batch_evaluation.assert_called_once_with(batchEvaluationId="be-stop-1")
    body = res.json()
    assert body["id"] == run_id
    # The row follows the poller (STOPPING → STOPPED); the pending stop is
    # visible to the console so STOP is not offered twice.
    assert body["status"] == "evaluating"
    assert body["stop_requested"] is True
    assert client.get(f"/api/eval/runs/{run_id}").json()["stop_requested"] is True


def test_eval_stop_completed_run_is_409_conflict(client, monkeypatch):
    data = MagicMock()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    for status in ("completed", "failed", "stopped"):
        run_id = make_run(status=status, batch_eval_id="be-done")
        res = client.post(f"/api/eval/runs/{run_id}/stop")
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "run.not_active"
        assert status in res.json()["message"]
    data.stop_batch_evaluation.assert_not_called()


def test_eval_stop_unknown_run_is_404(client):
    res = client.post("/api/eval/runs/nope/stop")
    assert res.status_code == 404
    assert res.json()["code"] == "run.not_found"


# ─── route: queued run is cancelled locally, never reaches AWS ───────────────
def test_eval_stop_cancels_queued_run_without_aws(client, monkeypatch):
    data = MagicMock()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    # One slot, held by a blocker, so the run under test stays pending.
    queue = EvalRunQueue(max_concurrency=1)
    monkeypatch.setattr(svc, "run_queue", queue)
    import app.evaluation.routers as routers

    monkeypatch.setattr(routers, "run_queue", queue)
    release = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        release.wait(5)

    queue.submit("blocker", blocker)
    assert started.wait(2)
    run_id = make_run(status="queued")
    executed = MagicMock()
    assert queue.submit(run_id, executed) == 1
    assert client.get(f"/api/eval/runs/{run_id}").json()["queue_position"] == 1

    res = client.post(f"/api/eval/runs/{run_id}/stop")

    assert res.status_code == 202, res.text
    assert res.json()["status"] == "stopped"
    assert res.json()["error"] == "stopped by operator"
    assert res.json()["queue_position"] is None
    assert queue.state()["queued"] == []  # the banner counts only active runs
    data.stop_batch_evaluation.assert_not_called()
    assert not svc.stop_requested(run_id)  # nothing left to read the flag

    release.set()
    queue._queue.join()
    executed.assert_not_called()  # the worker dropped it on dequeue
    assert get_run(run_id).status == "stopped"
    assert queue.state()["running"] == []


# ─── replay in progress, no batch yet: the flag stops it before StartBatch ───
def test_eval_stop_during_replay_never_starts_batch(monkeypatch):
    data = MagicMock()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(svc, "_wait_for_fresh_telemetry", MagicMock())
    run_id = make_run(status="queued", session_ids=[])
    workspace = ws_ctx()
    calls = {"n": 0}

    def invoke_runtime_text(client, arn, prompt, session_id=None, runtime_user_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # Operator clicks STOP while the first prompt is in flight: the run
            # is `invoking` with no batch_eval_id, so only the flag can be set.
            run = svc.request_stop(run_id, workspace=workspace)
            assert run.status == "invoking"
        return {"text": "42", "session_id": f"sess-{calls['n']}"}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke_runtime_text)

    svc.execute_run(
        run_id,
        workspace=workspace,
        agent_arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/rt-1",
        method="zip_runtime",
        service_name="rt.DEFAULT",
        log_group="/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT",
        items=[{"prompt": "1"}, {"prompt": "2"}, {"prompt": "3"}],
        evaluators=["Builtin.Correctness"],
        mode="evaluators",
        wait_seconds=0,
    )

    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.error == "stopped by operator"
    assert calls["n"] == 1  # the loop checked the flag before prompt 2
    data.start_batch_evaluation.assert_not_called()
    data.stop_batch_evaluation.assert_not_called()
    assert not svc.stop_requested(run_id)  # flag cleared with the run


def test_eval_stop_requested_right_after_batch_start_is_forwarded(monkeypatch):
    """The narrow race: STOP lands after the last flag check but before the
    batch id is on the row — the worker forwards it to AWS itself."""
    data = MagicMock()
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: data)
    run_id = make_run(status="queued")
    workspace = ws_ctx()

    def start_batch(**kwargs):
        svc.request_stop(run_id, workspace=workspace)  # row: evaluating, no batch id
        return {"batchEvaluationId": "be-race"}

    data.start_batch_evaluation.side_effect = start_batch
    data.get_batch_evaluation.side_effect = [{"status": "STOPPING"}, PARTIAL_RESULT]
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)

    svc.execute_run(
        run_id,
        workspace=workspace,
        agent_arn="arn",
        method="zip_runtime",
        service_name="rt.DEFAULT",
        log_group="lg",
        items=[],
        evaluators=["Builtin.Correctness"],
        mode="evaluators",
        wait_seconds=0,
        existing_session_ids=["s1", "s2"],
    )

    data.stop_batch_evaluation.assert_called_once_with(batchEvaluationId="be-race")
    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.batch_eval_id == "be-race"
    assert run.scores == [{"evaluatorId": "Builtin.Correctness", "score": 0.75}]


# ─── poller: STOPPING keeps polling, STOPPED is terminal + partial scores ─────
def test_eval_stop_poller_treats_stopping_as_running_and_stopped_as_terminal(monkeypatch):
    data = MagicMock()
    data.get_batch_evaluation.side_effect = [
        {"status": "IN_PROGRESS"},
        {"status": "STOPPING"},
        PARTIAL_RESULT,
    ]
    result = ac.poll_batch_evaluation(data, batch_id="be-1", interval=0)
    assert result["status"] == "STOPPED"
    assert data.get_batch_evaluation.call_count == 3


def test_eval_stop_finish_maps_stopped_to_stopped_with_partial_scores():
    run_id = make_run(status="evaluating", batch_eval_id="be-2")
    svc._finish_from_result(run_id, "evaluators", PARTIAL_RESULT)
    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.scores == [{"evaluatorId": "Builtin.Correctness", "score": 0.75}]
    assert run.error == "stopped by operator"


def test_eval_stop_finish_keeps_insight_trees_and_error_details():
    run_id = make_run(status="evaluating", batch_eval_id="be-3", mode="insights")
    svc._finish_from_result(
        run_id,
        "insights",
        {
            "status": "STOPPED",
            "errorDetails": ["2 of 5 sessions were not evaluated"],
            "failureAnalysisResult": {"failures": [{"category": "Tool misuse"}]},
        },
    )
    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.insights == {"failures": [{"category": "Tool misuse"}]}
    assert run.error == "stopped by operator — 2 of 5 sessions were not evaluated"


def test_eval_stop_reconcile_after_restart_lands_stopped(monkeypatch):
    """An evaluating run whose batch was stopped while the backend was down is
    re-polled on startup and settles as `stopped`, not `failed`."""
    run_id = make_run(status="evaluating", batch_eval_id="be-4")
    monkeypatch.setattr(svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(
        svc.ac, "poll_batch_evaluation", lambda client, batch_id, max_polls=60: PARTIAL_RESULT
    )
    assert run_id in svc.resume_interrupted_runs()
    run = wait_status(run_id, "stopped")
    assert run.scores == [{"evaluatorId": "Builtin.Correctness", "score": 0.75}]


def test_eval_stop_stopped_runs_are_terminal_for_resume():
    run_id = make_run(status="stopped", batch_eval_id="be-5", error="stopped by operator")
    assert svc.resume_interrupted_runs() == []
    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.error == "stopped by operator"


def test_eval_stop_wrapper_shape():
    client = MagicMock()
    client.stop_batch_evaluation.return_value = {
        "batchEvaluationId": "be-6",
        "status": "STOPPING",
    }
    assert ac.stop_batch_evaluation(client, batch_id="be-6")["status"] == "STOPPING"
    client.stop_batch_evaluation.assert_called_once_with(batchEvaluationId="be-6")
