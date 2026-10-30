"""Upstream #246 / #254, CN port (N15-obs6): CLEAR on an architect conversation whose
Agent ran a Runtime canary.

The purge must stop and remove the canary (local A/B row, per-arm online evaluations,
BOTH named endpoints) and read them back gone BEFORE ``DeleteAgentRuntime`` — which
answers ``ConflictException`` while a named endpoint exists — then read the Runtime back
gone before the residue sweep (execution role, Registry record, …) and the ledger rows
go. A skipped cleanup, a resource still DELETING, a conflict, an unowned dependency or a
failed role / Registry step stops the job with a blocker and keeps the conversation; the
next CLEAR resumes from the verified checkpoints. Every canary and Agent the clear tears
down must be the caller's (creator-or-admin, ``users-and-ownership.md`` §8) before any
side effect.

The AWS boundary is faked at the seams the purge calls: ``canary_service.act_cleanup``
/ ``remaining_resources``, ``purge.agent_remaining`` / ``foreign_endpoints`` /
``agent_residue``, the agents router's ``delete_agent_cloud_resource`` and
``canary_infra.delete_object_quiet``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.assistant import purge
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import teardown
from app.evaluation.models import OnlineEvalConfig
from app.main import create_app
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantProposal
from app.models.ledger import Agent, Job
from app.optimization import canary_infra, canary_service
from app.optimization.models import RuntimeCanary
from app.routers import agents as agents_router
from app.routers.auth import ROLE_ADMIN, Identity
from tests.conftest import ws_ctx

BASE = "/api/assistant/architect/conversations"
ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{rid}"


# ---------------------------------------------------------------------------
# ledger fixtures
# ---------------------------------------------------------------------------


def _agent(db, name: str, resource_id: str, *, workspace_id=DEFAULT_WORKSPACE_ID,
           system_key=None, created_by=None) -> Agent:
    agent = Agent(workspace_id=workspace_id, name=name, method="zip_runtime", status="active",
                  spec={"name": name, "method": "zip_runtime"}, arn=ARN.format(rid=resource_id),
                  resource_id=resource_id, system_key=system_key, created_by=created_by)
    db.add(agent)
    db.flush()
    return agent


def _canary(db, agent: Agent, *, status="running", running_action=None,
            workspace_id=DEFAULT_WORKSPACE_ID, created_by=None) -> RuntimeCanary:
    row = RuntimeCanary(
        workspace_id=workspace_id, name=f"CANARY-{agent.name}", champion_agent_id=agent.id,
        champion_agent_name=agent.name, challenger_agent_id=agent.id,
        challenger_agent_name=agent.name, status=status, running_action=running_action,
        created_by=created_by, artifacts={})
    db.add(row)
    db.flush()
    tag = row.id[:6]
    row.artifacts = {
        "agent_meta": {"id": agent.id, "resource_id": agent.resource_id,
                       "runtime_name": agent.name},
        "setup": {"runtime_id": agent.resource_id, "gateway_id": canary_service.ROUTING_MODE,
                  "stable_endpoint": f"stable{tag}", "treatment_endpoint": f"treat{tag}",
                  "ab_test_id": f"local_{row.id}", "v_candidate": "2",
                  "candidate_s3_key": f"canary/{agent.name}/{row.id}-candidate.zip",
                  "champion": {"target_name": f"stable{tag}", "online_eval_id": "oe-c"},
                  "challenger": {"target_name": f"treat{tag}", "online_eval_id": "oe-t"}},
        "abtest": {"ab_test_id": f"local_{row.id}", "execution_status": "RUNNING"},
    }
    db.flush()
    return row


def _world(*, canary_created_by=None, agent_created_by=None) -> dict[str, str]:
    """A conversation whose approval deployed one Runtime Agent with a running canary,
    plus an unrelated Agent + canary and the architect preset."""
    db = SessionLocal()
    try:
        conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="river",
                                     owner_principal="local-operator", title="canary demo",
                                     turns=3)
        db.add(conv)
        db.flush()
        agent = _agent(db, "kid-companion", "kidRuntime", created_by=agent_created_by)
        db.add(AssistantProposal(
            workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id, revision=1,
            source="model", content={"name": "kid-companion"}, content_hash="h" * 64,
            status="approved", created_by="river", approved_by="river",
            approved_at=datetime.now(UTC), agent_id=agent.id))
        db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id,
                                turn=1, role="user", text="hi"))
        canary = _canary(db, agent, created_by=canary_created_by)
        other = _agent(db, f"unrelated-{conv.id[:6]}", f"otherRuntime{conv.id[:6]}")
        other_canary = _canary(db, other)
        preset = db.query(Agent).filter_by(system_key="aws-agent-solution-architect").first()
        if preset is None:  # one preset row per workspace (unique system_key)
            preset = _agent(db, "aws-agent-solution-architect", "presetRuntime",
                            system_key="aws-agent-solution-architect")
        db.commit()
        return {"cid": conv.id, "agent": agent.id, "canary": canary.id,
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
            {"category": "abtest:local", "status": "deleted", "detail": "local A/B row STOPPED"},
            {"category": "online-eval:oe-c", "status": "deleted", "detail": ""},
            {"category": "online-eval:oe-t", "status": "deleted", "detail": ""},
            {"category": "endpoint:stable", "status": "deleted", "detail": ""},
            {"category": "endpoint:treat", "status": "deleted", "detail": ""},
            {"category": "endpoints-gone", "status": "skipped",
             "detail": "still listed after 1s: stable (DELETING is eventually consistent)"},
            {"category": "s3:canary/kid-companion/x-candidate.zip", "status": "skipped",
             "detail": "artifact of the live version 2"},
        ]
        # each readback pops the next answer; the last one repeats
        self.canary_readbacks: list[list[dict[str, str]]] = [[]]
        self.agent_readbacks: list[list[dict[str, str]]] = [[]]
        self.foreign: list[dict[str, str]] = []
        self.delete_error: Exception | None = None
        self.residue: list[teardown.TeardownStep] = [
            teardown.TeardownStep(teardown.STEP_ROLE, teardown.DELETED, "launchpad-agent-kid"),
            teardown.TeardownStep(teardown.STEP_ARTIFACTS, teardown.DELETED, "agents/kid/"),
            teardown.TeardownStep(teardown.STEP_REGISTRY, teardown.SKIPPED,
                                  "agent has no registry record"),
            teardown.TeardownStep(teardown.STEP_LOGS, teardown.DELETED, "1 group(s)"),
        ]


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

    def delete_resource(agent, workspace):
        c.events.append(f"agent-delete:{agent.id}")
        if c.delete_error is not None:
            raise c.delete_error
        return True

    def agent_remaining(agent, workspace):
        answer = _pop(c.agent_readbacks)
        c.events.append(f"agent-readback:{agent.id}:{len(answer)}")
        return answer

    def residue(agent, workspace, log):
        c.events.append(f"agent-residue:{agent.id}")
        return list(c.residue)

    def delete_object(key, workspace, **_kw):
        c.events.append(f"artifact-delete:{key}")

    monkeypatch.setattr(canary_service, "act_cleanup", act_cleanup)
    monkeypatch.setattr(canary_service, "remaining_resources", remaining)
    monkeypatch.setattr(agents_router, "delete_agent_cloud_resource", delete_resource)
    monkeypatch.setattr(canary_infra, "delete_object_quiet", delete_object)
    monkeypatch.setattr(purge, "agent_remaining", agent_remaining)
    monkeypatch.setattr(purge, "agent_residue", residue)
    monkeypatch.setattr(purge, "foreign_endpoints", lambda agent, ws: list(c.foreign))
    return c


def _purge(cid: str, resources: dict | None = None, *, identity: Identity | None = None):
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        if identity is not None:
            return purge.purge(db, row, ws_ctx(resources), identity=identity)
        return purge.purge(db, row, ws_ctx(resources), can=lambda _p: True)
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


def test_footprint_discloses_the_canary_and_its_resources(cloud):
    ids = _world()
    fp = _footprint(ids["cid"])
    assert [a["id"] for a in fp["agents"]] == [ids["agent"]]
    assert [c["id"] for c in fp["canaries"]] == [ids["canary"]]  # never the unrelated one
    canary = fp["canaries"][0]
    assert canary["pending"] is True and canary["kind"] == "runtime"
    assert canary["resources"] == {
        "ab_test": True, "online_evaluations": 2,
        "endpoints": [f"stable{ids['canary'][:6]}", f"treat{ids['canary'][:6]}"]}
    assert "experiments" not in fp  # configuration experiments were retired (N13-obs2)
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
    """The reproduction: endpoints stay DELETING for two readbacks and the Runtime for
    one; nothing about the Agent is touched until the canary reads back empty, the
    residue sweep (role, Registry record, …) runs only after the Runtime is read back
    gone, and the retained candidate zip goes last."""
    ids = _world()
    endpoint = {"category": f"endpoint:stable{ids['canary'][:6]}", "status": "DELETING"}
    cloud.canary_readbacks = [[endpoint], [endpoint], []]
    cloud.agent_readbacks = [[{"category": "runtime:kidRuntime", "status": "DELETING"}], []]
    result = _purge(ids["cid"])

    a, c = ids["agent"], ids["canary"]
    assert cloud.events == [
        f"canary-cleanup:{c}",
        f"canary-readback:{c}:1", f"canary-readback:{c}:1", f"canary-readback:{c}:0",
        f"agent-delete:{a}",
        f"agent-readback:{a}:1", f"agent-readback:{a}:0",
        f"agent-residue:{a}",
        f"artifact-delete:canary/kid-companion/{c}-candidate.zip",
    ]
    assert result["deleted"] is True and result["status"] == "succeeded"
    assert result["canaries"] == [{"id": c, "name": "CANARY-kid-companion"}]
    assert "experiments" not in result
    agent_entry = result["agents"][0]
    assert (agent_entry["id"], agent_entry["name"], agent_entry["aws_resource_deleted"],
            agent_entry["registry_record"]) == (a, "kid-companion", True, "none")
    assert [s["step"] for s in agent_entry["teardown"]] == [
        teardown.STEP_ROLE, teardown.STEP_ARTIFACTS, teardown.STEP_REGISTRY, teardown.STEP_LOGS]
    assert _get(AssistantConversation, ids["cid"]) is None
    assert _get(Agent, a).status == "deleted"
    # canary history is retained; the canary's own claim was released
    canary = _get(RuntimeCanary, c)
    assert canary.status == "cleaned" and canary.running_action is None
    # unrelated work and the system preset survive untouched
    assert _get(Agent, ids["other"]).status == "active"
    assert _get(RuntimeCanary, ids["other_canary"]).status == "running"
    assert _get(Agent, ids["preset"]).status == "active"


def test_an_already_cleaned_canary_is_still_read_back_before_the_runtime_goes(cloud):
    """A member's own canary cleanup said ``cleaned`` while the named endpoints were
    still DELETING (N-SCN-obs3). The purge does not re-run the cleanup, but it never
    trusts the status — it waits for the readback."""
    ids = _world()
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, ids["canary"])
        row.status = "cleaned"
        row.artifacts = {**row.artifacts, "cleanup": [
            {"category": "endpoint:stable", "status": "deleted", "detail": ""},
            {"category": "endpoints-gone", "status": "skipped",
             "detail": "still listed after 240s"},
            {"category": "s3:canary/x.zip", "status": "skipped",
             "detail": "artifact of the live version 2"}]}
        db.commit()
    finally:
        db.close()
    # a pending endpoint wait and a retained zip are not cleanup blockers
    assert _footprint(ids["cid"])["canaries"][0]["pending"] is False
    cloud.canary_readbacks = [[{"category": "endpoint:stable", "status": "DELETING"}], []]
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
        {"category": "abtest:local", "status": "deleted", "detail": ""},
        {"category": "online-eval:oe-c", "status": "skipped", "detail": "OperationalError: busy"}]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_canary_incomplete"
    assert exc.value.detail["remaining"][0]["category"] == "online-eval:oe-c"
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

    # the config goes; the second CLEAR is a new job that carries the evidence
    cloud.canary_results = [{"category": "online-eval:oe-c", "status": "deleted", "detail": ""}]
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
    cloud.canary_readbacks = [[{"category": "endpoint:stable", "status": "DELETING"}]]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_still_deleting"
    assert "endpoint:stable (DELETING)" in exc.value.message
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
    cloud.canary_readbacks = [[{"category": "endpoint:treat", "status": "DELETE_FAILED"}]]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_delete_failed"
    assert cloud.events.count(f"canary-readback:{ids['canary']}:1") == 1


def test_a_delete_runtime_conflict_keeps_the_conversation_and_the_agent(cloud):
    ids = _world()
    cloud.delete_error = ConflictException("endpoints exist")
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_agent_conflict"
    assert _get(AssistantConversation, ids["cid"]) is not None
    assert _get(Agent, ids["agent"]).status == "active"
    assert f"agent-residue:{ids['agent']}" not in cloud.events


def test_an_unowned_named_endpoint_blocks_before_any_delete_request(cloud):
    """An endpoint no canary of this conversation created is not this purge's to
    remove — it is surfaced, and DeleteAgentRuntime is never sent."""
    ids = _world()
    cloud.foreign = [{"category": "endpoint:prodPinned", "status": "READY"}]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_agent_dependency"
    assert "endpoint:prodPinned" in exc.value.message
    assert f"agent-delete:{ids['agent']}" not in cloud.events


def test_a_lingering_runtime_keeps_the_agent_in_the_footprint_until_read_back_gone(
        cloud, monkeypatch):
    """DeleteAgentRuntime accepted, then the bounded wait expires: the ledger may not
    flip and the role must stay. The retry does not send a second delete; it re-reads."""
    ids = _world()
    monkeypatch.setattr(purge, "AGENT_GONE_TIMEOUT_S", 0)
    cloud.agent_readbacks = [[{"category": "runtime:kidRuntime", "status": "DELETING"}]]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_still_deleting"
    a = ids["agent"]
    assert f"agent-residue:{a}" not in cloud.events
    assert _get(Agent, a).status == "active"
    assert [x["id"] for x in _footprint(ids["cid"])["agents"]] == [a]

    cloud.agent_readbacks = [[]]
    cloud.events.clear()
    assert _purge(ids["cid"])["deleted"] is True
    assert f"agent-delete:{a}" not in cloud.events  # delete_requested carried forward
    assert cloud.events[0] == f"agent-readback:{a}:0"
    assert cloud.events.index(f"agent-residue:{a}") < cloud.events.index(
        f"artifact-delete:canary/kid-companion/{ids['canary']}-candidate.zip")


def test_a_role_that_cannot_be_deleted_is_a_blocker(cloud):
    ids = _world()
    cloud.residue = [teardown.TeardownStep(teardown.STEP_ROLE, teardown.FAILED,
                                           "DeleteConflict: inline policies attached")]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_role_remains"
    assert "DeleteConflict" in exc.value.message
    assert _get(AssistantConversation, ids["cid"]) is not None
    assert _get(Agent, ids["agent"]).status == "active"  # retryable: evidence kept


def test_a_failed_registry_record_step_stops_the_clear_before_the_ledger_flips(cloud):
    ids = _world()
    cloud.residue = [
        teardown.TeardownStep(teardown.STEP_ROLE, teardown.DELETED, "launchpad-agent-kid"),
        teardown.TeardownStep(teardown.STEP_REGISTRY, teardown.FAILED,
                              "AccessDeniedException: no registry permission")]
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"])
    assert exc.value.code == "assistant.purge_step_failed"
    assert "AccessDeniedException" in exc.value.message
    assert _get(Agent, ids["agent"]).status == "active"
    assert _get(AssistantConversation, ids["cid"]) is not None


def test_a_deleted_registry_record_and_a_foreign_one_are_reported(cloud):
    ids = _world()
    cloud.residue = [teardown.TeardownStep(teardown.STEP_REGISTRY, teardown.DELETED,
                                           "rec-kid · was APPROVED")]
    assert _purge(ids["cid"])["agents"][0]["registry_record"] == "deleted"
    ids = _world()
    cloud.residue = [teardown.TeardownStep(
        teardown.STEP_REGISTRY, teardown.SKIPPED,
        "rec-other is not this agent's A2A record (MCP 'someone-else')")]
    assert _purge(ids["cid"])["agents"][0]["registry_record"] == "kept (not this agent's)"


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
# creator-or-admin on every dependency (users-and-ownership.md §8)
# ---------------------------------------------------------------------------


def _member(user_id: str) -> Identity:
    return Identity(username=f"member-{user_id}", role="member", user_id=user_id)


def test_a_canary_another_member_started_refuses_the_whole_clear_before_any_effect(cloud):
    ids = _world(canary_created_by="u-other", agent_created_by="u-me")
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"], identity=_member("u-me"))
    assert exc.value.status_code == 403 and exc.value.code == "canary.forbidden"
    assert exc.value.detail["canary_id"] == ids["canary"]
    assert cloud.events == []
    assert _get(AssistantConversation, ids["cid"]).purge_job_id is None
    assert _get(RuntimeCanary, ids["canary"]).status == "running"


def test_an_agent_another_member_created_refuses_the_clear(cloud):
    ids = _world(canary_created_by="u-me", agent_created_by="u-other")
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"], identity=_member("u-me"))
    assert exc.value.status_code == 403 and exc.value.code == "agent.forbidden"
    assert cloud.events == []


def test_the_creator_and_an_administrator_may_clear(cloud):
    ids = _world(canary_created_by="u-me", agent_created_by="u-me")
    assert _purge(ids["cid"], identity=_member("u-me"))["deleted"] is True
    ids = _world(canary_created_by="u-other", agent_created_by="u-other")
    admin = Identity(username="operator", role=ROLE_ADMIN)
    assert _purge(ids["cid"], identity=admin)["deleted"] is True
    # a NULL creator (config admin, legacy) is an administrator's — not a member's
    ids = _world()
    with pytest.raises(AppError) as exc:
        _purge(ids["cid"], identity=_member("u-me"))
    assert exc.value.code == "canary.forbidden"


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


def _claim(canary_id: str, progress: str) -> None:
    db = SessionLocal()
    try:
        canary = db.get(RuntimeCanary, canary_id)
        canary.running_action = "cleanup"
        canary.progress = progress
        db.commit()
    finally:
        db.close()


def test_the_startup_sweep_leaves_a_live_clears_canary_claim_alone_whatever_it_says(cloud):
    """A ``cleanup`` claim on a canary whose Agent's conversation has a live purge job
    belongs to the resumed job — whether the claim still carries the purge marker, the
    job already advanced it to a plain progress line, or ``act_cleanup`` rewrote it
    (N15-obs10: the check keys on the Agents, never on the text). Once the job is
    terminal it is a dead flag like any other."""
    ids = _world()
    db = SessionLocal()
    try:
        job, _ = purge.request_purge(db, db.get(AssistantConversation, ids["cid"]),
                                     can=lambda _p: True)
        job_id = job.id
    finally:
        db.close()
    _claim(ids["other_canary"], "deleting endpoints")  # a member's own cleanup, interrupted
    for progress in (f"{purge.MARKER_PREFIX}{ids['cid'][:8]}: starting cleanup",
                     "waiting up to 240s for stable, treat to disappear…", None):
        _claim(ids["canary"], progress)
        assert canary_service.clear_stale_running_actions() == [ids["other_canary"]]
        assert _get(RuntimeCanary, ids["canary"]).running_action == "cleanup"
        assert _get(RuntimeCanary, ids["canary"]).error is None
        _claim(ids["other_canary"], "deleting endpoints")
    db = SessionLocal()
    try:
        db.get(Job, job_id).status = "failed"
        db.commit()
    finally:
        db.close()
    assert sorted(canary_service.clear_stale_running_actions()) == sorted(
        [ids["canary"], ids["other_canary"]])
    assert _get(RuntimeCanary, ids["canary"]).running_action is None
    assert _get(RuntimeCanary, ids["canary"]).error.startswith("cleanup: interrupted")


def test_the_startup_sweep_never_overwrites_a_claim_released_under_it(cloud, monkeypatch):
    """The race the integration gate hit (N15-obs10): the sweep read the canary while the
    resumed job still held the claim, the job then released it and deleted the
    conversation, and the sweep's live check found nothing. The clear is a compare-and-set
    on the value the sweep read, so the released row keeps ``error = None``."""
    ids = _world()
    _claim(ids["canary"], f"{purge.MARKER_PREFIX}{ids['cid'][:8]}: tearing down")
    real = purge.canary_under_live_clear

    def check(db, row):
        if row.id == ids["canary"]:  # the job finishes between the sweep's read and write
            other = SessionLocal()
            try:
                released = other.get(RuntimeCanary, ids["canary"])
                released.running_action = None
                released.progress = None
                released.error = None
                other.commit()
            finally:
                other.close()
            return False
        return real(db, row)

    monkeypatch.setattr(purge, "canary_under_live_clear", check)
    assert canary_service.clear_stale_running_actions() == []
    canary = _get(RuntimeCanary, ids["canary"])
    assert canary.running_action is None and canary.error is None


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
    writes, the Agent refuses canary / delete (and the redeploy guard fires), a repeated
    DELETE returns the same job — and the job, once run, removes everything."""
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
    gone = client.delete(f"/api/agents/{ids['agent']}")
    assert gone.status_code == 409 and gone.json()["code"] == "assistant.agent_clearing"
    with pytest.raises(AppError) as exc:  # the redeploy route's guard, before any deploy
        purge.refuse_while_clearing(ids["agent"], "redeploy")
    assert exc.value.code == "assistant.agent_clearing" and exc.value.status_code == 409
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


