# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_assistant
"""A backend restart mid-work (quality pack Q4): the startup sweeps of ``app.main``.

The ledger is seeded the way a dead process leaves it — an architect turn and a Chat turn
holding their claims with a partial event log, a Runtime canary with a
``running_action`` thread flag, an eval run queued before dispatch and one whose worker
job was dispatched, a data pipeline whose run thread died ``running`` — then the REAL
startup runs (``create_app(resume_jobs=True)``), and every kind carries its documented
interrupted marker: turns persisted as ``interrupted``
from their logs and ended for reattaching clients; action flags cleared with a retryable
error; the undispatched run failed honestly; the dispatched run re-attached and settled.
A conversation CLEAR (``purge_assistant_conversation``, N15-obs6) a restart cut short is
the one job that RESUMES: the startup re-adopts the ``running`` row and its canary claim,
and finishes from the verified checkpoints.
"""

from __future__ import annotations

import pytest

import app.evaluation.service as eval_svc
from app.assistant import purge, service
from app.core.db import SessionLocal
from app.evaluation import pipeline_routers
from app.evaluation.jobs import JobDone, JobStatus
from app.evaluation.models import EvalPipeline
from app.main import create_app
from app.models.assistant import AssistantConversation
from app.models.ledger import Agent, Job
from app.optimization import canary_service
from app.optimization.models import RuntimeCanary
from app.services import chat_turns
from tests.evaluation.fakes import FakeExecutor, strip_ci
from tests.evaluation.test_resume import get_run, make_run, wait_status
from tests.test_assistant import _latest, _open
from tests.test_assistant_async_turns import _dead_turn as _dead_assistant_turn
from tests.test_assistant_purge_dependencies import (  # noqa: F401 — fixture by name
    _world,
    cloud,
    wait_job,
)
from tests.test_chat_api import make_active_agent
from tests.test_chat_async_turns import SID, _log, _rows, _session
from tests.test_chat_async_turns import _dead_turn as _dead_chat_turn
from tests.test_visibility import _add

RESTART_MARK = "interrupted by a backend restart"


def _get(model, row_id):
    db = SessionLocal()
    try:
        return db.get(model, row_id)
    finally:
        db.close()


@pytest.fixture
def executor(monkeypatch) -> FakeExecutor:
    fake = FakeExecutor()
    monkeypatch.setattr(eval_svc, "select_executor", lambda settings, job_type: fake)
    monkeypatch.setattr(eval_svc, "POLL_INTERVAL_S", 0.02)
    return fake


def test_backend_restart_mid_turn_leaves_every_kind_its_interrupted_marker(
    client, ready, executor
):
    # ── what the dead process left behind ──────────────────────────────────
    streamed = "Design for the helpdesk agent. " * 3
    cid = _open(client)
    _dead_assistant_turn(cid, streamed)
    agent_id = make_active_agent(method="container", name="restart-chat")
    _dead_chat_turn(agent_id, SID)
    canary_id = _add(RuntimeCanary(name="canary", champion_agent_id=agent_id,
                                   champion_agent_name="restart-chat",
                                   challenger_agent_id=agent_id,
                                   challenger_agent_name="restart-chat",
                                   running_action="setup", progress="minting"))
    queued = make_run(status="queued")  # killed before its job was dispatched
    # a data pipeline whose daemon run thread died with the process (upstream #239 port)
    pipeline_id = _add(EvalPipeline(name="nightly", description="", status="running",
                                    config={"output": {"dataset_name": "d"}},
                                    last_run={"at": "2026-10-19T00:00:00+00:00", "added": 3}))
    # a living process would hold it in `_live_runs`; the dead one left the row only
    assert pipeline_id not in pipeline_routers._live_runs
    dispatched = make_run(status="evaluating", batch_eval_id="job-restart", mode="evaluators")
    executor.complete("job-restart", [{"session_id": "s1", "evaluator_id": "Builtin.Correctness",
                                       "score": 1.0}])
    assert _latest(client, cid)["live_turn"] is not None  # both turns read as live
    assert client.get(f"/api/chat/{agent_id}/history",
                      params={"session_id": SID}).json()["live_turn"] is not None

    # ── the restart ────────────────────────────────────────────────────────
    create_app(resume_jobs=True)

    # architect turn: interrupted from its log, terminal event appended, claim cleared
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        assert conv.active_turn is None and conv.active_turn_token is None
        msgs = [m for m in service._messages(db, cid) if m.role != service.SESSION_ROLE]
        assert [m.role for m in msgs] == ["user", "assistant", "error"]
        assert msgs[1].text == streamed
        assert msgs[2].name == service.TURN_INTERRUPTED_NAME
        assert msgs[2].text == service.TURN_INTERRUPTED_TEXT
        events = service.turn_events(db, cid, 1)
        assert events[-1].event == "error" and events[-1].data == {"message": "interrupted"}
    finally:
        db.close()
    assert _latest(client, cid)["live_turn"] is None

    # chat turn: the open bubble rebuilt from the log + the interrupted marker row
    rows = [(r[0], r[1]) for r in _rows(SID)]
    assert rows[-2:] == [("agent", "The answer is 4"), ("error", chat_turns.INTERRUPTED_TEXT)]
    assert _log(agent_id, SID)[-1] == ("error", 6, {"message": "interrupted"})
    row = _session(agent_id, SID)
    assert row.active_turn is None and row.active_turn_token is None

    # canary: the daemon-thread flag is gone, the error says what happened (the configuration
    # experiment flow was retired in r13-b; canaries are the only optimization kind swept)
    canary = _get(RuntimeCanary, canary_id)
    assert canary.running_action is None and canary.progress is None
    assert canary.error.startswith("setup: ") and RESTART_MARK in canary.error

    # data pipeline: the orphaned `running` row is failed honestly, its last outcome kept
    pipeline = _get(EvalPipeline, pipeline_id)
    assert pipeline.status == "failed" and RESTART_MARK in pipeline.last_run["error"]
    assert pipeline.last_run["added"] == 3  # the previous run's numbers are not erased

    # eval runs: undispatched → failed honestly; dispatched → re-attached and settled
    failed = get_run(queued)
    assert failed.status == "failed" and "interrupted by a backend restart" in failed.error
    assert "submit the run again" in failed.error
    settled = wait_status(dispatched, "completed")
    assert strip_ci(settled.scores) == [{"evaluatorId": "Builtin.Correctness", "score": 1.0,
        "count": 1, "n": 1, "unit": "turn"}]
    assert settled.error is None

    # idempotent: a second start finds nothing to sweep
    assert service.clear_stale_turn_claims() == 0
    assert chat_turns.clear_stale_turn_claims() == 0
    assert eval_svc.resume_interrupted_runs() == []
    assert pipeline_routers.sweep_interrupted_pipelines() == []


