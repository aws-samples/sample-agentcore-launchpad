# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E11 — the evaluation-run lifecycle graph walked through the real routes (round 16, r16-o).

Graph: ``tests/graph/graphs/eval_run.yaml``. Hermetic: the hermetic worker of
``tests/evaluation/test_runs_flow.py`` (recording invoker, Logs double, ``FakeJudge``) behind
a HELD queue — a submitted run stays ``queued`` until the walk dispatches it, so the queued
edges (stop, in-use dataset, compare) are reachable. Accounts: ada creates; bob is another
member; the administrator reads.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

import pytest

import app.evaluation.routers as eval_routers
import app.evaluation.service as svc
from app.core.db import SessionLocal
from app.evaluation.models import EvalResult, EvalRun
from tests.evaluation.test_runs_flow import stub_environment, wait_terminal
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.walks import run_walk, walk_seeds
from tests.test_visibility import (  # noqa: F401 — fixtures by name
    _active_agent,
    _add,
    _dataset,
    ada,
    admin,
    app,
    bob,
)

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
EVALUATORS = ["Builtin.Correctness", "Builtin.Helpfulness"]
ITEMS = [{"prompt": "2+2?"}, {"prompt": "3+3?"}]


class HeldQueue:
    """The run queue as a hand-cranked stub: ``submit`` holds the callable, ``dispatch`` runs
    it inline (the real queue would do so on a worker thread)."""

    def __init__(self) -> None:
        self.held: dict[str, Callable[[], None]] = {}

    def submit(self, run_id: str, fn: Callable[[], None]) -> int:
        self.held[run_id] = fn
        return 0

    def cancel(self, run_id: str) -> bool:
        return self.held.pop(run_id, None) is not None

    def position(self, run_id: str) -> int | None:
        return 0 if run_id in self.held else None

    def dispatch(self, run_id: str) -> None:
        self.held.pop(run_id)()

    def state(self) -> dict[str, Any]:
        return {"running": 0, "queued": len(self.held), "locked": [], "max_concurrency": 1}


