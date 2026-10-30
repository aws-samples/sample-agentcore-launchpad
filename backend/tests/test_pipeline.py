"""Pipeline stage transitions, failure handling, persistence/resume."""

import json

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import pipeline
from app.deployer.pipeline import (
    INTERRUPTED,
    STAGE_INTERRUPTED,
    STAGE_ORDER,
    StageInterrupted,
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


def _serving(db, method: str, name: str) -> Agent:
    """An agent with a LIVE version: active, ARN set, one succeeded deployment."""
    agent = make_agent(db, method, name=name)
    agent.status = "active"
    agent.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/live"
    agent.version = "1"
    first, first_job = create_deployment(db, agent)
    first.status = "succeeded"
    first_job.status = "succeeded"
    db.commit()
    return agent


def _register_update_fail(name: str, stage: str = "package"):
    def boom(ctx, agent):
        raise RuntimeError("could not resolve the requirements into a hashed lockfile")

    register_method(name, {"generate": lambda ctx, agent: StageResult(detail="ok"), stage: boom})


def test_failed_update_keeps_a_serving_agent_active_and_records_the_error():
    """N10v3-bug7 (= N10v2-bug9): a redeploy that fails in package/provision must not flip
    a serving agent to `failed` — the live version is untouched and READY, Chat keeps
    working on it. The failure lives on the Deployment/Job and on the agent as its
    last-deploy error."""
    _register_update_fail("fake_update_fail")
    db = SessionLocal()
    agent = _serving(db, "fake_update_fail", "serving-agent")
    agent.status = "deploying"  # what the redeploy route sets
    deployment, job = create_deployment(db, agent, mode="update")
    db.commit()
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == "failed" and "hashed lockfile" in job.error
    assert deployment.status == "failed" and deployment.ended_at is not None
    assert agent.status == "active", "the live version still serves"
    assert "hashed lockfile" in agent.error  # surfaced as the last-deploy error
    assert agent.version == "1" and agent.arn.endswith("/live")
    events = [json.loads(line) for line in job.log.splitlines()]
    assert any("stays active" in e["msg"] for e in events)


def test_failed_update_inside_the_deploy_stage_fails_the_agent():
    """Review of f68cdff8: zip_runtime writes the NEW agentRuntimeVersion onto the row as
    soon as UpdateAgentRuntime is accepted, before wait_runtime_ready. A failure inside
    the deploy stage (UPDATE_FAILED / timeout) therefore may leave `version` naming a
    version that never became READY — the live version is NOT provably untouched, so the
    agent is `failed`, not active."""
    _register_update_fail("fake_update_deploy_fail", stage="deploy")
    db = SessionLocal()
    agent = _serving(db, "fake_update_deploy_fail", "deploy-fail-agent")
    agent.status = "deploying"
    deployment, job = create_deployment(db, agent, mode="update")
    db.commit()
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == "failed" and deployment.status == "failed"
    assert {s["name"]: s["status"] for s in deployment.stages}["deploy"] == "failed"
    assert agent.status == "failed" and "hashed lockfile" in agent.error
    events = [json.loads(line) for line in job.log.splitlines()]
    assert not any("stays active" in e["msg"] for e in events)


def _run_update(db, agent: Agent, method_stage_fail: str | None, name: str):
    """One update-mode job of ``agent`` under a method that fails at ``method_stage_fail``
    (None = every stage succeeds); returns (job, deployment) ids after the run."""
    if method_stage_fail is None:
        register_method(name, {s: lambda ctx, agent: StageResult() for s in STAGE_ORDER})
    else:
        _register_update_fail(name, stage=method_stage_fail)
    agent.method = name
    agent.status = "deploying"
    deployment, job = create_deployment(db, agent, mode="update")
    db.commit()
    execute_deploy_job(job.id)
    return job.id, deployment.id


def test_republish_failure_after_a_deploy_stage_failure_keeps_the_agent_failed():
    """N17-obs100: a failure INSIDE `deploy` left the live state unknown (agent `failed`,
    agents.md §4.3). The re-publish that follows fails at `package` — before its own
    `deploy` stage — and used to flip the agent back to `active` because the keep-live test
    found the OLDER succeeded deployment. Nothing has been verified since that deploy-stage
    failure, so the agent stays `failed` (§4.2) and the job log never claims a kept version."""
    db = SessionLocal()
    agent = _serving(db, "fake_obs100_seed", "obs100-agent")
    agent_id = agent.id
    _run_update(db, agent, "deploy", "fake_obs100_deploy_fail")
    db.expire_all()
    agent = db.get(Agent, agent_id)
    assert agent.status == "failed", "the §4.3 deploy-stage failure"

    job_id, deployment_id = _run_update(db, agent, "package", "fake_obs100_package_fail")
    db.close()

    job, deployment, agent = _reload(job_id, deployment_id, agent_id)
    assert job.status == "failed" and deployment.status == "failed"
    assert {s["name"]: s["status"] for s in deployment.stages}["package"] == "failed"
    assert agent.status == "failed", (agent.status, agent.error)
    assert "hashed lockfile" in agent.error
    events = [json.loads(line) for line in job.log.splitlines()]
    assert not any("stays active" in e["msg"] for e in events)


def test_a_succeeded_publish_after_a_deploy_stage_failure_re_verifies_the_live_version():
    """The counterpart: deploy-stage failure → a re-publish that SUCCEEDS (`active`) → a
    later re-publish that fails at `package` keeps the live version — the succeeded publish
    is newer than the unknown-state failure, so the version it left is verified."""
    db = SessionLocal()
    agent = _serving(db, "fake_obs100b_seed", "obs100b-agent")
    agent_id = agent.id
    _run_update(db, agent, "deploy", "fake_obs100b_deploy_fail")
    db.expire_all()
    agent = db.get(Agent, agent_id)
    _run_update(db, agent, None, "fake_obs100b_ok")
    db.expire_all()
    agent = db.get(Agent, agent_id)
    assert agent.status == "active"

    job_id, deployment_id = _run_update(db, agent, "package", "fake_obs100b_package_fail")
    db.close()

    job, _deployment, agent = _reload(job_id, deployment_id, agent_id)
    assert job.status == "failed"
    assert agent.status == "active", "the live version is verified again"
    events = [json.loads(line) for line in job.log.splitlines()]
    assert any("stays active" in e["msg"] for e in events)


def test_failed_update_in_register_after_a_ready_deploy_keeps_the_agent_active():
    """The deploy stage finished (the new version is READY and DEFAULT follows it); a
    register-stage failure is a registry problem, not a serving problem."""
    _register_update_fail("fake_update_register_fail", stage="register")
    db = SessionLocal()
    agent = _serving(db, "fake_update_register_fail", "register-fail-agent")
    agent.status = "deploying"
    deployment, job = create_deployment(db, agent, mode="update")
    db.commit()
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    stages = {s["name"]: s["status"] for s in deployment.stages}
    assert stages["deploy"] == "skipped" and stages["register"] == "failed"
    assert job.status == "failed" and agent.status == "active"
    assert "hashed lockfile" in agent.error


def test_failed_update_without_a_live_version_still_fails_the_agent():
    """A FIRST deploy that fails yields `failed`, even in update mode (the deploy stage
    falls back to Create when there is no live resource — nothing is serving)."""
    _register_update_fail("fake_update_fail_first")
    db = SessionLocal()
    agent = make_agent(db, "fake_update_fail_first", name="first-update-agent")
    deployment, job = create_deployment(db, agent, mode="update")
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == "failed" and deployment.status == "failed"
    assert agent.status == "failed" and "hashed lockfile" in agent.error


def test_failed_create_of_a_previously_served_agent_still_fails_it():
    """Only an UPDATE keeps the live version: a create-mode job on a row that happens to
    carry an ARN (a re-create after an out-of-band delete) failing means nothing serves."""
    _register_update_fail("fake_create_fail_served")
    db = SessionLocal()
    agent = _serving(db, "fake_create_fail_served", "recreate-agent")
    agent.status = "deploying"
    deployment, job = create_deployment(db, agent, mode="create")
    db.commit()
    db.close()

    execute_deploy_job(job.id)

    _job, _deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert agent.status == "failed"


def test_job_level_failure_of_an_update_keeps_the_serving_agent_active(monkeypatch):
    register_method(
        "fake_update_ws_gone", {s: lambda ctx, agent: StageResult() for s in STAGE_ORDER}
    )

    def gone(_workspace_id):
        raise LookupError("workspace 'x' no longer exists")

    monkeypatch.setattr("app.deployer.pipeline.context_for_workspace", gone)
    db = SessionLocal()
    agent = _serving(db, "fake_update_ws_gone", "update-ws-gone-agent")
    agent.status = "deploying"
    deployment, job = create_deployment(db, agent, mode="update")
    db.commit()
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == "failed" and deployment.status == "failed"
    assert agent.status == "active" and "no longer exists" in agent.error


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


def _interrupting_method(name: str, calls: list[str], kills: int):
    """A method whose `package` stage raises `StageInterrupted` on its first ``kills``
    attempts (a pip child killed by a signal) and succeeds afterwards."""
    attempts = {"package": 0}

    def package(ctx, agent):
        attempts["package"] += 1
        calls.append("package")
        if attempts["package"] <= kills:
            raise StageInterrupted(
                "pip install was killed (killed by signal 15 (SIGTERM)) — the stage re-runs",
                failure="pip install failed (killed by signal 15 (SIGTERM)): install process "
                "terminated (signal 15, SIGTERM) — the backend was restarting, or the build "
                "was stopped from outside; redeploy.",
            )
        return StageResult(detail="installed")

    register_method(
        name,
        {
            "generate": lambda ctx, agent: (calls.append("generate"), StageResult())[1],
            "package": package,
            "provision": lambda ctx, agent: (calls.append("provision"), StageResult())[1],
        },
    )


def test_interrupted_stage_leaves_the_job_running_and_the_resume_re_runs_it(monkeypatch):
    """N17-obs113 (b): a stage whose subprocess was stopped WITH the service (the shutdown
    flag is set) raises `StageInterrupted`. The stage is recorded `interrupted`, the Job
    stays `running` (so `resume_pending_jobs()` picks it), the agent stays `deploying`, the
    log says why, no in-process retry is scheduled — and the resumed job re-runs that stage
    (only succeeded / skipped stages are skipped)."""
    calls: list[str] = []
    _interrupting_method("fake_interrupted", calls, kills=1)
    monkeypatch.setattr(pipeline._SHUTDOWN, "is_set", lambda: True)
    scheduled: list[object] = []
    monkeypatch.setattr(pipeline, "schedule_later", lambda d, fn: scheduled.append(fn))
    db = SessionLocal()
    agent = make_agent(db, "fake_interrupted", name="interrupted-agent")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id, resume=False)
    assert scheduled == [], "stopping: the startup resume owns the re-run"

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    stages = {s["name"]: s for s in deployment.stages}
    assert stages["generate"]["status"] == "succeeded"
    assert stages["package"]["status"] == INTERRUPTED
    assert "signal 15" in stages["package"]["detail"] and stages["package"]["ended_at"]
    assert stages["provision"]["status"] == "pending", "nothing ran after the interruption"
    assert job.status == "running" and job.error is None, "resumable, not terminal"
    assert deployment.status == "running" and deployment.ended_at is None
    assert agent.status == "deploying" and agent.error is None
    events = [json.loads(line) for line in job.log.splitlines()]
    assert any(e["msg"] == STAGE_INTERRUPTED for e in events)
    assert calls == ["generate", "package"]

    # the startup resume adopts it …
    launched: list[str] = []
    monkeypatch.setattr(
        "app.deployer.pipeline.start_deploy_async", lambda jid: launched.append(jid)
    )
    assert job.id in resume_pending_jobs() and launched == [job.id]
    # … and the resumed run re-enters at the interrupted stage
    execute_deploy_job(job.id)
    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert calls == ["generate", "package", "package", "provision"]
    assert {s["name"]: s["status"] for s in deployment.stages}["package"] == "succeeded"
    assert (job.status, deployment.status, agent.status) == ("succeeded", "succeeded", "active")


