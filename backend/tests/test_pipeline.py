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
