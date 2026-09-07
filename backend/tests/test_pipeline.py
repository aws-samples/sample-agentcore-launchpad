"""Pipeline stage transitions, failure handling, persistence/resume."""

import json

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.pipeline import (
    STAGE_ORDER,
    StageResult,
    create_deployment,
    execute_deploy_job,
    register_method,
    resume_pending_jobs,
)
from app.models.ledger import Agent, Deployment, Job


def make_agent(db, method: str, name: str = "test-agent") -> Agent:
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name=name, method=method, status="deploying", spec={"name": name})
    db.add(agent)
    db.commit()
    return agent


def test_happy_path_runs_all_stages_in_order():
    calls: list[str] = []
    register_method(
        "fake_ok",
        {
            s: (lambda ctx, agent, _s=s: (calls.append(_s), StageResult(detail=f"{_s} done"))[1])
            for s in STAGE_ORDER
        },
    )
    db = SessionLocal()
    agent = make_agent(db, "fake_ok")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    assert calls == STAGE_ORDER
    db = SessionLocal()
    job = db.get(Job, job.id)
    deployment = db.get(Deployment, deployment.id)
    agent = db.get(Agent, agent.id)
    assert job.status == "succeeded"
    assert deployment.status == "succeeded"
    assert agent.status == "active"
    assert [s["status"] for s in deployment.stages] == ["succeeded"] * 5
    events = [json.loads(line) for line in job.log.splitlines()]
    assert all("ts" in e and "stage" in e for e in events)
    assert {e["stage"] for e in events} == set(STAGE_ORDER)
    db.close()


def test_failure_marks_stage_job_agent_failed():
    def boom(ctx, agent):
        raise RuntimeError("bad role arn")

    register_method(
        "fake_fail",
        {"generate": lambda ctx, agent: StageResult(detail="ok"), "deploy": boom},
    )
    db = SessionLocal()
    agent = make_agent(db, "fake_fail", name="fail-agent")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    db = SessionLocal()
    job = db.get(Job, job.id)
    deployment = db.get(Deployment, deployment.id)
    agent = db.get(Agent, agent.id)
    stage_by_name = {s["name"]: s for s in deployment.stages}
    assert stage_by_name["deploy"]["status"] == "failed"
    assert "bad role arn" in stage_by_name["deploy"]["detail"]
    assert stage_by_name["register"]["status"] == "pending"  # never reached
    assert job.status == "failed" and "bad role arn" in job.error
    assert agent.status == "failed" and "bad role arn" in agent.error
    errors = [json.loads(x) for x in job.log.splitlines() if json.loads(x)["level"] == "error"]
    assert errors, "error event must be logged"
    db.close()


def test_resume_skips_completed_stages():
    calls: list[str] = []
    register_method(
        "fake_resume",
        {s: (lambda ctx, agent, _s=s: (calls.append(_s), StageResult())[1]) for s in STAGE_ORDER},
    )
    db = SessionLocal()
    agent = make_agent(db, "fake_resume", name="resume-agent")
    deployment, job = create_deployment(db, agent)
    # simulate a crash after generate+package completed
    stages = [dict(s) for s in deployment.stages]
    stages[0]["status"] = "succeeded"
    stages[1]["status"] = "skipped"
    deployment.stages = stages
    job.status = "running"
    db.commit()
    db.close()

    execute_deploy_job(job.id)

    assert calls == ["provision", "deploy", "register"]
    db = SessionLocal()
    assert db.get(Job, job.id).status == "succeeded"
    db.close()


def test_resume_pending_jobs_picks_up_interrupted(monkeypatch):
    """Simulated restart: a 'running' job in the DB is re-executed on startup."""
    register_method("fake_restart", {s: lambda ctx, agent: StageResult() for s in STAGE_ORDER})
    db = SessionLocal()
    agent = make_agent(db, "fake_restart", name="restart-agent")
    _, job = create_deployment(db, agent)
    job.status = "running"  # backend died mid-run
    db.commit()
    job_id = job.id
    db.close()

    launched: list[str] = []
    monkeypatch.setattr(
        "app.deployer.pipeline.start_deploy_async", lambda jid: launched.append(jid)
    )
    resumed = resume_pending_jobs()
    assert job_id in resumed and job_id in launched


