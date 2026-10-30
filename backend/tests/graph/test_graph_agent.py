# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E03 — the Agent lifecycle graph walked through the real routes (round 16, r16-o).

Graph: ``tests/graph/graphs/agent.yaml``. Hermetic: the deploy job runs inline with the
``zip_runtime`` stages replaced by recording fakes (a hook may raise, or mark the row
deleted, inside a named stage — the shapes of ``tests/test_deploy_cancel.py``); the Runtime
delete and the residue sweep are recorded no-ops; the chat producer is a scripted delta
stream. Three accounts: ada (the creator), bob (another member), the administrator.
"""

from __future__ import annotations

import os
import uuid

import pytest

import app.routers.agents as agents_router
import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import SessionLocal
from app.deployer import pipeline
from app.deployer.pipeline import STAGE_ORDER, StageResult, execute_deploy_job
from app.models.ledger import Agent, Deployment, Job
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.walks import run_walk, walk_seeds
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
RESERVED_NAME = "launchpad-eval-worker"  # the system eval worker's base name (agents.md §3)


class StopPlane:
    def stop_runtime_session(self, **params):
        return {"runtimeSessionId": params["runtimeSessionId"], "statusCode": 200}


def _write(agent_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def _mark_deleted(agent_id: str) -> None:
    """What the product's DELETE route leaves on the row (``routers/agents.mark_agent_deleted``:
    status `deleted`, workload identity forgotten, name claim released) — the state agents.md
    §2 says the pipeline must find and respect."""
    db = SessionLocal()
    try:
        agents_router.mark_agent_deleted(db, db.get(Agent, agent_id))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def seams(monkeypatch):
    """Every AWS seam the agent lifecycle reaches, recorded / scripted. ``hooks[stage]``
    runs inside that fake stage (raise → the job fails there)."""
    launched: list[str] = []
    hooks: dict[str, object] = {}
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    monkeypatch.setattr(pipeline, "start_deploy_async", lambda jid: launched.append(jid))

    def stage(ctx, agent, name):
        hook = hooks.get(name)
        if hook is not None:
            hook(ctx, agent)
        if name == "deploy":  # the real deploy stage leaves the Runtime identity on the row
            _write(agent.id, resource_id=f"rt{agent.id[:10]}",
                   arn=f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/"
                       f"rt{agent.id[:10]}")
        return StageResult(detail=f"{name} done (fake)")

    monkeypatch.setitem(
        pipeline._METHODS, "zip_runtime",
        {s: (lambda ctx, agent, _s=s: stage(ctx, agent, _s)) for s in STAGE_ORDER},
    )
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, ws: None)
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(
        chat_service, "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: iter(
            [{"event": "delta", "data": {"text": "ok"}}]),
    )
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopPlane())
    return hooks


class AgentWorld(World):
    graph = "agent"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}
    #: N17-obs16 fixed in cn/r17-d: the redeploy a running canary refuses (agents.md §1.1) —
    #: the cross walk drives the refusal, the neighbourhood keeps the edge's surroundings
    FIXED: dict[str, list[str]] = {
        "N17-obs16": ["redeploy"],
        # N17-obs18 fixed in cn/r17-f: the delete an ACTIVE evaluation run refuses (§1.3)
        "N17-obs18": ["delete_active", "delete_failed"],
    }

    def __init__(self, clients, hooks) -> None:
        super().__init__(clients)
        self.hooks = hooks
        self.name = f"g-agent-{uuid.uuid4().hex[:8]}"
        self.agent_id: str | None = None
        self.job_id: str | None = None
        self.prompt = "hi"

    # -- helpers -------------------------------------------------------------------
    def spec(self, name: str | None = None) -> dict:
        return {"name": name or self.name, "method": "zip_runtime", "system_prompt": self.prompt}

    def row(self) -> Agent:
        db = SessionLocal()
        try:
            row = db.get(Agent, self.agent_id)
            db.expunge(row)
            return row
        finally:
            db.close()

    def job(self) -> Job:
        db = SessionLocal()
        try:
            row = db.get(Job, self.job_id)
            db.expunge(row)
            return row
        finally:
            db.close()

    def deployment(self) -> Deployment:
        db = SessionLocal()
        try:
            row = db.get(Deployment, (self.job().payload or {})["deployment_id"])
            db.expunge(row)
            return row
        finally:
            db.close()

    def run_job(self, *, fail_at: str | None = None, mark_deleted_at: str | None = None) -> None:
        self.hooks.clear()
        if fail_at:
            def boom(ctx, agent, _n=fail_at):
                raise RuntimeError(f"{_n} exploded (fake)")
            self.hooks[fail_at] = boom
        if mark_deleted_at:
            self.hooks[mark_deleted_at] = lambda ctx, agent: _mark_deleted(agent.id)
        try:
            execute_deploy_job(self.job_id)
        finally:
            self.hooks.clear()

    def accept_create(self, res, edge) -> None:
        body = self.expect(res, edge)
        self.agent_id = body["agent"]["id"]
        self.job_id = body["job_id"]
        self.ids |= {self.agent_id, self.job_id, body["deployment_id"]}
        assert body["agent"]["status"] == "deploying"
        assert self.job().status == "queued"

    def accept_redeploy(self, res, edge) -> None:
        body = self.expect(res, edge)
        assert body["agent"]["id"] == self.agent_id
        self.job_id = body["job_id"]
        self.ids |= {self.job_id, body["deployment_id"]}
        assert body["agent"]["status"] == "deploying"
        assert (self.job().payload or {}).get("mode") == "update"

    def chat(self, client):
        return client.post(f"/api/chat/{self.agent_id}", json={"prompt": "hello"})

    # -- edges -----------------------------------------------------------------------
    def edge_create(self, edge) -> None:
        self.accept_create(self.ada.post("/api/agents", json=self.spec()), edge)

    def edge_create_name_reserved(self, edge) -> None:
        res = self.ada.post("/api/agents", json=self.spec(RESERVED_NAME))
        body = self.expect(res, edge)
        assert body["detail"]["name"] == RESERVED_NAME and body["detail"]["reserved_for"]

    def edge_deploy_succeeds(self, edge) -> None:
        self.run_job()
        row, job = self.row(), self.job()
        assert (row.status, job.status) == ("active", "succeeded"), (row.status, job.status)
        assert row.arn and row.resource_id and row.error is None
        assert self.deployment().status == "succeeded"

    def edge_deploy_fails(self, edge) -> None:
        self.run_job(fail_at="package")
        row, job = self.row(), self.job()
        assert (row.status, job.status) == ("failed", "failed"), (row.status, job.status)
        assert "package exploded" in (row.error or "")
        assert self.deployment().status == "failed"

    def _delete_in_progress(self, edge) -> None:
        res = self.ada.delete(f"/api/agents/{self.agent_id}")
        body = self.expect(res, edge)
        assert body["detail"]["job_id"] == self.job_id
        assert body["detail"]["job_status"] == "queued" and body["detail"]["stage"] == "generate"
        assert self.row().status == "deploying"

    edge_delete_while_deploying = _delete_in_progress
    edge_delete_while_redeploying = _delete_in_progress
    edge_delete_while_cold_redeploying = _delete_in_progress

    def _redeploy_in_progress(self, edge) -> None:
        res = self.ada.post(f"/api/agents/{self.agent_id}/redeploy", json=self.spec())
        self.expect(res, edge)
        assert self.row().status == "deploying"

    edge_redeploy_while_deploying = _redeploy_in_progress

    def _chat_not_active(self, edge) -> None:
        self.expect(self.chat(self.ada), edge)

    edge_chat_while_deploying = _chat_not_active
    edge_chat_while_redeploying = _chat_not_active
    edge_chat_while_cold_redeploying = _chat_not_active

    def edge_create_duplicate_while_deploying(self, edge) -> None:
        res = self.ada.post("/api/agents", json=self.spec())
        body = self.expect(res, edge)
        assert body["detail"].get("agent_id") == self.agent_id

    def edge_row_marked_deleted_mid_job(self, edge) -> None:
        self.run_job(mark_deleted_at="provision")
        row, job, dep = self.row(), self.job(), self.deployment()
        assert row.status == "deleted", row.status
        assert job.status == pipeline.CANCELLED and job.error == pipeline.CANCEL_REASON
        assert dep.status == pipeline.CANCELLED and dep.ended_at is not None
        assert job.payload["cancelled"]["reason"] == "agent_deleted"

    def edge_redeploy(self, edge) -> None:
        self.prompt = f"{self.prompt} v2"
        self.accept_redeploy(
            self.ada.post(f"/api/agents/{self.agent_id}/redeploy", json=self.spec()), edge)

    edge_redeploy_from_failed = edge_redeploy

    def edge_redeploy_succeeds(self, edge) -> None:
        self.run_job()
        row, job = self.row(), self.job()
        assert (row.status, job.status) == ("active", "succeeded"), (row.status, job.status)
        assert row.spec["system_prompt"] == self.prompt

    edge_cold_redeploy_succeeds = edge_redeploy_succeeds

    def edge_cold_redeploy_fails(self, edge) -> None:
        self.run_job(fail_at="package")
        row, job = self.row(), self.job()
        assert (row.status, job.status) == ("failed", "failed"), (row.status, job.status)

    def edge_redeploy_fails_keeps_live(self, edge) -> None:
        self.run_job(fail_at="package")
        row, job = self.row(), self.job()
        assert job.status == "failed", job.status
        assert row.status == edge.expect["agent_status"], (row.status, row.error)
        out = self.ada.get(f"/api/agents/{self.agent_id}").json()
        assert out["last_deploy_failed"] is edge.expect["last_deploy_failed"], out
        assert "package exploded" in (out["last_deploy_error"] or "")

    def _delete_ok(self, edge) -> None:
        res = self.ada.delete(f"/api/agents/{self.agent_id}")
        body = self.expect(res, edge)
        assert body["deleted"] is True and body["agent_id"] == self.agent_id
        assert self.row().status == "deleted"
        assert self.ada.get(f"/api/agents/{self.agent_id}").status_code == 404

    edge_delete_active = _delete_ok
    edge_delete_failed = _delete_ok

    def edge_delete_deleted(self, edge) -> None:
        self.expect(self.ada.delete(f"/api/agents/{self.agent_id}"), edge)

    def edge_redeploy_deleted(self, edge) -> None:
        self.expect(self.ada.post(f"/api/agents/{self.agent_id}/redeploy", json=self.spec()), edge)

    def edge_chat_deleted(self, edge) -> None:
        self.expect(self.chat(self.ada), edge)

    def edge_create_after_delete_same_name(self, edge) -> None:
        old = self.agent_id
        self.accept_create(self.ada.post("/api/agents", json=self.spec()), edge)
        assert self.agent_id != old

    def edge_member_redeploy_other(self, edge) -> None:
        res = self.bob.post(f"/api/agents/{self.agent_id}/redeploy", json=self.spec())
        body = self.expect(res, edge)
        assert body["detail"]["agent_id"] == self.agent_id
        assert self.row().status == "active"

    def edge_member_delete_other(self, edge) -> None:
        body = self.expect(self.bob.delete(f"/api/agents/{self.agent_id}"), edge)
        assert body["detail"]["agent_id"] == self.agent_id
        assert self.row().status == "active"

    def edge_chat_active(self, edge) -> None:
        res = self.chat(self.ada)
        self.expect(res, edge)
        assert "event: done" in res.text, res.text[:300]
        sessions = self.ada.get(f"/api/chat/{self.agent_id}/sessions").json()["sessions"]
        assert sessions and sessions[-1]["turns"] >= 1


@pytest.fixture
def make_world(admin, ada, bob, seams):
    """A fresh world per call (the walk shrinker replays on fresh worlds)."""
    _ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return lambda: AgentWorld({"admin": admin, "ada": ada_client, "bob": bob_client}, seams)


@pytest.fixture
def world(make_world) -> AgentWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(AgentWorld)


@pytest.mark.parametrize("path", params_for(AgentWorld))
def test_agent_path(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(AgentWorld, include_covered_pairs=True))
def test_soak_agent_every_edge_pair(world, path):
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_agent_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, AgentWorld, GRAPHS["agent"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(AgentWorld, GRAPHS))
def test_agent_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(AgentWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_agent_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    world.run(path)
