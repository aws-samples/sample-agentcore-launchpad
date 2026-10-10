"""Issue #246: CLEAR on an architect conversation whose Agent ran a canary.

The purge must stop and remove the canary (A/B test, online evaluations, dedicated
gateway, BOTH named Harness endpoints) and read them back gone BEFORE DeleteHarness —
which answers ConflictException while a named endpoint exists — then read the Harness
back gone before its role and the ledger rows go. A skipped cleanup, a resource still
DELETING, a conflict or an unowned dependency stops the job with a blocker and keeps
the conversation; the next CLEAR resumes from the verified checkpoints.

The AWS boundary is faked at the seams the purge calls: ``canary_service.act_cleanup``
/ ``remaining_resources``, ``experiment_service.act_cleanup``, ``purge.agent_remaining``
/ ``foreign_endpoints`` and the agents router's split teardown.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.assistant import purge
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantProposal
from app.models.ledger import Agent, Job
from app.optimization import canary_service
from app.optimization import service as experiment_service
from app.optimization.models import Experiment, RuntimeCanary
from app.routers import agents as agents_router
from tests.conftest import ws_ctx

BASE = "/api/assistant/architect/conversations"


# ---------------------------------------------------------------------------
# ledger fixtures
# ---------------------------------------------------------------------------


def _agent(db, name: str, resource_id: str, *, workspace_id=DEFAULT_WORKSPACE_ID,
           system_key=None) -> Agent:
    agent = Agent(workspace_id=workspace_id, name=name, method="harness", status="active",
                  owner="river", spec={"name": name, "method": "harness"},
                  arn=f"arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/{resource_id}",
                  resource_id=resource_id, system_key=system_key)
    db.add(agent)
    db.flush()
    return agent


def _canary(db, agent: Agent, *, status="running", running_action=None,
            workspace_id=DEFAULT_WORKSPACE_ID) -> RuntimeCanary:
    row = RuntimeCanary(
        workspace_id=workspace_id, name=f"CANARY-{agent.name}", champion_agent_id=agent.id,
        champion_agent_name=agent.name, challenger_agent_id=agent.id,
        challenger_agent_name=agent.name, status=status, running_action=running_action,
        artifacts={"kind": "harness",
                   "agent_meta": {"id": agent.id, "resource_id": agent.resource_id},
                   "setup": {"gateway_id": f"gw-{agent.resource_id}", "ab_test_id": "ab-1",
                             "runtime_id": agent.resource_id,
                             "champion": {"online_eval_id": "oe-c"},
                             "challenger": {"online_eval_id": "oe-t"}}})
    db.add(row)
    db.flush()
    return row


def _world() -> dict[str, str]:
    """A conversation whose approval deployed one Harness Agent with a running canary
    and an experiment, plus an unrelated Agent + canary and the architect preset."""
    db = SessionLocal()
    try:
        conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="river",
                                     owner_principal="local-operator", title="canary demo",
                                     turns=3)
        db.add(conv)
        db.flush()
        agent = _agent(db, "kid-companion", "kidHarness")
        db.add(AssistantProposal(
            workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id, revision=1,
            source="model", content={"name": "kid-companion"}, content_hash="h" * 64,
            status="approved", created_by="river", approved_by="river",
            approved_at=datetime.now(UTC), agent_id=agent.id))
        db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id,
                                turn=1, role="user", text="hi"))
        canary = _canary(db, agent)
        exp = Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="EXP-kid", agent_id=agent.id,
                         agent_name=agent.name, status="ready", artifacts={})
        db.add(exp)
        other = _agent(db, "unrelated", "otherHarness")
        other_canary = _canary(db, other)
        preset = _agent(db, "aws-agent-solution-architect", "presetHarness",
                        system_key="aws-agent-solution-architect")
        db.commit()
        return {"cid": conv.id, "agent": agent.id, "canary": canary.id, "exp": exp.id,
                "other": other.id, "other_canary": other_canary.id, "preset": preset.id}
    finally:
        db.close()


def _get(model, row_id):
    db = SessionLocal()
    try:
        row = db.get(model, row_id)
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# the fake AWS boundary
# ---------------------------------------------------------------------------


class Cloud:
    """Records the order of every teardown effect and serves scripted readbacks."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.canary_results: list[dict[str, str]] = [
            {"category": "abtest:ab-1", "status": "deleted", "detail": ""},
            {"category": "endpoint:ctl", "status": "deleted", "detail": ""},
            {"category": "endpoint:trt", "status": "deleted", "detail": ""},
            {"category": "gateway:gw", "status": "deleted", "detail": ""},
        ]
        # each readback pops the next answer; the last one repeats
        self.canary_readbacks: list[list[dict[str, str]]] = [[]]
        self.agent_readbacks: list[list[dict[str, str]]] = [[]]
        self.foreign: list[dict[str, str]] = []
        self.delete_error: Exception | None = None
        self.role_ok = True
        self.experiment_results: list[dict[str, str]] = [
            {"category": "abtest:x", "status": "deleted", "detail": ""}]

    def canary_gone(self) -> bool:
        return not self.canary_readbacks[0]


