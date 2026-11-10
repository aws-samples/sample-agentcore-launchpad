"""Operator stop for evaluation runs (SE-009) on the worker (N12 E1-4).

Three exits, one route: a local queue cancel while the run is still pending, the cancel
marker once a job is dispatched (the worker stops at its next checkpoint and writes
``done.json`` STOPPED), and ``StopRuntimeSession`` after the grace period when the worker
never answers. STOPPED becomes the terminal ledger status ``stopped`` — never ``failed`` —
with the scores already ingested.
"""

import threading
import time
from unittest.mock import MagicMock

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.jobs import JobDone, JobStatus
from app.evaluation.models import EvalRun
from app.evaluation.queue import EvalRunQueue
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor, strip_ci
from tests.evaluation.test_runs_flow import GROUP, make_agent, stub_environment


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


def _fake_executor(monkeypatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: fake)
    monkeypatch.setattr(svc, "POLL_INTERVAL_S", 0.02)
    return fake


# ─── route: stop with a dispatched job ───────────────────────────────────────
def test_eval_stop_evaluating_run_writes_the_cancel_marker(client, monkeypatch):
    fake = _fake_executor(monkeypatch)
    run_id = make_run(status="evaluating", batch_eval_id="job-stop-1")

    res = client.post(f"/api/eval/runs/{run_id}/stop")

    assert res.status_code == 202, res.text
    assert fake.cancelled == ["job-stop-1"]
    body = res.json()
    assert body["id"] == run_id
    # The row follows the poller (the worker's STOPPED); the pending stop is
    # visible to the console so STOP is not offered twice.
    assert body["status"] == "evaluating"
    assert body["stop_requested"] is True
    assert client.get(f"/api/eval/runs/{run_id}").json()["stop_requested"] is True
    svc.stop_flags.clear(run_id)


def test_eval_stop_completed_run_is_409_conflict(client, monkeypatch):
    fake = _fake_executor(monkeypatch)
    for status in ("completed", "failed", "stopped"):
        run_id = make_run(status=status, batch_eval_id="job-done")
        res = client.post(f"/api/eval/runs/{run_id}/stop")
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "run.not_active"
        assert status in res.json()["message"]
    assert fake.cancelled == []


def test_eval_stop_unknown_run_is_404(client):
    res = client.post("/api/eval/runs/nope/stop")
    assert res.status_code == 404
    assert res.json()["code"] == "run.not_found"


# ─── route: queued run is cancelled locally, never dispatched ────────────────
def test_eval_stop_cancels_queued_run_without_a_job(client, monkeypatch):
    fake = _fake_executor(monkeypatch)
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
    assert fake.submitted == [] and fake.cancelled == []
    assert not svc.stop_requested(run_id)  # nothing left to read the flag

    release.set()
    queue._queue.join()
    executed.assert_not_called()  # the worker dropped it on dequeue
    assert get_run(run_id).status == "stopped"
    assert queue.state()["running"] == []


# ─── replay in progress: the marker stops the worker at its next checkpoint ─────
def test_eval_stop_during_replay_stops_the_worker_between_invocations(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="stop-replay-agent")
    db.close()
    env, calls = stub_environment(monkeypatch)
    run_id = make_run(status="queued", session_ids=[], agent_id=agent.id)
    workspace = ws_ctx(id=DEFAULT_WORKSPACE_ID)
    real_invoke = env.worker_env.invoke

    def invoke(prompt, session_id, session_spec):
        if calls["n"] == 0:
            # Operator clicks STOP while the first prompt is in flight: the job is
            # dispatched, so the marker is written for it.
            run = svc.request_stop(run_id, workspace=workspace)
            assert run.status == "invoking"
        return real_invoke(prompt, session_id, session_spec)

    env.worker_env.invoke = invoke
    plan = svc.plan_run(
        run_id, workspace=workspace, agent_arn=agent.arn, agent_id=agent.id, protocol="http",
        service_name="rt.DEFAULT", log_group=GROUP, log_groups=["aws/spans", GROUP],
        items=[{"prompt": "1"}, {"prompt": "2"}, {"prompt": "3"}],
        evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
        existing_session_ids=None, time_range=None, insights=None, actor_model_id=None,
        runtime_user_id=None,
    )
    svc.execute_run(run_id, workspace=workspace, plan=plan)

    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.error == "stopped by operator"
    assert calls["n"] == 1  # the worker checked the marker before prompt 2
    assert env.store.get(f"evaluations/{run_id}/cancel") == b"cancel"
    done = env.executor.done(workspace, run_id)
    assert done.state == "STOPPED"
    assert not svc.stop_requested(run_id)  # flag cleared with the run