def wait_job(job_id: str, timeout_s: float = 10.0) -> Job:
    """Poll a job run by a background worker thread until it is terminal."""
    deadline = time.monotonic() + timeout_s
    while True:
        job = _get(Job, job_id)
        if job.status not in purge.LIVE_JOB_STATUSES or time.monotonic() >= deadline:
            return job
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# the real canary readback (fake control client, ledger online configs)
# ---------------------------------------------------------------------------


class ResourceNotFoundException(Exception):
    pass


class _Control:
    def __init__(self, endpoints: dict[str, str] | None):
        self.endpoints = endpoints  # name → status; None = the runtime itself is gone

    def get_agent_runtime_endpoint(self, agentRuntimeId, endpointName):
        if self.endpoints is None or endpointName not in self.endpoints:
            raise ResourceNotFoundException(endpointName)
        return {"name": endpointName, "status": self.endpoints[endpointName]}


def _online_config(db, agent: Agent, name: str) -> str:
    row = OnlineEvalConfig(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                           agent_name=agent.name, config_id=f"cfg-{name}",
                           config_arn=f"arn:aws-cn:launchpad::1:online-evaluation-config/{name}",
                           name=name, service_name="svc", log_group="/aws/x")
    db.add(row)
    db.flush()
    return row.config_id