def test_killed_by_signal_rule():
    """Any negative exit status is a signal kill (pip / uv never kill themselves); a plain
    exit status or no status never is."""
    assert pipeline.killed_by_signal(-15) is True
    assert pipeline.killed_by_signal(-9) is True
    assert pipeline.killed_by_signal(1) is False
    assert pipeline.killed_by_signal(0) is False
    assert pipeline.killed_by_signal(None) is False


def _run_with_retry_scheduler(monkeypatch):
    """The shutdown flag is NOT set; the retry scheduler is captured instead of a timer."""
    monkeypatch.setattr(pipeline._SHUTDOWN, "is_set", lambda: False)
    scheduled: list[tuple[float, object]] = []
    monkeypatch.setattr(pipeline, "schedule_later", lambda d, fn: scheduled.append((d, fn)))
    return scheduled


def _run_scheduled(scheduled) -> None:
    """Fire the captured retry the way the timer would (the worker thread is joined)."""
    _delay, fn = scheduled.pop(0)
    thread = fn()
    thread.join(timeout=30)
    assert not thread.is_alive()


def test_interrupted_stage_on_a_live_service_is_retried_once_in_process(monkeypatch):
    """Lead ruling on N17-obs113: the service keeps running (no shutdown flag) and a stage's
    pip child dies -15 once — the stage is `interrupted`, the Job stays `running`, ONE retry
    is scheduled after the fixed delay (no sleep: the scheduler is injected), the retry
    re-runs that stage in-process and the job completes `succeeded`."""
    calls: list[str] = []
    _interrupting_method("fake_interrupted_retry", calls, kills=1)
    scheduled = _run_with_retry_scheduler(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, "fake_interrupted_retry", name="interrupted-retry-agent")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id, resume=False)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert {s["name"]: s["status"] for s in deployment.stages}["package"] == INTERRUPTED
    assert job.status == "running" and agent.status == "deploying"
    assert job.payload[pipeline.INTERRUPTED_RETRIES_KEY] == {"package": 1}
    assert [d for d, _ in scheduled] == [pipeline.INTERRUPTED_RETRY_DELAY_SECONDS]
    events = [json.loads(line) for line in job.log.splitlines()]
    assert any("re-run once in-process" in e["msg"] for e in events)

    _run_scheduled(scheduled)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert calls == ["generate", "package", "package", "provision"]
    assert (job.status, deployment.status, agent.status) == ("succeeded", "succeeded", "active")
    assert scheduled == []


