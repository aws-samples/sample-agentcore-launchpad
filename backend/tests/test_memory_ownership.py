"""Memory ownership (issue #55) — the account boundary is not the workspace boundary.

A memory is managed by a workspace only when it is the bootstrap memory or a
``ManagedMemory`` row names it (console create / administrator adopt). These pin
both sinks the issue reported:

* a spec-pinned ``memory.memory_id`` must be managed AND ``ACTIVE`` — refused at
  create / re-publish / convert time and again by the deploy job before any stage,
  so the execution-role grant and runtime binding never see a foreign id; the
  console read-back refuses a legacy foreign pin instead of reading it;
* the lifecycle API lists foreign memories as detected-not-managed and answers
  ``404 memory.not_managed`` on every per-id route for them, before any AWS call.

The issue's three PoC scenarios are inverted here (``test_poc_*``).
"""

import json

import pytest

import app.routers.agents as agents_router
import app.services.memory_admin as ma
import app.services.memory_ownership as mo
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.route_policy import ADMIN, PERM_MEMORY_MANAGE, required_role
from app.deployer.pipeline import (
    STAGE_ORDER,
    StageResult,
    create_deployment,
    execute_deploy_job,
    register_method,
)
from app.models.ledger import Agent, Job, ManagedMemory
from app.services import observability as obs
from app.services.workspace import WorkspaceContext

from .conftest import set_default_resources
from .test_memory_resources import MEM_ID, StubControl

FOREIGN = "analytics_team_prod-AAAA"  # another team's memory in the same account
OWNED = "team_notes-XYZ789"


class StatusControl(StubControl):
    """GetMemory answers per id, so ownership and ACTIVE are told apart."""

    def __init__(self, statuses=None, **kw):
        super().__init__(**kw)
        self.statuses = statuses or {}

    def get_memory(self, **kw):
        mem_id = kw["memoryId"]
        return self._reply(
            "get_memory",
            kw,
            {"memory": {"id": mem_id, "arn": f"arn:mem:{mem_id}",
                        "status": self.statuses.get(mem_id, "ACTIVE")}},
        )


@pytest.fixture
def control(client, monkeypatch):
    set_default_resources({"memory_id": MEM_ID})
    stub = StatusControl(
        memories=[
            {"id": MEM_ID, "arn": "arn:mem:1", "status": "ACTIVE"},
            {"id": OWNED, "arn": "arn:mem:2", "status": "ACTIVE"},
            {"id": FOREIGN, "arn": "arn:mem:foreign", "status": "ACTIVE"},
        ]
    )
    monkeypatch.setattr(ma, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(mo, "control_client", lambda _ws=None: stub)
    return stub


@pytest.fixture
def launched(monkeypatch):
    jobs: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: jobs.append(jid))
    return jobs


def manage(memory_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
    db = SessionLocal()
    db.add(ManagedMemory(workspace_id=workspace_id, memory_id=memory_id, origin="created"))
    db.commit()
    db.close()


def managed_rows() -> set[str]:
    db = SessionLocal()
    try:
        return {r.memory_id for r in db.query(ManagedMemory).all()}
    finally:
        db.close()


def pinned_spec(name: str, memory_id: str) -> dict:
    return {
        "name": name,
        "method": "harness",
        "system_prompt": "Answer concisely.",
        "memory": {"short_term": True, "memory_id": memory_id},
    }


def aws_ops(stub: StubControl) -> list[str]:
    return [op for op, _ in stub.calls]


# --------------------------------------------------------------------------- #
# The issue's PoC, inverted
# --------------------------------------------------------------------------- #


def test_poc_foreign_memory_is_listed_as_not_managed(client, control):
    items = {m["id"]: m for m in client.get("/api/memory/resources").json()["items"]}
    assert items[FOREIGN]["managed"] is False
    assert items[MEM_ID]["managed"] is True  # the bootstrap memory is always managed
    assert items[OWNED]["managed"] is False  # until created here or adopted
    manage(OWNED)
    items = {m["id"]: m for m in client.get("/api/memory/resources").json()["items"]}
    assert items[OWNED]["managed"] is True


def test_poc_foreign_memory_delete_is_refused_before_aws(client, control):
    res = client.delete(f"/api/memory/resources/{FOREIGN}")
    assert res.status_code == 404
    assert res.json()["code"] == "memory.not_managed"
    assert aws_ops(control) == []


def test_poc_deleting_the_pinning_agent_does_not_unlock_a_foreign_memory(client, control):
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="pinner", method="zip_runtime",
        status="deleted", spec={"memory": {"short_term": True, "memory_id": FOREIGN}},
    )
    db.add(agent)
    db.commit()
    db.close()
    res = client.delete(f"/api/memory/resources/{FOREIGN}")
    assert res.status_code == 404
    assert "delete_memory" not in aws_ops(control)


