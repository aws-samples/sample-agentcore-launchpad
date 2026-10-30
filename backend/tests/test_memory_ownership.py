"""Memory ownership (issue #55) — the account boundary is not the workspace boundary.

A memory is managed by a workspace only when it is the bootstrap memory or a
``ManagedMemory`` row names it (console create / administrator adopt). These pin
both sinks the issue reported:

* a spec-pinned ``memory.memory_id`` must be managed — refused at create /
  re-publish / convert time and again by the deploy job before any stage, so the
  runtime binding never sees a foreign id; the console read-back refuses a legacy
  foreign pin instead of reading it;
* the lifecycle API answers ``404 memory.not_managed`` on every per-id route for a
  foreign memory, before the backend is even consulted.

CN build: the ownership check is ledger-only (R7 — no live GetMemory ACTIVE probe),
and no memory backend is registered, so every route that would reach one answers
503 ``feature.unavailable`` *after* the ledger guard has had its say.
"""

import json

import pytest

import app.routers.agents as agents_router
import app.services.memory_ownership as mo
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.route_policy import ADMIN, PERM_MEMORY_MANAGE, required_role
from app.deployer.pipeline import (
    STAGE_ORDER,
    StageResult,
    create_deployment,
    execute_deploy_job,
    register_method,
)
from app.models.ledger import Agent, Job, ManagedMemory
from app.services import memory_backend
from app.services import observability as obs
from app.services.workspace import WorkspaceContext

from .conftest import set_default_resources, ws_ctx

MEM_ID = "launchpad_memory-ABC123"  # the workspace's bootstrap memory
FOREIGN = "analytics_team_prod-AAAA"  # another team's memory in the same account
OWNED = "team_notes-XYZ789"


@pytest.fixture
def default_memory(client):
    set_default_resources({"memory_id": MEM_ID, "artifacts_bucket": "launchpad-artifacts-test"})


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


def pinned_spec(name: str, memory_id: str) -> dict:
    return {
        "name": name,
        "method": "zip_runtime",
        "system_prompt": "Answer concisely.",
        "memory": {"short_term": True, "memory_id": memory_id},
    }


# --------------------------------------------------------------------------- #
# Ledger semantics
# --------------------------------------------------------------------------- #


def test_bootstrap_memory_and_managed_rows_are_managed(default_memory):
    ws = ws_ctx({"memory_id": MEM_ID})
    db = SessionLocal()
    try:
        assert mo.is_managed(db, ws, MEM_ID) is True
        assert mo.is_managed(db, ws, OWNED) is False
        assert mo.is_managed(db, ws, None) is False
        manage(OWNED)
        assert mo.is_managed(db, ws, OWNED) is True
        assert mo.managed_ids(db, ws) == {MEM_ID, OWNED}
    finally:
        db.close()


def test_another_workspaces_registration_does_not_make_a_memory_managed(default_memory):
    manage(FOREIGN, workspace_id="other-workspace")
    db = SessionLocal()
    try:
        assert mo.is_managed(db, ws_ctx({"memory_id": MEM_ID}), FOREIGN) is False
    finally:
        db.close()


def test_require_pinnable_is_ledger_only(default_memory):
    """R7: no live ACTIVE probe — a managed id passes without any client."""
    ws = ws_ctx({"memory_id": MEM_ID})
    db = SessionLocal()
    try:
        mo.require_pinnable(db, ws, MEM_ID)
        with pytest.raises(AppError) as exc:
            mo.require_pinnable(db, ws, FOREIGN)
        assert exc.value.code == "agent.memory_not_managed"
        assert exc.value.status_code == 422
        manage(OWNED)
        mo.require_pinnable(db, ws, OWNED)
    finally:
        db.close()


