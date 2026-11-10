"""N16-obs83 / N16-obs82 — a delete during a deploy job, and preset name families.

N16-obs83 (live, t1, build aaacb0a6, 2026-10-19): ``DELETE /api/agents/{id}`` while the
agent's deploy job was in `package` answered 200 ``{removed: [runtime, execution_role]}``
and the job kept running through provision / deploy / register to "succeeded" — an
AgentCore Runtime (READY), an execution role + inline policy, the S3 package, a log group
and a PENDING_APPROVAL registry record with no agent row. Two belts close it:

* the route refuses a delete while a deploy job is `queued` / `running` — 409
  ``agent.deploy_in_progress {job_id, stage, job_status}``, members and admins alike;
* the pipeline re-reads the agent row before and after every stage and ends the job
  ``cancelled`` (honest log line, no further stage, no further AWS call) when the row is
  gone or marked deleted; a create-mode job then tears down what its finished stages
  created (Runtime if the deploy stage ran, then ``teardown.cleanup_residue``) and
  records the report on the job payload. ``_finish`` never flips a deleted row back.

N16-obs82: ``launchpad-eval-worker`` (the worker's base name) was not reserved — only the
env-suffixed preset name was. Every preset's family (name, base, ``<base>-<suffix>``) is
now 422 ``agent.name_reserved {name, reserved_for}`` at every creation boundary.
"""

import json

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import pipeline, teardown
from app.deployer.pipeline import (
    CANCEL_REASON,
    CANCELLED,
    STAGE_ORDER,
    StageResult,
    create_deployment,
    execute_deploy_job,
    register_method,
)
from app.models.ledger import Agent, Deployment, Job, SpecSnapshot
from tests.conftest import settle_deploy_jobs

SPEC = {"name": "cancel-test-agent", "method": "zip_runtime", "system_prompt": "hi"}


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    yield launched


@pytest.fixture
def residue(monkeypatch):
    """`teardown.cleanup_residue` and the Runtime delete are recorded, never run (C8)."""
    calls: dict[str, list] = {"residue": [], "runtime": []}

    def fake_residue(agent, workspace, *, settings, log=None):
        calls["residue"].append((agent.id, agent.name, agent.resource_id))
        if log:
            log("fake residue sweep")
        return [teardown.TeardownStep(teardown.STEP_ROLE, teardown.DELETED, "role-x")]

    monkeypatch.setattr(teardown, "cleanup_residue", fake_residue)
    monkeypatch.setattr(
        agents_router, "delete_agent_cloud_resource",
        lambda agent, ws: calls["runtime"].append(agent.resource_id) or True,
    )
    return calls


def _reload(job_id, deployment_id, agent_id):
    db = SessionLocal()
    try:
        return db.get(Job, job_id), db.get(Deployment, deployment_id), db.get(Agent, agent_id)
    finally:
        db.close()


def _events(job: Job) -> list[dict]:
    return [json.loads(line) for line in job.log.splitlines() if line.strip()]


def _stage_status(deployment: Deployment) -> dict[str, str]:
    return {s["name"]: s["status"] for s in deployment.stages}


def _mark_deleted(agent_id: str) -> None:
    """What `routers/agents.mark_agent_deleted` does to the row, straight in the DB."""
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = "deleted"
        db.commit()
    finally:
        db.close()


def _drop_row(agent_id: str) -> None:
    """The raw race: the row physically gone (nothing in the product does this)."""
    db = SessionLocal()
    try:
        db.delete(db.get(Agent, agent_id))
        db.commit()
    finally:
        db.close()


def _write(agent_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def _fake_method(name: str, hooks: dict[str, callable], calls: list[str]) -> None:
    """Every stage records itself; ``hooks[stage]`` runs inside that stage first."""

    def stage(ctx, agent, _s):
        calls.append(_s)
        if _s in hooks:
            hooks[_s](ctx, agent)
        return StageResult(detail=f"{_s} done")

    register_method(
        name, {s: (lambda ctx, agent, _s=s: stage(ctx, agent, _s)) for s in STAGE_ORDER},
    )


def _agent(db, method: str, name: str = "cancel-agent") -> Agent:
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method,
                  status="deploying", spec={"name": name})
    db.add(agent)
    db.commit()
    return agent