# --------------------------------------------------------------------------- #
# Lifecycle API
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", {"description": "mine now"}), ("delete", None)],
)
def test_per_id_routes_refuse_an_unmanaged_memory(client, control, method, body):
    res = client.request(method.upper(), f"/api/memory/resources/{FOREIGN}", json=body)
    assert res.status_code == 404
    assert res.json()["code"] == "memory.not_managed"
    assert aws_ops(control) == []  # not even a GetMemory: nothing is disclosed


def test_another_workspaces_registration_does_not_make_a_memory_managed(client, control):
    manage(FOREIGN, workspace_id="other-workspace")
    assert client.get(f"/api/memory/resources/{FOREIGN}").status_code == 404


def test_create_registers_the_memory_and_delete_forgets_it(client, control):
    res = client.post("/api/memory/resources", json={"name": "fresh_notes", "strategies": []})
    assert res.status_code == 201
    created = res.json()
    assert created["managed"] is True
    assert created["id"] in managed_rows()

    assert client.get(f"/api/memory/resources/{created['id']}").status_code == 200
    res = client.delete(f"/api/memory/resources/{created['id']}")
    assert res.status_code == 200
    assert created["id"] not in managed_rows()


def test_adopt_registers_an_existing_memory(client, control):
    res = client.post(f"/api/memory/resources/{OWNED}/adopt")
    assert res.status_code == 200
    assert res.json()["managed"] is True
    assert ("get_memory", {"memoryId": OWNED}) in control.calls  # must exist first
    db = SessionLocal()
    row = db.query(ManagedMemory).filter_by(memory_id=OWNED).one()
    assert row.origin == "adopted" and row.workspace_id == DEFAULT_WORKSPACE_ID
    db.close()
    # idempotent
    assert client.post(f"/api/memory/resources/{OWNED}/adopt").status_code == 200
    assert managed_rows() == {OWNED}


def test_adopt_of_an_unknown_id_registers_nothing(client, control, monkeypatch):
    from botocore.exceptions import ClientError

    def missing(**kw):
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no such memory"}},
            "GetMemory",
        )

    monkeypatch.setattr(control, "get_memory", missing)
    res = client.post("/api/memory/resources/ghost-0000/adopt")
    assert res.status_code == 404
    assert res.json()["code"] == "aws.not_found"
    assert managed_rows() == set()


def test_route_policy_gates_memory_writes():
    base = "/api/memory/resources"
    assert required_role("POST", base) == PERM_MEMORY_MANAGE
    assert required_role("PUT", f"{base}/{{memory_id}}") == PERM_MEMORY_MANAGE
    assert required_role("DELETE", f"{base}/{{memory_id}}") == PERM_MEMORY_MANAGE
    assert required_role("POST", f"{base}/{{memory_id}}/adopt") == ADMIN


# --------------------------------------------------------------------------- #
# Spec-pinned memory: create / re-publish
# --------------------------------------------------------------------------- #


def test_create_refuses_a_foreign_memory_pin_before_any_row(client, control, launched):
    res = client.post("/api/agents", json=pinned_spec("foreign-pin", FOREIGN))
    assert res.status_code == 422
    assert res.json()["code"] == "agent.memory_not_managed"
    assert launched == []
    assert aws_ops(control) == []  # ownership is a ledger check, before GetMemory
    db = SessionLocal()
    assert db.query(Agent).filter_by(name="foreign-pin").count() == 0
    db.close()


