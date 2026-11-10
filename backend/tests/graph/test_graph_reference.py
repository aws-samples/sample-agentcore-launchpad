# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""REFERENCE graph — cross-entity dependencies walked through the real routes (round 16, r16-o).

Graph: ``tests/graph/graphs/reference.yaml``. One wired world: ada's active agent mounts a
knowledge base and pins a managed memory; a run of ada's is replaying her dataset; a running
canary on the agent lists a custom evaluator. Hermetic: the self-hosted KB / memory backends
run on the N6 fakes, the agent delete's AWS seams are recorded no-ops, the chat producer is
scripted. Every delete below is attempted while (or after) another row still points at the
target.
"""

from __future__ import annotations

import os
import uuid

import pytest

import app.routers.agents as agents_router
import app.services.chat as chat_service
from app.core.db import SessionLocal
from app.evaluation.models import EvalRun
from app.models.ledger import Agent, SpecSnapshot
from app.optimization.models import RuntimeCanary
from app.services import knowledge_backend, memory_backend
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from tests.conftest import set_default_resources, ws_ctx
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.walks import run_walk, walk_seeds
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_selfhosted import SUBSTRATE, T0
from tests.test_visibility import (  # noqa: F401 — fixtures by name
    _active_agent,
    _add,
    _canary,
    _dataset,
    _evaluator,
    ada,
    admin,
    app,
    bob,
)

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"


@pytest.fixture
def seams(monkeypatch):
    fakes = {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
             "embedder": FakeEmbedder()}
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    fakes["opensearch"].ensure_index(core.memory_index(""), core.memory_index_body())
    knowledge_backend.set_backend(SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0))
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    set_default_resources(SUBSTRATE)
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, ws: None)
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(
        chat_service, "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: iter(
            [{"event": "delta", "data": {"text": "ok"}}]),
    )
    return fakes


def _count(model, **by) -> int:
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**by).count()
    finally:
        db.close()


def _set(model, row_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(model, row_id)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


class ReferenceWorld(World):
    graph = "reference"
    UNDRIVEN: dict[str, str] = {}
    # N16-obs111 (agent_delete_with_running_canary) was pinned here until cn/r16-q added the
    # 409 `agent.in_use` guard; the edge is a refusal self-loop now.
    XFAIL: dict[str, str] = {}
    #: N17-obs31 (= N16-obs34) fixed in cn/r17-h: the force strip is an edge of its own now
    FIXED: dict[str, list[str]] = {"N17-obs31": ["kb_force_delete_strips"]}

    def __init__(self, clients, ada_id: str) -> None:
        super().__init__(clients)
        self.ada_id = ada_id
        self.gone: set[str] = set()  # kinds deleted so far (the graph's *_again refusals)
        tag = uuid.uuid4().hex[:6]
        ws = ws_ctx(SUBSTRATE, region="cn-northwest-1", id="default")
        kb = knowledge_backend.get_backend().create_kb(ws, f"g-kb-{tag}", "", {"mode": "upload"})
        self.kb_id = kb["kb_id"]
        res = self.admin.post("/api/memory/resources", json={"name": f"g_mem_{tag}"})
        assert res.status_code == 201, res.text
        self.memory_id = res.json()["id"]
        self.agent_id = _active_agent(f"g-ref-{tag}", created_by=ada_id)
        spec = {"name": f"g-ref-{tag}", "method": "zip_runtime", "system_prompt": "p",
                "protocol": "http", "knowledge_bases": [{"kb_id": self.kb_id}],
                "memory": {"memory_id": self.memory_id}}
        _set(Agent, self.agent_id, spec=spec)
        self.dataset_id = _dataset(ada_id, f"g-ref-ds-{tag}")
        self.run_id = _add(EvalRun(
            agent_id=self.agent_id, agent_name=f"g-ref-{tag}", status="evaluating",
            mode="evaluators", evaluators=["Builtin.Correctness"], created_by=ada_id,
            created_by_name="ada", dataset_id=self.dataset_id, dataset_name=f"g-ref-ds-{tag}",
        ))
        self.evaluator_id = _evaluator(f"g-ref-ev-{tag}", ada_id)
        self.canary_id = _canary(
            ada_id, champion_agent_id=self.agent_id, challenger_agent_id=self.agent_id,
            status="running", artifacts={"online_evaluators": [self.evaluator_id]},
        )
        self.ids |= {self.kb_id, self.memory_id, self.agent_id, self.dataset_id, self.run_id,
                     self.evaluator_id, self.canary_id}

    # -- helpers -------------------------------------------------------------------
    def agent_status(self) -> str:
        db = SessionLocal()
        try:
            return db.get(Agent, self.agent_id).status
        finally:
            db.close()

    def canary_status(self) -> str:
        db = SessionLocal()
        try:
            return db.get(RuntimeCanary, self.canary_id).status
        finally:
            db.close()

    def run_status(self) -> str:
        db = SessionLocal()
        try:
            return db.get(EvalRun, self.run_id).status
        finally:
            db.close()

    def before(self, edge) -> None:
        if edge.id == "agent_delete_releases_kb":
            if self.canary_status() == "running":
                _set(RuntimeCanary, self.canary_id, status="cleaned")  # "(no canary"
            if self.run_status() in ("queued", "invoking", "waiting", "evaluating"):
                _set(EvalRun, self.run_id, status="completed")  # ", no ACTIVE run)" — §1.3

    # -- edges: guards while wired -------------------------------------------------------
    def edge_kb_force_delete_strips(self, edge) -> None:
        """API_KB_DELETE (N17-obs31 = N16-obs34, fixed in cn/r17-h): the force strips the pin from
        the mounting agent in the shared layer, marks it `kb_stale`, writes no snapshot, and the
        shared ledger scan has no "pins knowledge base … that no longer exists" line left."""
        snaps_before = _count(SpecSnapshot, agent_id=self.agent_id)
        body = self.expect(self.admin.delete(f"/api/knowledge-bases/{self.kb_id}?force=true"), edge)
        assert body["deleted"] is True
        assert [a["id"] for a in body["stripped_agents"]] == [self.agent_id]
        out = self.ada.get(f"/api/agents/{self.agent_id}").json()
        assert out["spec"].get("knowledge_bases", []) == []
        assert out["kb_stale"] is True and out["last_strip"]["kb_ids"] == [self.kb_id]
        assert _count(SpecSnapshot, agent_id=self.agent_id) == snaps_before
        assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 404
        self.gone.add("kb")

    def edge_kb_force_delete_member(self, edge) -> None:
        """API_KB_DELETE (N17-obs4): a member's force is the administrator's override refused —
        403 before any ledger guard, the mount and the KB untouched (digest oracle)."""
        self.expect(self.ada.delete(f"/api/knowledge-bases/{self.kb_id}?force=true"), edge)
        out = self.ada.get(f"/api/agents/{self.agent_id}").json()
        assert [k["kb_id"] for k in out["spec"]["knowledge_bases"]] == [self.kb_id]
        assert out["kb_stale"] is False
        assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 200

    def edge_kb_delete_again(self, edge) -> None:
        assert "kb" in self.gone
        self.expect(self.admin.delete(f"/api/knowledge-bases/{self.kb_id}"), edge)

    def edge_kb_delete_mounted(self, edge) -> None:
        body = self.expect(self.admin.delete(f"/api/knowledge-bases/{self.kb_id}"), edge)
        assert body["detail"]["agents"]
        assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 200

    def edge_memory_delete_pinned(self, edge) -> None:
        body = self.expect(self.admin.delete(f"/api/memory/resources/{self.memory_id}"), edge)
        assert {a["id"] for a in body["detail"]["agents"]} == {self.agent_id}
        assert self.admin.get(f"/api/memory/resources/{self.memory_id}").status_code == 200

    def edge_dataset_delete_active_run(self, edge) -> None:
        body = self.expect(self.ada.delete(f"/api/eval/datasets/{self.dataset_id}"), edge)
        assert {(r["kind"], r["id"]) for r in body["detail"]["referrers"]} == {("run", self.run_id)}

    def edge_dataset_force_member(self, edge) -> None:
        self.expect(self.ada.delete(f"/api/eval/datasets/{self.dataset_id}?force=true"), edge)

    def edge_evaluator_delete_running_canary(self, edge) -> None:
        body = self.expect(self.ada.delete(f"/api/eval/evaluators/{self.evaluator_id}"), edge)
        assert {(r["kind"], r["id"]) for r in body["detail"]["referrers"]} == {
            ("canary", self.canary_id)}

    def edge_agent_delete_with_running_canary(self, edge) -> None:
        """AGENTS_1_1 / UO_8_AGENT_IN_USE (N16-obs111 fixed in cn/r16-q): the creator's
        delete is refused with 409 `agent.in_use` naming the running canary; the agent stays
        `active`, the canary `running`, and (LEDGER_CP) no canary ever names a deleted
        Agent. The same refusal for the administrator — no force for anyone."""
        for client in (self.ada, self.admin):
            body = self.expect(client.delete(f"/api/agents/{self.agent_id}"), edge)
            assert body["detail"] == {  # AGENTS_1_3: both kinds on every refusal
                "canaries": [{"id": self.canary_id, "name": "c", "status": "running",
                              "stage": "setup"}],
                "runs": [{"id": self.run_id, "name": None, "status": "evaluating"}]}
        assert self.agent_status() == edge.expect["agent_kept"]
        assert self.canary_status() == edge.expect["canary_kept"]
        # still visible, still the same row: a refusal is not a soft delete
        assert self.ada.get(f"/api/agents/{self.agent_id}").json()["status"] == "active"

    def edge_agent_delete_with_active_run(self, edge) -> None:
        """AGENTS_1_3 (N17-obs18 fixed in cn/r17-f): the canary is cleaned, the replaying run
        still `evaluating` — the delete is refused naming the run alone, for the creator and
        the administrator; the agent stays `active`, the run keeps its status."""
        assert self.canary_status() == "cleaned"
        for client in (self.ada, self.admin):
            body = self.expect(client.delete(f"/api/agents/{self.agent_id}"), edge)
            assert body["detail"] == {
                "canaries": [],
                "runs": [{"id": self.run_id, "name": None, "status": "evaluating"}]}
        assert self.agent_status() == edge.expect["agent_kept"]
        assert self.run_status() == edge.expect["run_kept"]

    # -- edges: the agent goes -------------------------------------------------------------
    def edge_agent_delete_releases_kb(self, edge) -> None:
        res = self.ada.delete(f"/api/agents/{self.agent_id}")
        assert res.status_code == 200, res.text
        assert self.agent_status() == edge.expect["agent_status"]
        res = self.admin.delete(f"/api/knowledge-bases/{self.kb_id}")
        assert res.status_code == edge.expect["kb_delete_status"], res.text
        assert res.json()["deleted"] is True

    def edge_memory_delete_after_agent_gone(self, edge) -> None:
        assert "memory" not in self.gone, "the graph reaches this edge once (N17-obs11)"
        self.expect(self.admin.delete(f"/api/memory/resources/{self.memory_id}"), edge)
        self.gone.add("memory")
        assert self.admin.get(f"/api/memory/resources/{self.memory_id}").status_code == 404

    def edge_memory_delete_again(self, edge) -> None:
        """API_MEMORY_RESOURCE_DELETE "the managed registration is dropped" + the per-id rule:
        the id is not managed any more, so the repeat is the 404 before any AWS call."""
        assert "memory" in self.gone
        self.expect(self.admin.delete(f"/api/memory/resources/{self.memory_id}"), edge)


    def edge_chat_agent_gone(self, edge) -> None:
        self.expect(self.ada.post(f"/api/chat/{self.agent_id}", json={"prompt": "hi"}), edge)

    edge_chat_agent_gone_memory_gone = edge_chat_agent_gone

    def edge_run_create_agent_gone(self, edge) -> None:
        res = self.ada.post("/api/eval/runs", json={
            "agent_id": self.agent_id, "dataset_id": self.dataset_id,
            "evaluators": ["Builtin.Correctness"], "wait_seconds": 0})
        self.expect(res, edge)

    def edge_canary_create_agent_gone(self, edge) -> None:
        res = self.ada.post("/api/runtime-canaries", json={
            "agent_id": self.agent_id, "candidate": {"system_prompt": "x"}})
        self.expect(res, edge)

    # -- edges: the referrer finishes --------------------------------------------------------
    def edge_run_finishes(self, edge) -> None:
        _set(EvalRun, self.run_id, status="completed")
        assert self.ada.get(f"/api/eval/runs/{self.run_id}").json()["status"] == "completed"

    def edge_dataset_delete_finished_run(self, edge) -> None:
        assert "dataset" not in self.gone, "the graph reaches this edge once (N17-obs11)"
        name = self.ada.get(f"/api/eval/runs/{self.run_id}").json()["dataset_name"]
        body = self.expect(self.ada.delete(f"/api/eval/datasets/{self.dataset_id}"), edge)
        assert body["deleted"] is True
        self.gone.add("dataset")
        assert self.ada.get(f"/api/eval/runs/{self.run_id}").json()["dataset_name"] == name

    def edge_dataset_delete_again(self, edge) -> None:
        assert "dataset" in self.gone
        name = self.ada.get(f"/api/eval/runs/{self.run_id}").json()["dataset_name"]
        self.expect(self.ada.delete(f"/api/eval/datasets/{self.dataset_id}"), edge)
        assert self.ada.get(f"/api/eval/runs/{self.run_id}").json()["dataset_name"] == name

    def edge_canary_cleaned(self, edge) -> None:
        _set(RuntimeCanary, self.canary_id, status="cleaned")
        assert self.canary_status() == "cleaned"

    def edge_evaluator_delete_cleaned_canary(self, edge) -> None:
        assert "evaluator" not in self.gone, "the graph reaches this edge once (N17-obs11)"
        body = self.expect(self.ada.delete(f"/api/eval/evaluators/{self.evaluator_id}"), edge)
        assert body["deleted"] is True
        self.gone.add("evaluator")

    def edge_evaluator_delete_again(self, edge) -> None:
        assert "evaluator" in self.gone
        self.expect(self.ada.delete(f"/api/eval/evaluators/{self.evaluator_id}"), edge)


@pytest.fixture
def make_world(admin, ada, bob, seams):
    """A fresh wired world per call — the walk shrinker replays on fresh worlds."""
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return lambda: ReferenceWorld(
        {"admin": admin, "ada": ada_client, "bob": bob_client}, ada_id)


@pytest.fixture
def world(make_world) -> ReferenceWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(ReferenceWorld)


@pytest.mark.parametrize("path", params_for(ReferenceWorld))
def test_reference_path(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(ReferenceWorld, include_covered_pairs=True))
def test_soak_reference_every_edge_pair(world, path):
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_reference_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, ReferenceWorld, GRAPHS["reference"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(ReferenceWorld, GRAPHS))
def test_reference_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(ReferenceWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_reference_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    world.run(path)