def test_interrupted_stage_killed_twice_on_a_live_service_fails_with_the_signal_sentence(
    monkeypatch,
):
    """The retry dies too → terminal `failed` with the signal sentence (never `running`
    forever, never 'resolver produced no output'); no further retry is scheduled."""
    calls: list[str] = []
    _interrupting_method("fake_interrupted_twice", calls, kills=2)
    scheduled = _run_with_retry_scheduler(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, "fake_interrupted_twice", name="interrupted-twice-agent")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id, resume=False)
    _run_scheduled(scheduled)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert calls == ["generate", "package", "package"]
    stages = {s["name"]: s for s in deployment.stages}
    assert stages["package"]["status"] == "failed"
    assert "install process terminated (signal 15, SIGTERM)" in stages["package"]["detail"]
    assert job.status == "failed" and deployment.status == "failed"
    assert agent.status == "failed" and "signal 15" in agent.error
    assert "no output" not in job.error
    events = [json.loads(line) for line in job.log.splitlines()]
    assert any(e["msg"] == pipeline.STAGE_INTERRUPTED_TWICE for e in events)
    assert scheduled == []


def test_interrupted_stage_killed_twice_on_a_live_update_keeps_the_live_version(monkeypatch):
    """The terminal failure goes through `_finish`: a redeploy whose package stage was
    killed twice keeps the serving version (agents.md §4.3) — `active`, last_deploy_error."""
    calls: list[str] = []
    _interrupting_method("fake_interrupted_twice_update", calls, kills=2)
    scheduled = _run_with_retry_scheduler(monkeypatch)
    db = SessionLocal()
    agent = _serving(db, "fake_interrupted_twice_update", "interrupted-live-agent")
    agent.status = "deploying"
    deployment, job = create_deployment(db, agent, mode="update")
    db.commit()
    db.close()

    execute_deploy_job(job.id, resume=False)
    _run_scheduled(scheduled)

    job, _deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == "failed" and agent.status == "active"
    assert "signal 15" in agent.error