def test_create_refuses_a_managed_memory_that_is_not_active(client, control, launched):
    manage(OWNED)
    control.statuses[OWNED] = "CREATING"
    res = client.post("/api/agents", json=pinned_spec("early-pin", OWNED))
    assert res.status_code == 409
    assert res.json()["code"] == "agent.memory_not_active"
    assert res.json()["detail"]["status"] == "CREATING"
    assert launched == []


def test_create_accepts_a_managed_active_memory_and_the_default(client, control, launched):
    manage(OWNED)
    assert client.post("/api/agents", json=pinned_spec("own-pin", OWNED)).status_code == 202
    res = client.post("/api/agents", json=pinned_spec("default-pin", MEM_ID))
    assert res.status_code == 202
    assert len(launched) == 2


def test_redeploy_refuses_a_foreign_pin_and_keeps_the_stored_spec(client, control, launched):
    manage(OWNED)
    created = client.post("/api/agents", json=pinned_spec("repin", OWNED)).json()
    agent_id = created["agent"]["id"]
    db = SessionLocal()
    db.get(Agent, agent_id).status = "active"
    db.commit()
    db.close()

    res = client.post(f"/api/agents/{agent_id}/redeploy", json=pinned_spec("repin", FOREIGN))
    assert res.status_code == 422
    assert res.json()["code"] == "agent.memory_not_managed"
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    assert agent.spec["memory"]["memory_id"] == OWNED
    assert agent.status == "active"
    db.close()


# --------------------------------------------------------------------------- #
# Deploy job gate (covers every path into a job, resumes included)
# --------------------------------------------------------------------------- #


def test_deploy_job_fails_a_foreign_pin_before_any_stage(client, control):
    calls: list[str] = []
    register_method(
        "fake_memory_gate",
        {s: (lambda ctx, agent, _s=s: (calls.append(_s), StageResult())[1]) for s in STAGE_ORDER},
    )
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="legacy-pin", method="fake_memory_gate",
        status="deploying", spec={"name": "legacy-pin", "memory": {"memory_id": FOREIGN}},
    )
    db.add(agent)
    db.commit()
    _, job = create_deployment(db, agent)
    job_id, agent_id = job.id, agent.id
    db.close()

    execute_deploy_job(job_id)

    assert calls == []
    db = SessionLocal()
    assert db.get(Job, job_id).status == "failed"
    assert "not managed" in db.get(Agent, agent_id).error
    db.close()


# --------------------------------------------------------------------------- #
# Console read-back
# --------------------------------------------------------------------------- #


def test_chat_memory_readback_refuses_a_legacy_foreign_pin(client, control, monkeypatch):
    from app.services import memory as memory_service

    read: list = []
    monkeypatch.setattr(
        memory_service, "session_memory_summary", lambda *a, **k: read.append(k) or {}
    )
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="legacy", method="harness", status="active",
        spec={"memory": {"short_term": True, "memory_id": FOREIGN}},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()

    res = client.get(f"/api/chat/{agent_id}/memory", params={"session_id": "s" * 40})
    assert res.status_code == 409
    assert res.json()["code"] == "agent.memory_not_managed"
    assert read == []


def test_transcript_never_reads_a_foreign_pinned_memory(client, control, monkeypatch):
    from app.models.ledger import ChatSession

    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="legacy", method="harness", status="active",
        spec={"memory": {"short_term": True, "memory_id": FOREIGN}},
    )
    db.add(agent)
    db.commit()
    sid = "t" * 40
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                       session_id=sid, actor_id="river"))
    db.commit()

    probed: list = []
    monkeypatch.setattr(obs.memory, "list_events", lambda *a, **k: probed.append(k) or [])
    monkeypatch.setattr(obs.memory, "list_records", lambda *a, **k: probed.append(k) or [])
    ws = WorkspaceContext(account_id="1", region="us-west-2", resources={"memory_id": MEM_ID})
    result = obs.session_transcript(db, sid, ws)
    db.close()
    assert probed == []
    # the chat ledger is empty too, so the transcript reports memory unavailable
    assert result["available"] is False
    assert "not manage" in json.dumps(result)