def test_canary_readback_reports_every_owned_resource_still_present(monkeypatch):
    create_app()
    db = SessionLocal()
    try:
        agent = _agent(db, "rb", "rbRuntime")
        row = _canary(db, agent)
        own = _online_config(db, agent, f"can_{row.id[:8]}_oec")
        _online_config(db, agent, "can_ffffffff_oec")  # another canary's arm
        db.commit()
        db.expunge(row)
    finally:
        db.close()
    stable, treat = f"stable{row.id[:6]}", f"treat{row.id[:6]}"
    monkeypatch.setattr(canary_service, "control_client",
                        lambda _ws: _Control({stable: "DELETING", "DEFAULT": "READY"}))
    remaining = canary_service.remaining_resources(row, ws_ctx())
    assert remaining == [{"category": f"online-eval:{own}", "status": "ACTIVE"},
                         {"category": f"endpoint:{stable}", "status": "DELETING"}]
    assert treat not in str(remaining)  # already gone

    # all gone — including the runtime itself (its endpoints went with it)
    db = SessionLocal()
    try:
        db.query(OnlineEvalConfig).filter(OnlineEvalConfig.config_id == own).delete()
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(canary_service, "control_client", lambda _ws: _Control(None))
    assert canary_service.remaining_resources(row, ws_ctx()) == []