def _pop(queue: list):
    return queue.pop(0) if len(queue) > 1 else queue[0]


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    create_app()
    c = Cloud()
    monkeypatch.setattr(purge, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(purge, "_sleep", lambda _s: None)

    def act_cleanup(canary_id, progress):
        c.events.append(f"canary-cleanup:{canary_id}")
        progress("tearing down")
        canary_service._update(canary_id, status="cleaned", stage="cleanup",
                               artifact={"cleanup": list(c.canary_results)})
        return list(c.canary_results)

    def remaining(row, workspace):
        answer = _pop(c.canary_readbacks)
        c.events.append(f"canary-readback:{row.id}:{len(answer)}")
        return answer

    def exp_cleanup(exp_id, progress):
        c.events.append(f"experiment-cleanup:{exp_id}")
        experiment_service._update(exp_id, status="cleaned", stage="cleanup",
                                   artifact={"cleanup": list(c.experiment_results)})
        return list(c.experiment_results)

    def delete_resource(agent, workspace):
        c.events.append(f"agent-delete:{agent.id}")
        if c.delete_error is not None:
            raise c.delete_error
        return True

    def agent_remaining(agent, workspace):
        answer = _pop(c.agent_readbacks)
        c.events.append(f"agent-readback:{agent.id}:{len(answer)}")
        return answer

    def delete_role(agent, workspace):
        c.events.append(f"agent-role:{agent.id}")
        return c.role_ok

    monkeypatch.setattr(canary_service, "act_cleanup", act_cleanup)
    monkeypatch.setattr(canary_service, "remaining_resources", remaining)
    monkeypatch.setattr(experiment_service, "act_cleanup", exp_cleanup)
    monkeypatch.setattr(agents_router, "delete_agent_cloud_resource", delete_resource)
    monkeypatch.setattr(agents_router, "delete_agent_role", delete_role)
    monkeypatch.setattr(purge, "agent_remaining", agent_remaining)
    monkeypatch.setattr(purge, "foreign_endpoints", lambda agent, ws: list(c.foreign))
    return c


def _purge(cid: str):
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        return purge.purge(db, row, ws_ctx(), can=lambda _p: True)
    finally:
        db.close()


def _footprint(cid: str) -> dict:
    db = SessionLocal()
    try:
        return purge.footprint(db, db.get(AssistantConversation, cid))
    finally:
        db.close()


class ConflictException(Exception):
    """Named like botocore's modeled error — the purge keys on the class name."""


# ---------------------------------------------------------------------------
# footprint + ordering
# ---------------------------------------------------------------------------


def test_footprint_discloses_the_canary_and_experiment_and_their_resources(cloud):
    ids = _world()
    fp = _footprint(ids["cid"])
    assert [a["id"] for a in fp["agents"]] == [ids["agent"]]
    assert [c["id"] for c in fp["canaries"]] == [ids["canary"]]  # never the unrelated one
    canary = fp["canaries"][0]
    assert canary["pending"] is True and canary["kind"] == "harness"
    assert canary["resources"]["ab_test"] is True
    assert canary["resources"]["gateway"] == "gw-kidHarness"
    assert canary["resources"]["online_evaluations"] == 2
    assert canary["resources"]["endpoints"] == [f"ctl{ids['canary'][:6]}",
                                                f"trt{ids['canary'][:6]}"]
    assert [e["id"] for e in fp["experiments"]] == [ids["exp"]]
    assert fp["blockers"] == [] and fp["purge"] is None
    assert fp["required_permissions"] == ["agents.delete"]
    assert cloud.events == []  # a ledger read only


def test_a_running_canary_action_blocks_the_clear_before_anything_is_deleted(cloud):
    ids = _world()
    db = SessionLocal()
    try:
        db.get(RuntimeCanary, ids["canary"]).running_action = "traffic"
        db.commit()
    finally:
        db.close()
    fp = _footprint(ids["cid"])
    assert [b["kind"] for b in fp["blockers"]] == ["canary"]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.conversation_busy" and exc.value.status_code == 409
    assert cloud.events == []
    assert _get(AssistantConversation, ids["cid"]).purge_job_id is None


def test_clear_stops_the_canary_waits_for_its_endpoints_then_deletes_the_agent(cloud):
    """The reproduction: endpoints stay DELETING for two readbacks and the Harness for
    one; nothing about the Agent is touched until the canary reads back empty, and the
    role goes only after the Harness is read back gone."""
    ids = _world()
    endpoint = {"category": f"endpoint:ctl{ids['canary'][:6]}", "status": "DELETING"}
    cloud.canary_readbacks = [[endpoint], [endpoint], []]
    cloud.agent_readbacks = [[{"category": "harness:kidHarness", "status": "DELETING"}], []]
    result = _purge(ids["cid"])

    a, c, e = ids["agent"], ids["canary"], ids["exp"]
    assert cloud.events == [
        f"canary-cleanup:{c}",
        f"canary-readback:{c}:1", f"canary-readback:{c}:1", f"canary-readback:{c}:0",
        f"experiment-cleanup:{e}",
        f"agent-delete:{a}",
        f"agent-readback:{a}:1", f"agent-readback:{a}:0",
        f"agent-role:{a}",
    ]
    assert result["deleted"] is True and result["status"] == "succeeded"
    assert result["canaries"] == [{"id": c, "name": "CANARY-kid-companion"}]
    assert result["experiments"] == [{"id": e, "name": "EXP-kid"}]
    assert result["agents"] == [{"id": a, "name": "kid-companion", "aws_resource_deleted": True}]
    assert _get(AssistantConversation, ids["cid"]) is None
    assert _get(Agent, a).status == "deleted"
    # canary / experiment history is retained; the canary's own claim was released
    canary = _get(RuntimeCanary, c)
    assert canary.status == "cleaned" and canary.running_action is None
    assert _get(Experiment, e).status == "cleaned"
    # unrelated work and the system preset survive untouched
    assert _get(Agent, ids["other"]).status == "active"
    assert _get(RuntimeCanary, ids["other_canary"]).status == "running"
    assert _get(Agent, ids["preset"]).status == "active"


def test_an_already_cleaned_canary_is_still_read_back_before_the_harness_goes(cloud):
    """The incident's second half: a member's own canary cleanup said ``cleaned`` while
    the named endpoints were still DELETING. The purge does not re-run the cleanup, but
    it never trusts the status — it waits for the readback."""
    ids = _world()
    db = SessionLocal()
    try:
        db.get(RuntimeCanary, ids["canary"]).status = "cleaned"
        db.get(RuntimeCanary, ids["canary"]).artifacts = {
            **db.get(RuntimeCanary, ids["canary"]).artifacts,
            "cleanup": [{"category": "endpoint:ctl", "status": "deleted", "detail": ""}]}
        db.commit()
    finally:
        db.close()
    assert _footprint(ids["cid"])["canaries"][0]["pending"] is False
    cloud.canary_readbacks = [[{"category": "endpoint:ctl", "status": "DELETING"}], []]
    _purge(ids["cid"])
    c, a = ids["canary"], ids["agent"]
    assert f"canary-cleanup:{c}" not in cloud.events
    assert cloud.events.index(f"canary-readback:{c}:0") < cloud.events.index(f"agent-delete:{a}")


# ---------------------------------------------------------------------------
# incomplete states are blockers, and a retry resumes
# ---------------------------------------------------------------------------


def test_a_skipped_canary_cleanup_stops_the_clear_and_a_retry_resumes(cloud):
    ids = _world()
    cloud.canary_results = [
        {"category": "abtest:ab-1", "status": "deleted", "detail": ""},
        {"category": "gateway:gw", "status": "skipped", "detail": "ConflictException: busy"}]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_canary_incomplete"
    assert exc.value.detail["remaining"][0]["category"] == "gateway:gw"
    assert not any(e.startswith("agent-") for e in cloud.events)  # the Agent is untouched
    conv = _get(AssistantConversation, ids["cid"])
    assert conv is not None and _get(Agent, ids["agent"]).status == "active"
    failed = _get(Job, conv.purge_job_id)
    assert failed.status == "failed" and failed.payload["blocker"]["code"] == (
        "assistant.purge_canary_incomplete")
    # the canary row says cleaned, but the footprint still treats it as pending
    fp = _footprint(ids["cid"])
    assert fp["canaries"][0]["pending"] is True
    assert fp["purge"]["status"] == "failed" and fp["blockers"] == []
    assert _get(RuntimeCanary, ids["canary"]).running_action is None  # claim released

    # the gateway drains; the second CLEAR is a new job that carries the evidence
    cloud.canary_results = [{"category": "gateway:gw", "status": "deleted", "detail": ""}]
    cloud.events.clear()
    result = _purge(ids["cid"])
    assert result["deleted"] is True
    assert cloud.events[0] == f"canary-cleanup:{ids['canary']}"
    db = SessionLocal()
    try:
        second = db.get(Job, result["job_id"])
        assert second.payload["previous_job_id"] == failed.id
    finally:
        db.close()


def test_endpoints_still_deleting_after_the_bounded_wait_is_a_retryable_blocker(
        cloud, monkeypatch):
    ids = _world()
    monkeypatch.setattr(purge, "CANARY_GONE_TIMEOUT_S", 0)
    cloud.canary_readbacks = [[{"category": "endpoint:ctl", "status": "DELETING"}]]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_still_deleting"
    assert "endpoint:ctl (DELETING)" in exc.value.message
    assert not any(e.startswith("agent-") for e in cloud.events)

    # later the endpoints are gone: the canary is not cleaned a second time (its
    # cleanup did complete) — only re-read, then the teardown continues
    cloud.canary_readbacks = [[]]
    cloud.events.clear()
    assert _purge(ids["cid"])["deleted"] is True
    assert f"canary-cleanup:{ids['canary']}" not in cloud.events
    assert cloud.events[0] == f"canary-readback:{ids['canary']}:0"


def test_delete_failed_on_a_canary_resource_is_reported_not_waited_out(cloud):
    ids = _world()
    cloud.canary_readbacks = [[{"category": "gateway:gw", "status": "DELETE_FAILED"}]]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_delete_failed"
    assert cloud.events.count(f"canary-readback:{ids['canary']}:1") == 1


def test_a_delete_harness_conflict_keeps_the_conversation_and_the_agent(cloud):
    ids = _world()
    cloud.delete_error = ConflictException("endpoints exist")
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_agent_conflict"
    assert _get(AssistantConversation, ids["cid"]) is not None
    assert _get(Agent, ids["agent"]).status == "active"
    assert f"agent-role:{ids['agent']}" not in cloud.events


def test_an_unowned_named_endpoint_blocks_before_any_delete_request(cloud):
    """An endpoint no canary of this conversation created is not this purge's to
    remove — it is surfaced, and DeleteHarness is never sent."""
    ids = _world()
    cloud.foreign = [{"category": "endpoint:prodPinned", "status": "READY"}]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_agent_dependency"
    assert "endpoint:prodPinned" in exc.value.message
    assert f"agent-delete:{ids['agent']}" not in cloud.events


def test_a_lingering_harness_keeps_the_agent_in_the_footprint_until_read_back_gone(
        cloud, monkeypatch):
    """DeleteHarness accepted, then the bounded wait expires: the ledger may not flip
    and the role must stay. The retry does not send a second delete; it re-reads."""
    ids = _world()
    monkeypatch.setattr(purge, "AGENT_GONE_TIMEOUT_S", 0)
    cloud.agent_readbacks = [[{"category": "harness:kidHarness", "status": "DELETING"}]]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_still_deleting"
    a = ids["agent"]
    assert f"agent-role:{a}" not in cloud.events
    assert _get(Agent, a).status == "active"
    assert [x["id"] for x in _footprint(ids["cid"])["agents"]] == [a]

    cloud.agent_readbacks = [[]]
    cloud.events.clear()
    assert _purge(ids["cid"])["deleted"] is True
    assert f"agent-delete:{a}" not in cloud.events  # delete_requested carried forward
    assert cloud.events[-2:] == [f"agent-readback:{a}:0", f"agent-role:{a}"]


def test_a_role_that_cannot_be_deleted_is_a_blocker(cloud):
    ids = _world()
    cloud.role_ok = False
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_role_remains"
    assert _get(AssistantConversation, ids["cid"]) is not None


def test_a_skipped_experiment_cleanup_is_a_blocker(cloud):
    ids = _world()
    cloud.experiment_results = [{"category": "bundle:b", "status": "skipped", "detail": "x"}]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_experiment_incomplete"
    assert not any(e.startswith("agent-") for e in cloud.events)


def test_a_preset_reached_through_a_proposal_is_refused_and_survives(cloud):
    ids = _world()
    db = SessionLocal()
    try:
        prop = db.query(AssistantProposal).filter_by(conversation_id=ids["cid"]).one()
        prop.agent_id = ids["preset"]
        db.commit()
    finally:
        db.close()
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.status_code == 403
    assert f"agent-delete:{ids['preset']}" not in cloud.events
    assert _get(Agent, ids["preset"]).status == "active"


# ---------------------------------------------------------------------------
# durability: restart resume, fencing, concurrency
# ---------------------------------------------------------------------------


def test_a_job_a_crash_left_running_is_resumed_on_startup_only(cloud):
    ids = _world()
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, ids["cid"])
        job, done = purge.request_purge(db, row, can=lambda _p: True)
        assert done is None
        db.get(Job, job.id).status = "running"  # the process died mid-run
        db.commit()
        job_id = job.id
    finally:
        db.close()
    purge.execute_purge_job(job_id)  # an ordinary start never adopts a running job
    assert _get(Job, job_id).status == "running" and cloud.events == []
    purge.execute_purge_job(job_id, resume=True)
    assert _get(Job, job_id).status == "succeeded"
    assert _get(AssistantConversation, ids["cid"]) is None