def test_eval_stop_before_dispatch_never_submits(monkeypatch):
    fake = _fake_executor(monkeypatch)
    run_id = make_run(status="queued", session_ids=[])
    svc.stop_flags.request(run_id)
    plan = svc.RunPlan(run_id=run_id, job_type="batch_eval", target={}, sessions=[],
                       replay=False, wait={})
    svc.execute_run(run_id, workspace=ws_ctx(), plan=plan)
    assert get_run(run_id).status == "stopped" and fake.submitted == []
    assert not svc.stop_requested(run_id)


# ─── poller: the worker's STOPPED is terminal + partial scores ────────────────
def test_eval_stop_finish_maps_stopped_to_stopped_with_partial_scores():
    from app.evaluation import results_store

    run_id = make_run(status="evaluating", batch_eval_id="job-2")
    with SessionLocal() as db:
        results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id, records=[
            {"session_id": "s1", "evaluator_id": "Builtin.Correctness", "score": 0.75},
            {"session_id": "s1", "evaluator_id": "Builtin.Helpfulness", "score": None,
             "error_type": "RuntimeError", "error_message": "stopped mid-judgement"},
        ])
    svc._finish(run_id, "evaluators", [JobDone(state="STOPPED", error="stopped by operator",
                                               result_count=2)],
                workspace=ws_ctx(), executor=FakeExecutor(), job_ids=["job-2"])
    run = get_run(run_id)
    assert run.status == "stopped"
    assert strip_ci(run.scores) == [{"evaluatorId": "Builtin.Correctness", "score": 0.75,
        "count": 1, "n": 1, "unit": "turn"}]
    assert run.error == "stopped by operator"


def test_eval_stop_finish_keeps_insight_trees():
    run_id = make_run(status="evaluating", batch_eval_id="job-3", mode="insights")
    fake = FakeExecutor()
    svc._finish(
        run_id, "insights",
        [JobDone(state="STOPPED", error="stopped by operator",
                 summary={"insights": {"failures": [{"category": "Tool misuse"}]}})],
        workspace=ws_ctx(), executor=fake, job_ids=["job-3"],
    )
    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.insights == {"failures": [{"category": "Tool misuse"}]}
    assert run.error == "stopped by operator"


def test_eval_stop_reconcile_after_restart_lands_stopped(monkeypatch):
    """An evaluating run whose job was stopped while the backend was down is re-attached
    on startup and settles as `stopped`, not `failed`."""
    fake = _fake_executor(monkeypatch)
    run_id = make_run(status="evaluating", batch_eval_id="job-4")
    fake.complete("job-4", [{"session_id": "s1", "evaluator_id": "Builtin.Correctness",
                             "score": 0.75}])
    fake.dones["job-4"] = JobDone(state="STOPPED", error="stopped by operator", result_count=1)
    fake.statuses["job-4"] = JobStatus(phase="evaluating", state="STOPPED")
    assert run_id in svc.resume_interrupted_runs()
    run = wait_status(run_id, "stopped")
    assert strip_ci(run.scores) == [{"evaluatorId": "Builtin.Correctness", "score": 0.75,
        "count": 1, "n": 1, "unit": "turn"}]


def test_eval_stop_stopped_runs_are_terminal_for_resume():
    run_id = make_run(status="stopped", batch_eval_id="job-5", error="stopped by operator")
    assert svc.resume_interrupted_runs() == []
    run = get_run(run_id)
    assert run.status == "stopped"
    assert run.error == "stopped by operator"


def test_eval_stop_grace_period_ends_the_worker_session(monkeypatch):
    """A worker that never writes STOPPED after the cancel marker is stopped hard
    (StopRuntimeSession) once the grace period is over; the row is `stopped`."""
    fake = _fake_executor(monkeypatch)
    stopped: list[str] = []
    fake.stop_session = lambda ws, job_id: stopped.append(job_id) or True  # type: ignore[attr-defined]
    clock = {"t": 1000.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["t"])
    monkeypatch.setattr(svc, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + 30.0))
    run_id = make_run(status="queued", session_ids=[])
    fake.statuses[run_id] = JobStatus(phase="evaluating")
    svc.stop_flags.request(run_id)
    plan = svc.RunPlan(run_id=run_id, job_type="batch_eval", target={},
                       sessions=[{"session_id": "s1"}], replay=False, wait={})
    svc.stop_flags.clear(run_id)
    # the stop lands right after dispatch
    real_submit = fake.submit

    def submit(ws, job):
        out = real_submit(ws, job)
        svc.stop_flags.request(run_id)
        return out

    fake.submit = submit  # type: ignore[method-assign]
    svc.execute_run(run_id, workspace=ws_ctx(), plan=plan)
    assert fake.cancelled == [run_id] and stopped == [run_id]
    run = get_run(run_id)
    assert run.status == "stopped" and run.error == "stopped by operator"