def test_cleanup_blockers_ignore_retained_zips_and_the_endpoint_wait_marker():
    results = [
        {"category": "online-eval:x", "status": "deleted", "detail": ""},
        {"category": "s3:canary/a.zip", "status": "skipped", "detail": "live version"},
        {"category": "endpoints-gone", "status": "skipped", "detail": "still listed"},
        {"category": "endpoint:treat", "status": "skipped", "detail": "ThrottlingException"},
    ]
    assert canary_service.cleanup_blockers(results) == [results[3]]


# ---------------------------------------------------------------------------
# N17-obs479: the terminal status is the job's LAST write (forced interleaving)
# ---------------------------------------------------------------------------


def _paused_after_terminal_commit(job_id: str):
    """Run the purge job in a worker thread that PAUSES right after the commit that made the
    job terminal (the moment a poller — the graph walk's ``wait_job``, the console's
    ``runConversationClear`` — returns), and hand the main thread that window."""
    import threading

    from sqlalchemy import event, text
    from sqlalchemy.orm import Session

    from app.core.db import engine

    terminal_seen, release = threading.Event(), threading.Event()
    worker: dict[str, int] = {}

    def after_commit(_session) -> None:
        if threading.get_ident() != worker.get("id") or terminal_seen.is_set():
            return
        with engine.connect() as conn:
            status = conn.execute(text("SELECT status FROM jobs WHERE id = :i"),
                                  {"i": job_id}).scalar()
        if status in ("succeeded", "failed"):
            terminal_seen.set()
            release.wait(10)

    def run() -> None:
        worker["id"] = threading.get_ident()
        purge.execute_purge_job(job_id)

    event.listen(Session, "after_commit", after_commit)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert terminal_seen.wait(10), "the job never reached a terminal status"
        yield
    finally:
        release.set()
        thread.join(10)
        event.remove(Session, "after_commit", after_commit)


