"""N12-bug6 — job submission refuses 503 ``evaluation.worker_missing`` while the
evaluation worker preset is not installed / active, resolved from the preset row at
request time: a stale ``eval_worker_runtime_arn`` in the resource map (left behind by
UNINSTALL) does not count. SCORE NOW and the in-process executor are untouched."""

import threading
import time
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import executor as ex
from app.evaluation.models import EvalDataset, EvalRecommendation, EvalRun
from app.evaluation.runtime_executor import RuntimeExecutor
from app.optimization import canary_service as canary_svc
from tests.conftest import set_default_resources
from tests.evaluation.fakes import FakeS3
from tests.evaluation.test_runs_flow import make_agent
from tests.test_runtime_executor import CURRENT_WORKER, WORKER, FakeData, preset_row

RESOURCES = {
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-test-execution",
    "artifacts_bucket": "launchpad-artifacts-test",
    "eval_worker_runtime_arn": WORKER,  # stale: written by bootstrap, never cleared
}


@pytest.fixture
def worker_runtime(client, monkeypatch):
    """The production wiring: ``eval_executor=runtime`` with a RuntimeExecutor whose AWS
    clients are fakes, the default workspace carrying a stale worker ARN."""
    set_default_resources(RESOURCES)
    data = FakeData()
    runtime = RuntimeExecutor(data_client_factory=lambda ws: data,
                              s3_client_factory=lambda ws: FakeS3())
    ex.set_runtime_executor(runtime)
    monkeypatch.setattr(get_settings(), "eval_executor", "runtime")
    monkeypatch.setattr(svc.rt, "get_runtime",
                        lambda client, rid: {"agentRuntimeName": "eval_agent_abc123"})
    monkeypatch.setattr(svc, "control_client", lambda _ws=None: MagicMock())
    # `create_run` hands `start()` → `execute_run` to the process-global `run_queue`,
    # which runs it on a daemon thread: an accepted run reaches `executed` a little AFTER
    # the 201 — on a loaded box (the xdist gate) later than the next assertion, and a
    # `start()` still pending at teardown would write into the NEXT test's wiped ledger.
    # The recorder therefore signals, and `wait_executed` blocks until the queued
    # callable has run (r8-nits, review of r8-b).
    executed: list[str] = []
    ran = threading.Event()

    def record(run_id, **kw):
        executed.append(run_id)
        ran.set()

    monkeypatch.setattr(svc, "execute_run", record)
    yield data, executed
    if executed or ran.is_set():
        ran.wait(10)
    ex.set_runtime_executor(None)


def wait_executed(executed: list[str], *, timeout: float = 10.0) -> list[str]:
    """The recorded run ids once the queue's worker thread has run the callable."""
    deadline = time.monotonic() + timeout
    while not executed and time.monotonic() < deadline:
        time.sleep(0.01)
    return list(executed)


def _dataset() -> str:
    db = SessionLocal()
    try:
        dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="gate",
                              items=[{"prompt": "x"}])
        db.add(dataset)
        db.commit()
        return dataset.id
    finally:
        db.close()


def _run_body(client) -> dict:
    db = SessionLocal()
    agent = make_agent(db, name="gate-agent")
    db.close()
    return {"agent_id": agent.id, "dataset_id": _dataset(), "wait_seconds": 0,
            "evaluators": ["Builtin.Helpfulness"]}


def test_runs_are_refused_while_the_worker_is_uninstalled(client, worker_runtime):
    data, executed = worker_runtime
    res = client.post("/api/eval/runs", json=_run_body(client))
    assert res.status_code == 503, res.text
    body = res.json()
    assert body["code"] == "evaluation.worker_missing"
    assert body["detail"] == {"worker_status": "not_installed"}
    with SessionLocal() as db:
        assert db.query(EvalRun).count() == 0
    assert data.calls == [] and executed == []


def test_runs_are_accepted_once_the_worker_preset_is_active(client, worker_runtime):
    preset_row(status="active", arn=CURRENT_WORKER)
    _, executed = worker_runtime
    res = client.post("/api/eval/runs", json=_run_body(client))
    assert res.status_code == 201, res.text
    assert wait_executed(executed) == [res.json()["id"]]


def test_a_worker_still_deploying_is_not_ready(client, worker_runtime):
    preset_row(status="deploying", arn=None)
    res = client.post("/api/eval/runs", json=_run_body(client))
    assert res.status_code == 503 and res.json()["detail"] == {"worker_status": "deploying"}


def test_insights_runs_go_through_the_same_gate(client, worker_runtime):
    body = {**_run_body(client), "mode": "insights", "lookback_hours": 1}
    body.pop("dataset_id")
    res = client.post("/api/eval/runs", json=body)
    assert res.status_code == 503 and res.json()["code"] == "evaluation.worker_missing"


def test_score_now_and_evaluator_crud_keep_working_without_the_worker(client, worker_runtime):
    # SCORE NOW is in-process everywhere (CA-8); the gate never touches it
    assert isinstance(ex.select_executor(get_settings(), job_type=ex.ONDEMAND),
                      ex.InProcessExecutor)
    res = client.post("/api/eval/evaluators", json={
        "name": "gate_judge", "instructions": "Rate {assistant_turn} for tone", "level": "TRACE",
    })
    assert res.status_code == 201
    assert client.get("/api/eval/datasets").status_code == 200


def test_recommendations_are_refused_while_the_worker_is_uninstalled(client, worker_runtime):
    from tests.evaluation.test_run_recommendations import _agent, _run

    run_id = _run(_agent())  # completed, with results — eligible for a recommendation
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "Old prompt."})
    assert res.status_code == 503, res.text
    assert res.json()["code"] == "evaluation.worker_missing"
    with SessionLocal() as db:
        assert db.query(EvalRecommendation).count() == 0


def test_the_canary_traffic_stage_probes_the_worker(workspace, worker_runtime):
    """The canary traffic stage submits a batch_eval job — same gate, same code."""
    from app.core.errors import AppError

    executor = canary_svc.eval_executor.select_executor(get_settings(), job_type="batch_eval")
    with pytest.raises(AppError) as exc:
        canary_svc.eval_executor.assert_executor_ready(executor, workspace)
    assert exc.value.code == "evaluation.worker_missing"