def test_schedule_later_runs_the_callback_on_a_daemon_timer():
    import threading

    fired = threading.Event()
    pipeline.schedule_later(0.01, fired.set)
    assert fired.wait(5)


def test_chained_term_handler_sets_the_flag_then_calls_the_previous_handler(monkeypatch):
    seen: list[tuple[int, object]] = []
    noted: list[bool] = []
    monkeypatch.setattr(pipeline, "note_shutdown", lambda *a: noted.append(True))
    handler = pipeline._chained_term_handler(lambda signum, frame: seen.append((signum, frame)))
    handler(15, None)
    assert noted == [True] and seen == [(15, None)]


def test_chained_term_handler_never_swallows_the_stop(monkeypatch):
    """A previous handler that is not a Python callable (SIG_DFL, or None for one installed
    outside Python) means the default action: the handler restores SIG_DFL and re-sends
    the signal to this process; SIG_IGN stays ignored. The flag is set in every case."""
    import signal as _signal

    monkeypatch.setattr(pipeline, "note_shutdown", lambda *a: None)
    installed: list[object] = []
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(pipeline.signal, "signal", lambda signum, h: installed.append((signum, h)))
    monkeypatch.setattr(pipeline.os, "kill", lambda pid, signum: sent.append((pid, signum)))
    for previous in (_signal.SIG_DFL, None):
        installed.clear()
        sent.clear()
        pipeline._chained_term_handler(previous)(15, None)
        assert installed == [(15, _signal.SIG_DFL)] and sent == [(pipeline.os.getpid(), 15)]
    installed.clear()
    sent.clear()
    pipeline._chained_term_handler(_signal.SIG_IGN)(15, None)
    assert installed == [] and sent == []


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