def _clear_job(monkeypatch) -> str:
    ids = _world()
    started: list[str] = []
    monkeypatch.setattr(purge, "start_purge_async", lambda job_id: started.append(job_id))
    res = TestClient(create_app()).delete(f"{BASE}/{ids['cid']}")
    assert res.status_code == 202, res.text
    return res.json()["job_id"]


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_nothing_writes_the_job_after_it_is_terminal(cloud, monkeypatch, outcome):
    """N17-obs479 (graph walk seed 5: "refused edge 'clear_again' changed ledger table(s):
    ['jobs']"): the purge committed `succeeded` / `failed` and only THEN appended its final log
    line in a second commit — a poller that stopped at the terminal status (AT_10: "poll
    GET /api/jobs/{job_id}"; the console follows it "until terminal") missed that line, and
    the late write landed inside the next request. Forced here: the worker pauses right after
    the terminal commit; the ledger seen in that window must be the final one."""
    from tests.graph.executor import ledger_digest

    job_id = _clear_job(monkeypatch)
    if outcome == "failed":
        cloud.delete_error = ConflictException("runtime busy")
    gen = _paused_after_terminal_commit(job_id)
    next(gen)
    seen = ledger_digest()
    job = _get(Job, job_id)
    assert job.status == outcome, (job.status, job.error)
    last = job.log.strip().splitlines()[-1]
    with pytest.raises(StopIteration):
        next(gen)
    assert ledger_digest() == seen, "a write landed after the job was terminal"
    assert _get(Job, job_id).log.strip().splitlines()[-1] == last
    assert ("conversation rows removed" in last) if outcome == "succeeded" else \
        ('"level": "error"' in last)


def test_no_job_runner_appends_its_log_after_the_terminal_commit():
    """N17-obs479 guard: the pattern `job.status = <terminal> … db.commit()` followed by an
    `_append_log` (a second commit on a terminal job) is gone from every runner — the final
    line goes through `pipeline._log_line` inside the terminal commit."""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    late = re.compile(r'job\.status = (?:"(?:succeeded|failed|cancelled)"|CANCELLED)[^\n]*\n'
                      r'(?:(?!\n\s*\n)[\s\S])*?db\.commit\(\)\n\s*(?:if [^\n]*\n\s*)?'
                      r'_append_log\(')
    offenders = [str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py")
                 if late.search(p.read_text(encoding="utf-8"))]
    assert offenders == [], offenders