def test_skip_register_only_skips_registry_stage():
    calls: list[str] = []
    register_method(
        "fake_promotion",
        {
            s: (lambda ctx, agent, _s=s: (calls.append(_s), StageResult())[1])
            for s in STAGE_ORDER
        },
    )
    db = SessionLocal()
    agent = make_agent(db, "fake_promotion", name="promotion-agent")
    deployment, job = create_deployment(
        db, agent, mode="update", skip_register=True
    )
    assert job.payload["mode"] == "update"
    assert job.payload["skip_register"] is True
    db.close()

    execute_deploy_job(job.id)

    assert calls == STAGE_ORDER[:-1]
    db = SessionLocal()
    stages = {s["name"]: s for s in db.get(Deployment, deployment.id).stages}
    assert stages["register"]["status"] == "skipped"
    assert db.get(Job, job.id).status == "succeeded"
    db.close()


def _reload(job_id: str, deployment_id: str, agent_id: str):
    db = SessionLocal()
    try:
        return db.get(Job, job_id), db.get(Deployment, deployment_id), db.get(Agent, agent_id)
    finally:
        db.close()


def _assert_job_level_failure(job, deployment, agent, message: str) -> None:
    assert job.status == "failed" and message in job.error
    assert deployment.status == "failed" and deployment.ended_at is not None
    assert agent.status == "failed" and message in agent.error
    events = [json.loads(line) for line in job.log.splitlines()]
    errors = [e for e in events if e["level"] == "error"]
    assert len(errors) == 1 and message in errors[0]["msg"]


def test_job_level_failure_marks_agent_and_deployment_failed(monkeypatch):
    """A failure raised *outside* any stage (workspace gone) must leave the agent in
    the same honest state a stage failure does — otherwise the Create page polls
    forever and redeploy is refused with 409."""
    register_method("fake_ws_gone", {s: lambda ctx, agent: StageResult() for s in STAGE_ORDER})

    def gone(_workspace_id):
        raise LookupError("workspace 'x' no longer exists")

    monkeypatch.setattr("app.deployer.pipeline.context_for_workspace", gone)
    db = SessionLocal()
    agent = make_agent(db, "fake_ws_gone", name="ws-gone-agent")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)  # must not raise

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    _assert_job_level_failure(job, deployment, agent, "workspace 'x' no longer exists")
    # no stage ever ran, so none of them is marked failed — the job itself carries it
    assert {s["status"] for s in deployment.stages} == {"pending"}


def test_unregistered_method_marks_agent_and_deployment_failed():
    db = SessionLocal()
    agent = make_agent(db, "no_such_method", name="no-method-agent")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    _assert_job_level_failure(job, deployment, agent, "no deploy method registered")


def test_job_level_failure_with_missing_rows_still_fails_job():
    """Half-deleted ledger: agent/deployment rows gone, the job must still end failed."""
    db = SessionLocal()
    agent = make_agent(db, "fake_ok", name="orphan-agent")
    deployment, job = create_deployment(db, agent)
    db.delete(deployment)
    db.delete(agent)
    db.commit()
    job_id = job.id
    db.close()

    execute_deploy_job(job_id)

    db = SessionLocal()
    job = db.get(Job, job_id)
    assert job.status == "failed" and "ledger rows missing" in job.error
    assert any(json.loads(x)["level"] == "error" for x in job.log.splitlines())
    db.close()


def test_failures_reach_the_process_logger(monkeypatch, caplog):
    """Stage failure → WARNING, job-level failure → ERROR on the pipeline logger, so the
    prod runbook's `journalctl | grep -iE "error|traceback"` shows deploy failures."""
    import logging

    caplog.set_level(logging.WARNING, logger="launchpad.deploy")

    def boom(ctx, agent):
        raise RuntimeError("bad role arn")

    register_method("fake_log_fail", {"deploy": boom})
    db = SessionLocal()
    agent = make_agent(db, "fake_log_fail", name="log-stage-agent")
    _, job = create_deployment(db, agent)
    db.close()
    execute_deploy_job(job.id)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert agent.id in warnings[0].getMessage()
    assert "deploy" in warnings[0].getMessage() and "bad role arn" in warnings[0].getMessage()

    caplog.clear()
    db = SessionLocal()
    agent = make_agent(db, "no_such_method", name="log-job-agent")
    _, job = create_deployment(db, agent)
    db.close()
    execute_deploy_job(job.id)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert agent.id in errors[0].getMessage() and "no_such_method" in errors[0].getMessage()
    assert errors[0].exc_info is not None  # logger.exception carries the traceback
    assert all(r.levelno < logging.ERROR for r in caplog.records if r is not errors[0])