# ─── belt 1: the route ─────────────────────────────────────────────────────────────────────


def _stage_in_flight(agent_id: str, stage: str) -> str:
    """Shape the (stubbed, still `queued`) deploy job as a live job at ``stage``."""
    db = SessionLocal()
    try:
        dep = db.query(Deployment).filter(Deployment.agent_id == agent_id).one()
        stages = [dict(s) for s in dep.stages]
        for s in stages:
            if STAGE_ORDER.index(s["name"]) < STAGE_ORDER.index(stage):
                s["status"] = "succeeded"
            elif s["name"] == stage:
                s["status"] = "running"
        dep.stages = stages
        job = db.get(Job, dep.job_id)
        job.status = "running"
        db.commit()
        return job.id
    finally:
        db.close()


@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_delete_during_a_deploy_stage_is_409_and_touches_nothing(client, monkeypatch, stage):
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached during a deploy"))
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    job_id = _stage_in_flight(agent_id, stage)

    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "agent.deploy_in_progress"
    assert body["detail"] == {"job_id": job_id, "stage": stage, "job_status": "running"}
    # untouched: the row still reads, still deploying, the name is still taken
    assert client.get(f"/api/agents/{agent_id}").json()["status"] == "deploying"
    assert client.post("/api/agents", json=SPEC).json()["code"] == "agent.name_exists"


def test_delete_of_a_queued_job_is_409_until_the_job_is_terminal(client, monkeypatch):
    """A job that has not started is in flight too (the startup resume adopts it); once
    the job is terminal — succeeded or failed — the delete goes through."""
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, w: None)
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 409 and res.json()["code"] == "agent.deploy_in_progress"
    assert res.json()["detail"]["stage"] == "generate"
    assert res.json()["detail"]["job_status"] == "queued"
    settle_deploy_jobs(agent_id, status="failed")
    assert client.delete(f"/api/agents/{agent_id}").status_code == 200


@pytest.mark.parametrize("stage", STAGE_ORDER)
def test_delete_issued_while_a_stage_runs_is_409_and_the_job_runs_to_success(
    client, residue, stage,
):
    """The live sequence, end to end: the DELETE arrives while ``stage`` is executing (the
    stage itself issues it); the route answers 409, the job continues through every
    remaining stage and finishes `succeeded`; nothing was torn down."""
    seen: dict = {}
    calls: list[str] = []

    def delete_now(ctx, agent):
        seen["res"] = client.delete(f"/api/agents/{agent.id}")

    _fake_method(f"fake_409_{stage}", {stage: delete_now}, calls)
    db = SessionLocal()
    agent = _agent(db, f"fake_409_{stage}", name=f"api-del-{stage}")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    assert seen["res"].status_code == 409, seen["res"].text
    assert seen["res"].json()["detail"]["stage"] == stage
    assert seen["res"].json()["detail"]["job_id"] == job.id
    assert calls == STAGE_ORDER
    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == "succeeded" and deployment.status == "succeeded"
    assert agent.status == "active"
    assert residue == {"residue": [], "runtime": []}
    # ...and now that the job is terminal, the delete is accepted
    assert client.delete(f"/api/agents/{agent.id}").status_code == 200


# ─── belt 2: the pipeline ──────────────────────────────────────────────────────────────────