def test_worker_crash_mid_batch_reattaches_every_dispatched_phase(executor):
    """The worker Runtime kept going while the backend died in any dispatched phase: the
    row is re-attached by its job id and settles from the job's artefacts; a job that
    ended FAILED settles the row as failed with the worker's reason."""
    runs = {phase: make_run(status=phase, batch_eval_id=f"job-{phase}", mode="evaluators")
            for phase in ("invoking", "waiting", "evaluating")}
    for phase in ("invoking", "waiting"):
        executor.complete(f"job-{phase}", [{"session_id": "s1",
                                            "evaluator_id": "Builtin.Correctness",
                                            "score": 0.5}])
    executor.statuses["job-evaluating"] = JobStatus(phase="evaluating", state="FAILED")
    executor.dones["job-evaluating"] = JobDone(
        state="FAILED", error="scenario s1 failed after 3/3 attempts: ClientError: boom")
    create_app(resume_jobs=True)
    for phase in ("invoking", "waiting"):
        assert strip_ci(wait_status(runs[phase], "completed").scores) == [
            {"evaluatorId": "Builtin.Correctness", "score": 0.5,
                "count": 1, "n": 1, "unit": "turn"}]
    failed = wait_status(runs["evaluating"], "failed")
    assert "3/3 attempts" in failed.error


def test_backend_restart_mid_clear_resumes_the_purge_job_from_its_checkpoints(cloud):
    """The process died while a conversation CLEAR was tearing down its canary: the job
    row is still ``running`` and the canary still carries the purge's ``cleanup`` claim.
    The startup resume (``resume_pending_jobs`` → ``start_purge_resume``) adopts both —
    the canary sweep that runs next to it leaves a live clear's claim alone — and the job
    finishes: canary read back gone, Agent deleted, conversation rows removed."""
    ids = _world()
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, ids["cid"])
        job, done = purge.request_purge(db, row, can=lambda _p: True)
        assert done is None
        job = db.get(Job, job.id)
        job.status = "running"
        job.payload = {**job.payload, "progress": {
            f"canary:{ids['canary']}": {"state": "cleaning", "detail": "stopping"}}}
        canary = db.get(RuntimeCanary, ids["canary"])
        canary.running_action = "cleanup"
        canary.progress = f"{purge.MARKER_PREFIX}{ids['cid'][:8]}: tearing down"
        db.commit()
        job_id = job.id
    finally:
        db.close()
    # what the dead process left: a live clear the console still reports
    fp_before = purge.footprint(SessionLocal(), _get(AssistantConversation, ids["cid"]))
    assert fp_before["purge"]["status"] == "running" and fp_before["blockers"] == []

    # ── the restart ────────────────────────────────────────────────────────
    create_app(resume_jobs=True)

    job = wait_job(job_id)
    assert job.status == "succeeded", (job.status, job.error, job.payload.get("blocker"))
    assert _get(AssistantConversation, ids["cid"]) is None
    assert _get(Agent, ids["agent"]).status == "deleted"
    canary = _get(RuntimeCanary, ids["canary"])
    assert canary.status == "cleaned" and canary.running_action is None
    assert not (canary.error or "").startswith("cleanup: interrupted")  # not swept as dead
    assert cloud.events[0] == f"canary-cleanup:{ids['canary']}"
    assert f"agent-residue:{ids['agent']}" in cloud.events
    # idempotent: a second start finds nothing to resume or sweep
    from app.deployer import pipeline

    assert pipeline.resume_pending_jobs() == []
    assert canary_service.clear_stale_running_actions() == []