def test_startup_resume_registers_the_purge_job_type(cloud, monkeypatch):
    from app.deployer import pipeline

    started: list[str] = []
    monkeypatch.setattr(purge, "start_purge_resume", lambda job_id: started.append(job_id))
    db = SessionLocal()
    try:
        db.add(Job(id="purgejob1", workspace_id=DEFAULT_WORKSPACE_ID, type=purge.JOB_TYPE,
                   status="running", payload={"conversation_id": "x"}))
        db.commit()
    finally:
        db.close()
    assert "purgejob1" in pipeline.resume_pending_jobs()
    assert started == ["purgejob1"]


def test_a_held_lock_makes_a_second_worker_inert(cloud):
    ids = _world()
    db = SessionLocal()
    try:
        job, _ = purge.request_purge(db, db.get(AssistantConversation, ids["cid"]),
                                     can=lambda _p: True)
        job_id = job.id
    finally:
        db.close()
    held = purge._acquire_lock(ids["cid"])
    try:
        purge.execute_purge_job(job_id)
        assert _get(Job, job_id).status == "queued" and cloud.events == []
    finally:
        purge._release_lock(held)
    purge.execute_purge_job(job_id)
    assert _get(Job, job_id).status == "succeeded"


def test_over_http_the_clear_is_a_job_and_everything_it_owns_is_fenced(cloud, monkeypatch):
    """DELETE answers 202 with a job; while it is queued the conversation refuses
    writes, the Agent refuses canary / experiment / delete / redeploy, a repeated DELETE
    returns the same job — and the job, once run, removes everything."""
    ids = _world()
    started: list[str] = []
    monkeypatch.setattr(purge, "start_purge_async", lambda job_id: started.append(job_id))
    client = TestClient(create_app())
    res = client.delete(f"{BASE}/{ids['cid']}")
    assert res.status_code == 202, res.text
    body = res.json()
    job_id = body["job_id"]
    assert body["deleted"] is False and body["status"] == "queued" and started == [job_id]

    again = client.delete(f"{BASE}/{ids['cid']}")
    assert again.status_code == 202 and again.json()["job_id"] == job_id

    turn = client.post(f"{BASE}/{ids['cid']}/turns", json={"prompt": "more"})
    assert turn.status_code == 409 and turn.json()["code"] == "assistant.conversation_clearing"
    action = client.post(f"/api/runtime-canaries/{ids['canary']}/action",
                         json={"action": "traffic", "dataset_id": "d"})
    assert action.status_code == 409 and action.json()["code"] == "assistant.agent_clearing"
    exp = client.post(f"/api/experiments/{ids['exp']}/action", json={"action": "cleanup"})
    assert exp.status_code == 409 and exp.json()["code"] == "assistant.agent_clearing"
    gone = client.delete(f"/api/agents/{ids['agent']}")
    assert gone.status_code == 409 and gone.json()["code"] == "assistant.agent_clearing"
    # the unrelated agent is not fenced
    assert purge.clearing_job_for_agent(SessionLocal(), ids["other"]) is None

    fp = client.get(f"{BASE}/{ids['cid']}/footprint").json()
    assert fp["purge"]["job_id"] == job_id and fp["purge"]["status"] == "queued"

    purge.execute_purge_job(job_id)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded", job
    assert job["payload"]["result"]["agents"][0]["id"] == ids["agent"]
    assert client.get(f"{BASE}/{ids['cid']}").status_code == 404