def test_row_marked_deleted_between_stages_cancels_the_job_and_sweeps_the_residue(residue):
    """The product's delete marks the row `deleted`. Landing while `provision` ran: the
    job notices right after that stage, never runs deploy / register, ends `cancelled`,
    leaves the row `deleted`, and tears down what package + provision created (the
    residue sweep — no Runtime exists yet, so no Runtime delete)."""
    calls: list[str] = []
    _fake_method("fake_cancel_mark", {"provision": lambda ctx, agent: _mark_deleted(agent.id)},
                 calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_mark")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    assert calls == ["generate", "package", "provision"]  # deploy / register never ran
    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == CANCELLED and job.error == CANCEL_REASON
    assert deployment.status == CANCELLED and deployment.ended_at is not None
    assert _stage_status(deployment) == {
        "generate": "succeeded", "package": "succeeded", "provision": "succeeded",
        "deploy": CANCELLED, "register": "pending",
    }
    assert agent.status == "deleted"  # never flipped back to active / failed
    assert job.payload["cancelled"]["stage"] == "deploy"
    assert job.payload["cancelled"]["reason"] == "agent_deleted"
    assert [s["step"] for s in job.payload["cancelled"]["teardown"]] == [teardown.STEP_ROLE]
    assert residue["residue"] == [(agent.id, agent.name, None)]
    assert residue["runtime"] == []  # the deploy stage never ran — no runtime to delete
    events = _events(job)
    assert any(e["msg"] == CANCEL_REASON and e["level"] == "warning" and e["stage"] == "deploy"
               for e in events)
    assert any(e["stage"] == "teardown" and "fake residue sweep" in e["msg"] for e in events)


def test_row_deleted_after_the_deploy_stage_also_deletes_the_runtime_the_job_created(residue):
    """The deploy stage recorded the Runtime it created (`resource_id`) and then the row
    went away — physically (the raw race): the snapshot taken after that stage still
    names it, so the cancel deletes the Runtime first, then the residue."""
    calls: list[str] = []

    def deploy(ctx, agent):
        _write(agent.id, resource_id="rt-cancel-1", arn="arn:aws-cn:x:y:z:runtime/rt-cancel-1")

    _fake_method("fake_cancel_drop", {"deploy": deploy}, calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_drop", name="cancel-drop")
    deployment, job = create_deployment(db, agent)
    db.close()
    agent_id = agent.id

    # the row disappears right after the deploy stage: hook the snapshot refresh
    real_snapshot = pipeline._agent_snapshot

    def snapshot_then_drop(db_, aid):
        snap = real_snapshot(db_, aid)
        if snap is not None and snap.resource_id == "rt-cancel-1":
            _drop_row(aid)
        return snap

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "_agent_snapshot", snapshot_then_drop)
        execute_deploy_job(job.id)

    assert calls == ["generate", "package", "provision", "deploy"]
    job, deployment, agent = _reload(job.id, deployment.id, agent_id)
    assert agent is None  # gone for good; the ledger was not resurrected
    assert job.status == CANCELLED and deployment.status == CANCELLED
    assert _stage_status(deployment)["register"] == CANCELLED
    assert residue["runtime"] == ["rt-cancel-1"]
    assert residue["residue"] == [(agent_id, "cancel-drop", "rt-cancel-1")]
    steps = job.payload["cancelled"]["teardown"]
    assert [(s["step"], s["status"]) for s in steps] == [
        (teardown.STEP_RUNTIME, teardown.DELETED), (teardown.STEP_ROLE, teardown.DELETED),
    ]


def test_row_vanishing_inside_a_stage_reads_as_cancelled_not_failed(residue):
    """The stage itself blew up because the row was gone under it: the exception is a
    symptom, the delete is the truth — `cancelled`, the stage marked so, the error
    still in the log (warning), and the job's own failure path never ran."""
    calls: list[str] = []

    def vanish(ctx, agent):
        _drop_row(agent.id)
        raise RuntimeError("row vanished under the stage")

    _fake_method("fake_cancel_inside", {"package": vanish}, calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_inside", name="cancel-inside")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    assert calls == ["generate", "package"]
    job, deployment, _ = _reload(job.id, deployment.id, agent.id)
    assert job.status == CANCELLED
    assert _stage_status(deployment)["package"] == CANCELLED
    events = _events(job)
    assert any("row vanished" in e["msg"] and e["level"] == "warning" for e in events)
    assert not any(e["level"] == "error" for e in events)
    # the snapshot from after `generate` names the row; nothing past generate finished,
    # but package was RUNNING (it may have uploaded) — so the sweep still runs
    assert residue["residue"] == [(agent.id, "cancel-inside", None)]


def test_delete_during_the_last_stage_never_resurrects_the_row(residue):
    """`_finish` used to write `agent.status = "active"` unconditionally — the live orphan
    read as a succeeded job. A delete that lands during `register` leaves the row deleted."""
    calls: list[str] = []
    _fake_method("fake_cancel_last", {"register": lambda ctx, agent: _mark_deleted(agent.id)},
                 calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_last", name="cancel-last")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    assert calls == STAGE_ORDER
    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert agent.status == "deleted"
    assert job.status == CANCELLED and deployment.status == CANCELLED
    assert job.payload["cancelled"]["stage"] is None
    assert all(v == "succeeded" for v in _stage_status(deployment).values())
    assert any(e["stage"] == "job" and e["msg"] == CANCEL_REASON for e in _events(job))


def test_an_update_mode_job_cancelled_tears_down_nothing_of_its_own(residue):
    """A redeploy rides the agent's existing Runtime: the role, artifacts and record belong
    to the agent's delete (which already ran), so the cancelled job touches nothing and
    says so."""
    calls: list[str] = []
    _fake_method("fake_cancel_update", {"deploy": lambda ctx, agent: _mark_deleted(agent.id)},
                 calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_update", name="cancel-update")
    agent.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/live"
    agent.resource_id = "live"
    db.commit()
    deployment, job = create_deployment(db, agent, mode="update")
    db.close()

    execute_deploy_job(job.id)

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == CANCELLED and agent.status == "deleted"
    assert residue == {"residue": [], "runtime": []}
    assert job.payload["cancelled"]["teardown"] == []
    assert any("update-mode job" in e["msg"] for e in _events(job))


def test_cancel_before_anything_was_created_has_nothing_to_tear_down(residue):
    calls: list[str] = []
    _fake_method("fake_cancel_early", {"generate": lambda ctx, agent: _mark_deleted(agent.id)},
                 calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_early", name="cancel-early")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    assert calls == ["generate"]
    job, deployment, _ = _reload(job.id, deployment.id, agent.id)
    assert job.status == CANCELLED and _stage_status(deployment)["package"] == CANCELLED
    assert residue == {"residue": [], "runtime": []}
    assert any("nothing to tear down" in e["msg"] for e in _events(job))


def test_a_cancelled_job_is_terminal_for_the_route_and_the_resume():
    """`cancelled` is terminal: the delete route no longer sees a live job (the row is
    already deleted anyway — 404) and the startup resume does not pick it up."""
    calls: list[str] = []
    _fake_method("fake_cancel_terminal", {"package": lambda ctx, agent: _mark_deleted(agent.id)},
                 calls)
    db = SessionLocal()
    agent = _agent(db, "fake_cancel_terminal", name="cancel-terminal")
    deployment, job = create_deployment(db, agent)
    db.close()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(teardown, "cleanup_residue", lambda *a, **k: [])
        execute_deploy_job(job.id)
    db = SessionLocal()
    try:
        assert pipeline.in_flight_deploy_job(db, agent.id) is None
    finally:
        db.close()
    assert job.id not in pipeline.resume_pending_jobs()


# ─── N16-obs82: preset name families ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "launchpad-eval-worker",            # the evaluation worker's BASE name (the live hole)
    "launchpad-eval-worker-t1",         # another environment's suffixed worker name
    "aws-agent-solution-architect",     # the architect's reserved name (= its base)
    "aws-agent-solution-architect-v2",  # a suffixed variant of the architect's family
])
def test_reserved_preset_name_families_are_422_before_any_row(client, no_real_deploy, name):
    from app.system_agents.presets import PRESETS, reserved_for

    res = client.post("/api/agents", json={**SPEC, "name": name})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "agent.name_reserved"
    assert body["detail"] == {"name": name, "reserved_for": reserved_for(name).key}
    assert body["detail"]["reserved_for"] in PRESETS
    assert no_real_deploy == [] and client.get("/api/agents").json()["agents"] == []


def test_the_current_environments_suffixed_worker_name_is_still_reserved(client):
    from app.system_agents.presets import EVAL_WORKER

    res = client.post("/api/agents", json={**SPEC, "name": EVAL_WORKER.name})
    assert res.status_code == 422 and res.json()["detail"]["reserved_for"] == EVAL_WORKER.key


@pytest.mark.parametrize("name", ["launchpad-eval-workers", "launchpad-eval", "my-eval-worker"])
def test_names_that_only_resemble_a_preset_family_are_accepted(client, no_real_deploy, name):
    """The family is `<base>` and `<base>-<suffix>` exactly — a near-miss is ordinary."""
    res = client.post("/api/agents", json={**SPEC, "name": name})
    assert res.status_code == 202, res.text


def test_reserved_family_is_derived_from_the_presets_table():
    from app.system_agents import presets

    for preset in presets.PRESETS.values():
        base = presets.family_base(preset)
        assert presets.reserved_for(preset.name) is preset
        assert presets.reserved_for(base) is preset
        assert presets.reserved_for(f"{base}-anything") is preset
        assert presets.is_reserved_name(f"{base}-anything")
    assert presets.reserved_for("hr-helpdesk") is None


# ─── N16-obs83, live fact 13:30Z: the FINALIZER resurrected the deleted row ─────────────


def _snapshot_of(deployment_id: str) -> SpecSnapshot | None:
    db = SessionLocal()
    try:
        return db.query(SpecSnapshot).filter(SpecSnapshot.deployment_id == deployment_id).first()
    finally:
        db.close()


def test_delete_during_the_deploy_stage_leaves_the_row_gone_and_nothing_registered(
    client, residue,
):
    """The lead's exact scenario: the row is deleted while `deploy` runs and the job is let
    run on. Register never runs (no registry record), the spec snapshot is never stamped
    as deployed, the row reads 404 and stays `deleted`, the job is `cancelled`, and the
    Runtime the stage created is torn down."""
    calls: list[str] = []

    def deploy(ctx, agent):
        _write(agent.id, resource_id="rt-live-1", arn="arn:aws-cn:x:y:z:runtime/rt-live-1",
               version="1")
        _mark_deleted(agent.id)  # the DELETE lands while CreateAgentRuntime is in flight

    _fake_method("fake_resurrect", {"deploy": deploy}, calls)
    db = SessionLocal()
    agent = _agent(db, "fake_resurrect", name="resurrect-me")
    deployment, job = create_deployment(db, agent)
    db.close()
    assert _snapshot_of(deployment.id).aws_version is None  # recorded at publish, unstamped

    execute_deploy_job(job.id)

    assert calls == ["generate", "package", "provision", "deploy"]  # register never ran
    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert agent.status == "deleted" and agent.registry_record_id is None
    assert client.get(f"/api/agents/{agent.id}").status_code == 404
    assert all(a["id"] != agent.id for a in client.get("/api/agents").json()["agents"])
    assert job.status == CANCELLED and deployment.status == CANCELLED
    snap = _snapshot_of(deployment.id)
    assert snap.aws_version is None  # `_finish` never stamped it as deployed
    assert residue["runtime"] == ["rt-live-1"]
    assert residue["residue"] == [(agent.id, "resurrect-me", "rt-live-1")]
    # the teardown is recorded on the job: payload steps + `teardown`-stage log events
    steps = job.payload["cancelled"]["teardown"]
    assert [(s["step"], s["status"]) for s in steps] == [
        (teardown.STEP_RUNTIME, teardown.DELETED), (teardown.STEP_ROLE, teardown.DELETED),
    ]
    events = _events(job)
    assert any(e["stage"] == "teardown" and "deleted runtime rt-live-1" in e["msg"]
               for e in events)
    assert any(e["stage"] == "teardown" and "fake residue sweep" in e["msg"] for e in events)


def test_the_real_register_stage_skips_a_deleted_row_and_creates_no_record(monkeypatch):
    """(b) The register stage itself — not only the loop in front of it — re-reads the
    row and skips when it is deleted: no `register_agent_record` call, no record id on
    the row."""
    from app.deployer import registration
    from app.deployer.pipeline import StageContext
    from tests.conftest import ws_ctx

    monkeypatch.setattr(registration, "register_agent_record",
                        lambda *a, **k: pytest.fail("a registry record was created"))
    db = SessionLocal()
    agent = _agent(db, "zip_runtime", name="register-guard")
    db.close()
    _mark_deleted(agent.id)
    logged: list[str] = []
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j",
                       workspace=ws_ctx({}, id=DEFAULT_WORKSPACE_ID), log=logged.append)

    result = registration.register_stage(ctx, agent)

    assert result.skipped and result.detail == registration.AGENT_DELETED_DETAIL
    assert logged == [registration.AGENT_DELETED_DETAIL]
    db = SessionLocal()
    try:
        row = db.get(Agent, agent.id)
        assert row.status == "deleted" and row.registry_record_id is None
    finally:
        db.close()
    # a physically missing row skips the same way
    _drop_row(agent.id)
    assert registration.register_stage(ctx, agent).skipped


@pytest.mark.parametrize("error", [None, "RuntimeError: late failure"])
def test_the_finalizer_itself_refuses_to_write_onto_a_deleted_row(error):
    """Defence in depth: `_finish` called directly (success or failure path) on a row that
    is `deleted` writes nothing onto it — no `active`, no `failed`, no stamped snapshot —
    and closes the job `cancelled` with the symptom still in the log."""
    db = SessionLocal()
    agent = _agent(db, "zip_runtime", name="finalizer-guard")
    agent.version = "7"
    deployment, job = create_deployment(db, agent)
    db.close()
    _mark_deleted(agent.id)

    db = SessionLocal()
    try:
        pipeline._finish(db, job.id, deployment.id, agent.id, error=error)
    finally:
        db.close()

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert agent.status == "deleted" and agent.error is None
    assert job.status == CANCELLED and deployment.status == CANCELLED
    assert job.payload["cancelled"]["finalizer_refused"] is True
    assert _snapshot_of(deployment.id).aws_version is None
    events = _events(job)
    assert any("finalizer refused" in e["msg"] for e in events)
    if error:
        assert any(e["msg"] == error and e["level"] == "warning" for e in events)


def test_a_job_level_failure_on_a_deleted_row_is_cancelled_not_failed():
    """`_fail_job` (a failure outside any stage) used to write `failed` onto the agent —
    an un-delete as well. On a deleted row it leaves the row alone and cancels the job."""
    db = SessionLocal()
    agent = _agent(db, "zip_runtime", name="fail-job-guard")
    deployment, job = create_deployment(db, agent)
    db.close()
    _mark_deleted(agent.id)

    db = SessionLocal()
    try:
        pipeline._fail_job(db, job.id, RuntimeError("workspace gone"))
    finally:
        db.close()

    job, deployment, agent = _reload(job.id, deployment.id, agent.id)
    assert agent.status == "deleted" and agent.error is None
    assert job.status == CANCELLED and deployment.status == CANCELLED
    assert any("workspace gone" in e["msg"] for e in _events(job))


def test_a_physically_missing_row_never_comes_back_by_insert(residue):
    """Design for both shapes deploy-ops may report: the row gone by DELETE (not only
    marked). No stage, finalizer or teardown re-INSERTs it — the ledger stays empty."""
    calls: list[str] = []
    _fake_method("fake_no_insert", {"deploy": lambda ctx, agent: _drop_row(agent.id)}, calls)
    db = SessionLocal()
    agent = _agent(db, "fake_no_insert", name="no-insert")
    deployment, job = create_deployment(db, agent)
    db.close()

    execute_deploy_job(job.id)

    job, deployment, row = _reload(job.id, deployment.id, agent.id)
    assert row is None
    assert job.status == CANCELLED and deployment.status == CANCELLED
    db = SessionLocal()
    try:
        assert db.query(Agent).filter(Agent.name == "no-insert").count() == 0
    finally:
        db.close()


def test_an_update_mode_cancel_still_removes_a_record_the_re_register_recreated(monkeypatch):
    """Update-mode jobs tear down nothing of their own — except the registry record a
    `register` stage that ran after the delete may have recreated."""
    seen: list[str] = []
    monkeypatch.setattr(teardown, "cleanup_residue",
                        lambda *a, **k: pytest.fail("full residue sweep on an update job"))
    monkeypatch.setattr(
        teardown, "_step_registry_record",
        lambda agent, ws, log: seen.append(agent.registry_record_id) or teardown.TeardownStep(
            teardown.STEP_REGISTRY, teardown.DELETED, "rec-9"),
    )
    calls: list[str] = []

    def register(ctx, agent):
        _write(agent.id, registry_record_id="rec-9")
        _mark_deleted(agent.id)

    _fake_method("fake_update_rereg", {"register": register}, calls)
    db = SessionLocal()
    agent = _agent(db, "fake_update_rereg", name="update-rereg")
    agent.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/live"
    agent.resource_id = "live"
    db.commit()
    deployment, job = create_deployment(db, agent, mode="update")
    db.close()

    execute_deploy_job(job.id)

    job, _, agent = _reload(job.id, deployment.id, agent.id)
    assert job.status == CANCELLED and agent.status == "deleted"
    assert seen == ["rec-9"]
    assert [s["step"] for s in job.payload["cancelled"]["teardown"]] == [teardown.STEP_REGISTRY]