# --- N17-obs13 belt: `_finish` stamps the spec the job packaged, never the row's -----------


def _snapshot_for(deployment_id: str):
    from app.models.ledger import SpecSnapshot

    db = SessionLocal()
    try:
        return db.query(SpecSnapshot).filter_by(deployment_id=deployment_id).one()
    finally:
        db.close()


def _rewrite_row(agent_id: str, **fields) -> None:
    other = SessionLocal()
    try:
        row = other.get(Agent, agent_id)
        row.spec = {**row.spec, **fields.pop("spec", {})}
        for k, v in fields.items():
            setattr(row, k, v)
        other.commit()
    finally:
        other.close()


def test_finish_stamps_the_spec_the_job_packaged_not_a_concurrent_writers(monkeypatch):
    """N17-obs13: a publish by another writer that lands BETWEEN two stages (the canary
    promote before its 409 guard, or any future writer) must never be recorded as what THIS
    job deployed. The job's own rewrites ARE carried: the `generate` stage's (BYOC provenance,
    on the passed object) and the `deploy` stage's (skills pinned through its own session, as
    `test_spec_snapshots._rewriting_deploy`). The writer fires in the inter-stage window the
    loop's `_agent_snapshot` re-read sits in — once, right after `package` succeeded."""
    import app.deployer.pipeline as pipeline

    def generate(ctx, agent):
        agent.spec = {**agent.spec, "provenance": "stamped-by-generate"}
        return StageResult(detail="generated")

    def deploy(ctx, agent):
        _rewrite_row(agent.id, spec={"skills": ["s3://bucket/system-skills/copy-1/"]},
                     version="7")
        return StageResult(detail="deployed")

    register_method("fake_concurrent", {
        "generate": generate,
        "package": lambda ctx, agent: StageResult(detail="packaged"),
        "provision": lambda ctx, agent: StageResult(detail="provisioned"),
        "deploy": deploy,
        "register": lambda ctx, agent: StageResult(detail="registered"),
    })
    db = SessionLocal()
    agent = make_agent(db, "fake_concurrent", name="concurrent-agent")
    agent.spec = {"name": "concurrent-agent", "system_prompt": "hi v2", "skills": ["skills/a/"]}
    db.commit()
    deployment, job = create_deployment(db, agent, mode="update")
    agent_id = agent.id
    db.close()

    real_snapshot = pipeline._agent_snapshot
    fired: list[str] = []

    def snapshot_then_publish(db_, aid):
        out = real_snapshot(db_, aid)
        dep = db_.get(Deployment, deployment.id)
        if not fired and {s["name"] for s in dep.stages if s["status"] == "succeeded"} == {
                "generate", "package"}:
            fired.append("between package and provision")
            _rewrite_row(aid, spec={"system_prompt": "candidate prompt"})  # the other writer
        return out

    monkeypatch.setattr(pipeline, "_agent_snapshot", snapshot_then_publish)

    execute_deploy_job(job.id)

    assert fired == ["between package and provision"]
    db = SessionLocal()
    job = db.get(Job, job.id)
    agent = db.get(Agent, agent_id)
    assert job.status == "succeeded"
    packaged = {"name": "concurrent-agent", "system_prompt": "hi v2",
                "skills": ["s3://bucket/system-skills/copy-1/"],
                "provenance": "stamped-by-generate"}
    assert job.payload["packaged_spec"] == packaged  # the job's own rewrites, nothing else
    assert agent.spec["system_prompt"] == "candidate prompt"  # the row is the writer's
    db.close()
    snap = _snapshot_for(deployment.id)
    assert snap.spec == packaged, snap.spec  # the snapshot says what the JOB deployed
    assert snap.aws_version == "7"