def test_a_transcript_only_conversation_is_still_deleted_inline(cloud):
    db = SessionLocal()
    try:
        conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="river",
                                     owner_principal="local-operator", title="chat")
        db.add(conv)
        db.commit()
        cid = conv.id
    finally:
        db.close()
    res = TestClient(create_app()).delete(f"{BASE}/{cid}")
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert SessionLocal().query(Job).filter(Job.type == purge.JOB_TYPE).count() == 0


# ---------------------------------------------------------------------------
# the real canary readback (fake clients)
# ---------------------------------------------------------------------------


class ResourceNotFoundException(Exception):
    pass


class _Control:
    def __init__(self, *, endpoints, gateway_status=None, online=()):
        self.endpoints, self.gateway_status, self.online = endpoints, gateway_status, online

    def list_online_evaluation_configs(self, **_kw):
        return {"onlineEvaluationConfigs": list(self.online)}

    def get_gateway(self, gatewayIdentifier):
        if self.gateway_status is None:
            raise ResourceNotFoundException(gatewayIdentifier)
        return {"status": self.gateway_status}

    def list_harness_endpoints(self, harnessId, **_kw):
        if self.endpoints is None:
            raise ResourceNotFoundException(harnessId)
        return {"endpoints": list(self.endpoints)}

    def list_gateway_targets(self, gatewayIdentifier, **_kw):
        raise ResourceNotFoundException(gatewayIdentifier)