def _run(run_id: str) -> EvalRun:
    db = SessionLocal()
    try:
        row = db.get(EvalRun, run_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _results(run_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(EvalResult).filter(EvalResult.run_id == run_id).count()
    finally:
        db.close()


class EvalRunWorld(World):
    graph = "eval_run"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}

    def __init__(self, clients, monkeypatch, ada_id: str, agent_id: str, deploying_id: str,
                 dataset_id: str, queue: HeldQueue) -> None:
        super().__init__(clients)
        self.monkeypatch = monkeypatch
        self.ada_id, self.agent_id, self.deploying_id = ada_id, agent_id, deploying_id
        self.dataset_id, self.queue = dataset_id, queue
        self.run_id: str | None = None
        self.completed: list[str] = []
        self.other: str | None = None  # a second completed run on the dataset (compare)
        self.sessions_run: str | None = None  # a seeded chosen-session run (rerun refusal)
        self.dataset_deleted = False
        self.ids |= {agent_id, dataset_id}
        self.env, self.calls = stub_environment(monkeypatch)
        self.n = 0

    # -- helpers -------------------------------------------------------------------
    def body(self, **over) -> dict:
        self.n += 1
        return {"agent_id": self.agent_id, "dataset_id": self.dataset_id,
                "evaluators": list(EVALUATORS), "wait_seconds": 0,
                "name": f"g-run-{self.n}", "description": "graph walk", **over}

    def create(self, edge, **over) -> str:
        res = self.ada.post("/api/eval/runs", json=self.body(**over))
        body = self.expect(res, edge, status=201)
        assert body["status"] == "queued", body
        self.ids.add(body["id"])
        return body["id"]

    def dispatch(self, run_id: str) -> dict:
        self.queue.dispatch(run_id)
        return wait_terminal(self.ada, run_id)

    def another_completed_run(self) -> str:
        """A second completed run on the same dataset (compare needs two) — staged once."""
        if self.other is None:
            assert not self.dataset_deleted, "stage the second run before the dataset goes"
            rid = self.ada.post("/api/eval/runs", json=self.body()).json()["id"]
            self.ids.add(rid)
            out = self.dispatch(rid)
            assert out["status"] == "completed", out.get("error")
            self.other = rid
        return self.other

    def before(self, edge) -> None:
        # the second completed run a compare needs is staged BEFORE the dataset goes (at
        # `completed_dataset_gone` nothing can be created on the dataset any more)
        if edge.id in ("compare_completed_pair", "compare_with_queued",
                       "dataset_delete_after_completed", "compare_dataset_gone"):
            self.another_completed_run()
        if edge.id == "rerun_sessions_scope" and self.sessions_run is None:
            self.sessions_run = _add(EvalRun(
                agent_id=self.agent_id, agent_name="g", status="completed", mode="evaluators",
                evaluators=list(EVALUATORS), session_ids=["s" * 40], dataset_name="sessions:1",
                created_by=self.ada_id, created_by_name="ada",
            ))
            self.ids.add(self.sessions_run)

    def current(self) -> EvalRun:
        return _run(self.run_id)

    # -- edges -----------------------------------------------------------------------
    def edge_create(self, edge) -> None:
        self.run_id = self.create(edge)

    def edge_create_inactive_agent(self, edge) -> None:
        self.expect(self.ada.post("/api/eval/runs", json=self.body(agent_id=self.deploying_id)),
                    edge)

    def edge_create_no_evaluators(self, edge) -> None:
        self.expect(self.ada.post("/api/eval/runs", json=self.body(evaluators=[])), edge)

    def edge_dataset_delete_while_queued(self, edge) -> None:
        body = self.expect(self.ada.delete(f"/api/eval/datasets/{self.dataset_id}"), edge)
        assert ("run", self.run_id) in {(r["kind"], r["id"]) for r in body["detail"]["referrers"]}

    def edge_stop_queued(self, edge) -> None:
        body = self.expect(self.ada.post(f"/api/eval/runs/{self.run_id}/stop"), edge)
        assert body["status"] == "stopped" and self.current().status == "stopped"
        assert self.run_id not in self.queue.held  # cancelled locally, never dispatched

    def edge_dispatch_completes(self, edge) -> None:
        before = self.calls["n"]
        out = self.dispatch(self.run_id)
        assert out["status"] == "completed", out.get("error")
        assert self.calls["n"] - before == len(ITEMS)
        assert _results(self.run_id) == len(ITEMS) * len(EVALUATORS)
        assert out["snapshot"] is None or out["snapshot"]["seq"] is not None
        self.completed.append(self.run_id)

    def edge_dispatch_fails(self, edge) -> None:
        self.env, self.calls = stub_environment(
            self.monkeypatch, invoke_errors=[ValueError("runtime gone") for _ in ITEMS])
        out = self.dispatch(self.run_id)
        assert out["status"] == "failed", out
        assert out["error"]
        # back to a healthy worker for the rest of the walk
        self.env, self.calls = stub_environment(self.monkeypatch)

    def edge_stop_completed(self, edge) -> None:
        self.expect(self.ada.post(f"/api/eval/runs/{self.run_id}/stop"), edge)

    edge_stop_failed = edge_stop_completed

    def edge_delete_completed(self, edge) -> None:
        self.expect(self.ada.delete(f"/api/eval/runs/{self.run_id}"), edge)
        assert self.current().status == "completed"

    def edge_recheck_completed(self, edge) -> None:
        self.expect(self.ada.post(f"/api/eval/runs/{self.run_id}/recheck"), edge)

    def edge_dataset_delete_after_completed(self, edge) -> None:
        name = self.current().dataset_name
        assert not self.dataset_deleted, "the graph reaches this edge once (node split, N17-obs10)"
        body = self.expect(self.ada.delete(f"/api/eval/datasets/{self.dataset_id}"), edge)
        assert body["deleted"] is True
        self.dataset_deleted = True
        run = self.ada.get(f"/api/eval/runs/{self.run_id}").json()
        assert run["dataset_name"] == name and run["dataset_id"] == self.dataset_id

    def edge_rerun_dataset_gone(self, edge) -> None:
        """EE_23 — the rerun goes through the same gate as `POST /api/eval/runs`: the deleted
        dataset reads as missing, so the source run stays the newest and nothing is queued
        (N17-obs10: the `completed` node split on this history)."""
        assert self.dataset_deleted
        self.expect(self.ada.post(f"/api/eval/runs/{self.run_id}/rerun"), edge)
        assert self.current().status == "completed"

    def _rerun(self, edge) -> None:
        source = self.current()
        res = self.ada.post(f"/api/eval/runs/{self.run_id}/rerun")
        body = self.expect(res, edge)
        # differential: the new run is what POST /api/eval/runs would start from the
        # source's own fields — same target, evaluators, dataset and description
        assert body["id"] != source.id and body["status"] == "queued"
        assert body["agent_id"] == source.agent_id
        assert body["dataset_id"] == source.dataset_id
        assert body["evaluators"] == list(source.evaluators)
        assert body["description"] == source.description
        assert body["name"] == f"{source.name.removesuffix(' · 再评')} · 再评"
        assert body["created_by_name"] == "ada"
        self.run_id = body["id"]
        self.ids.add(self.run_id)

    edge_rerun_completed = _rerun
    edge_rerun_stopped = _rerun

    def edge_rerun_sessions_scope(self, edge) -> None:
        body = self.expect(self.ada.post(f"/api/eval/runs/{self.sessions_run}/rerun"), edge)
        assert body["detail"]["scope"] == "sessions"

    def edge_compare_completed_pair(self, edge) -> None:
        other = self.other
        res = self.ada.get(f"/api/eval/runs/compare?a={self.run_id}&b={other}")
        body = self.expect(res, edge)
        assert body["alignment"] == edge.expect["alignment"], body["alignment_reason"]
        assert {body["runs"]["a"]["id"], body["runs"]["b"]["id"]} == {self.run_id, other}
        assert len(body["cases"]) == len(ITEMS)

    def edge_compare_dataset_gone(self, edge) -> None:
        """EE_22 (the completed-run rule): both runs keep their snapshot, so the compare
        answers exactly as it did while the dataset existed — scenario-aligned, the stored
        `dataset_name` on both sides."""
        assert self.dataset_deleted
        name = self.current().dataset_name
        res = self.ada.get(f"/api/eval/runs/compare?a={self.run_id}&b={self.other}")
        body = self.expect(res, edge)
        assert body["alignment"] == edge.expect["alignment"], body["alignment_reason"]
        assert {body["runs"]["a"]["dataset_name"], body["runs"]["b"]["dataset_name"]} == {name}
        assert len(body["cases"]) == len(ITEMS)

    def edge_compare_with_queued(self, edge) -> None:
        done = self.other
        res = self.ada.get(f"/api/eval/runs/compare?a={done}&b={self.run_id}")
        self.expect(res, edge)

    def _delete_ok(self, edge) -> None:
        body = self.expect(self.ada.delete(f"/api/eval/runs/{self.run_id}"), edge)
        assert body["deleted"] is True and body["run_id"] == self.run_id
        assert self.ada.get(f"/api/eval/runs/{self.run_id}").status_code == 404

    edge_delete_failed = _delete_ok
    edge_delete_stopped = _delete_ok

    def edge_delete_deleted(self, edge) -> None:
        self.expect(self.ada.delete(f"/api/eval/runs/{self.run_id}"), edge)

    def edge_rerun_deleted(self, edge) -> None:
        self.expect(self.ada.post(f"/api/eval/runs/{self.run_id}/rerun"), edge)


@pytest.fixture
def make_world(monkeypatch, admin, ada, bob):
    """A fresh world per call: its own agents, dataset and held queue (the walk shrinker
    replays on fresh worlds)."""
    ada_id, ada_client = ada
    _bob_id, bob_client = bob

    def make() -> EvalRunWorld:
        queue = HeldQueue()
        monkeypatch.setattr(svc, "run_queue", queue)
        monkeypatch.setattr(eval_routers, "run_queue", queue)
        tag = uuid.uuid4().hex[:6]
        agent_id = _active_agent(f"g-eval-{tag}", created_by=ada_id)
        deploying = _active_agent(f"g-eval-dep-{tag}", created_by=ada_id)
        db = SessionLocal()
        try:
            from app.models.ledger import Agent

            db.get(Agent, deploying).status = "deploying"
            db.commit()
        finally:
            db.close()
        dataset = _dataset(ada_id, f"g-ds-{tag}", items=list(ITEMS))
        return EvalRunWorld({"admin": admin, "ada": ada_client, "bob": bob_client},
                            monkeypatch, ada_id, agent_id, deploying, dataset, queue)

    return make


@pytest.fixture
def world(make_world) -> EvalRunWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(EvalRunWorld)


@pytest.mark.parametrize("path", params_for(EvalRunWorld))
def test_eval_run_path(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(EvalRunWorld, include_covered_pairs=True))
def test_soak_eval_run_every_edge_pair(world, path):
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_eval_run_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, EvalRunWorld, GRAPHS["eval_run"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(EvalRunWorld, GRAPHS))
def test_eval_run_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(EvalRunWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_eval_run_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    world.run(path)