def test_a_job_that_predates_the_packaged_spec_key_starts_from_the_row_on_resume():
    """A job resumed across the upgrade carries no `packaged_spec`: the loop seeds it from the
    row as the job resumes (what the old finalizer would have read) and `_finish` stamps it —
    no behaviour change for in-flight jobs, and `stamp_deployed(spec=None)` keeps the row
    fallback for a direct caller."""
    register_method("fake_legacy", {s: lambda ctx, agent: StageResult() for s in STAGE_ORDER})
    db = SessionLocal()
    agent = make_agent(db, "fake_legacy", name="legacy-agent")
    deployment, job = create_deployment(db, agent)
    # a pre-upgrade job that already passed `generate` + `package` has no key to read
    deployment.stages = [
        {**s, "status": "succeeded" if s["name"] in ("generate", "package") else "pending"}
        for s in deployment.stages
    ]
    job.status = "running"
    db.commit()
    agent_id, deployment_id, job_id = agent.id, deployment.id, job.id
    db.close()

    execute_deploy_job(job_id)

    db = SessionLocal()
    job = db.get(Job, job_id)
    row_spec = db.get(Agent, agent_id).spec
    assert job.status == "succeeded" and job.payload["packaged_spec"] == row_spec
    db.close()
    assert _snapshot_for(deployment_id).spec == row_spec