class _Data:
    def __init__(self, tests=()):
        self.tests = tests

    def list_ab_tests(self, **_kw):
        return {"abTests": list(self.tests)}


def _readback(monkeypatch, row, control, data):
    from app.optimization import canary_service as real

    monkeypatch.setattr(real, "control_client", lambda _ws: control)
    monkeypatch.setattr(real, "data_client", lambda _ws: data)
    return real.remaining_resources(row, ws_ctx())


def test_canary_readback_reports_every_owned_resource_still_present(monkeypatch):
    create_app()
    db = SessionLocal()
    try:
        row = _canary(db, _agent(db, "rb", "rbHarness"))
        db.commit()
        db.expunge(row)
    finally:
        db.close()
    ctl, trt = f"ctl{row.id[:6]}", f"trt{row.id[:6]}"
    control = _Control(
        endpoints=[{"endpointName": "DEFAULT", "status": "READY"},
                   {"endpointName": ctl, "status": "DELETING"},
                   {"endpointName": trt, "status": "READY"},
                   {"endpointName": "someoneElse", "status": "READY"}],
        gateway_status="DELETING",
        online=[{"onlineEvaluationConfigId": "oe-c", "onlineEvaluationConfigName": "x",
                 "status": "DELETING"},
                {"onlineEvaluationConfigId": "foreign", "onlineEvaluationConfigName": "y"}])
    data = _Data([{"abTestId": "ab-1", "name": "n", "status": "DELETING"},
                  {"abTestId": "ab-foreign", "name": "other"}])
    remaining = _readback(monkeypatch, row, control, data)
    assert {r["category"] for r in remaining} == {
        "abtest:ab-1", "online-eval:oe-c", "gateway:gw-rbHarness",
        f"endpoint:{ctl}", f"endpoint:{trt}"}  # DEFAULT and unowned endpoints excluded

    # all gone — including the Harness itself (its endpoints went with it)
    assert _readback(monkeypatch, row, _Control(endpoints=None), _Data()) == []


def test_canary_cleanup_rerun_tolerates_its_dedicated_gateway_already_gone(monkeypatch):
    """A retry after the gateway was deleted must not crash listing its targets."""
    create_app()
    db = SessionLocal()
    try:
        row = _canary(db, _agent(db, "rr", "rrHarness"))
        db.commit()
        db.expunge(row)
    finally:
        db.close()
    from app.optimization import canary_service as real

    gateway_id, targets, _evals, abs_ = real._owned_resources(
        row, _Control(endpoints=[]), _Data())
    assert gateway_id == "gw-rrHarness" and targets == [] and abs_ == ["ab-1"]