def test_forget_drops_the_row(default_memory):
    manage(OWNED)
    ws = ws_ctx({"memory_id": MEM_ID})
    db = SessionLocal()
    try:
        mo.forget(db, ws, OWNED)
        assert mo.is_managed(db, ws, OWNED) is False
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Lifecycle API — the ledger guard runs before the (absent) backend
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", {"description": "mine now"}), ("delete", None)],
)
def test_per_id_routes_refuse_an_unmanaged_memory(client, default_memory, method, body):
    res = client.request(method.upper(), f"/api/memory/resources/{FOREIGN}", json=body)
    assert res.status_code == 404
    assert res.json()["code"] == "memory.not_managed"


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("put", {"description": "mine now"}), ("delete", None)],
)
def test_per_id_routes_on_a_managed_memory_need_a_backend(client, default_memory, method, body):
    memory_backend.set_backend(None)
    res = client.request(method.upper(), f"/api/memory/resources/{MEM_ID}", json=body)
    assert res.status_code == 503
    assert res.json()["code"] == "feature.unavailable"
    assert res.json()["detail"] == {"feature": "memory"}


def test_list_and_create_need_a_backend(client, default_memory):
    memory_backend.set_backend(None)
    assert client.get("/api/memory/resources").status_code == 503
    res = client.post("/api/memory/resources", json={"name": "fresh_notes", "strategies": []})
    assert res.status_code == 503
    db = SessionLocal()
    assert db.query(ManagedMemory).count() == 0  # nothing registered without a store
    db.close()


def test_route_policy_gates_memory_writes():
    base = "/api/memory/resources"
    assert required_role("POST", base) == PERM_MEMORY_MANAGE
    assert required_role("PUT", f"{base}/{{memory_id}}") == PERM_MEMORY_MANAGE
    assert required_role("DELETE", f"{base}/{{memory_id}}") == PERM_MEMORY_MANAGE
    assert required_role("POST", f"{base}/{{memory_id}}/adopt") == ADMIN


# --------------------------------------------------------------------------- #
# Spec-pinned memory: create / re-publish
# --------------------------------------------------------------------------- #


def test_create_refuses_a_foreign_memory_pin_before_any_row(client, default_memory, launched):
    res = client.post("/api/agents", json=pinned_spec("foreign-pin", FOREIGN))
    assert res.status_code == 422
    assert res.json()["code"] == "agent.memory_not_managed"
    assert launched == []
    db = SessionLocal()
    assert db.query(Agent).filter_by(name="foreign-pin").count() == 0
    db.close()


def test_create_accepts_a_managed_memory_and_the_default(client, default_memory, launched):
    manage(OWNED)
    assert client.post("/api/agents", json=pinned_spec("own-pin", OWNED)).status_code == 202
    res = client.post("/api/agents", json=pinned_spec("default-pin", MEM_ID))
    assert res.status_code == 202
    assert len(launched) == 2


def test_redeploy_refuses_a_foreign_pin_and_keeps_the_stored_spec(
    client, default_memory, launched
):
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


def test_deploy_job_fails_a_foreign_pin_before_any_stage(client, default_memory):
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


def test_chat_memory_readback_refuses_a_legacy_foreign_pin(client, default_memory):
    read: list = []

    class _Backend:
        def session_memory_summary(self, *a, **k):
            read.append(k)
            return {}

    memory_backend.set_backend(_Backend())
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


def test_chat_memory_readback_without_a_backend_is_unavailable(client, default_memory):
    memory_backend.set_backend(None)
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="plain", method="harness", status="active",
        spec={"memory": {"short_term": True}},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()

    res = client.get(f"/api/chat/{agent_id}/memory", params={"session_id": "s" * 40})
    assert res.status_code == 503
    assert res.json()["code"] == "feature.unavailable"


def test_transcript_never_reads_a_foreign_pinned_memory(client, default_memory):
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

    class _Backend:
        def list_events(self, *a, **k):
            probed.append(a)
            return {"items": []}

        def list_records(self, *a, **k):
            probed.append(a)
            return {"items": []}

    memory_backend.set_backend(_Backend())
    ws = WorkspaceContext(account_id="1", region="us-west-2", resources={"memory_id": MEM_ID})
    result = obs.session_transcript(db, sid, ws)
    db.close()
    assert probed == []
    # the chat ledger is empty too, so the transcript reports memory unavailable
    assert result["available"] is False
    assert "not manage" in json.dumps(result)
