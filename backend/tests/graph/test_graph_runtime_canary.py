# ruff: noqa: F811  — pytest fixtures are imported by name from the r16-e canary model
"""E13 — the Runtime-canary lifecycle graph walked through the real routes (round 16, r16-o).

Graph: ``tests/graph/graphs/runtime_canary.yaml``. Hermetic: the ``Seams`` of
``tests/model/test_canary_state_machine.py`` (r16-e) — setup / traffic / verdict / rollback
are ledger emulations writing the real artifacts, advance / complete / cleanup are the real
functions with the control-plane / S3 seams recorded; the canary row an accepted
``POST /api/runtime-canaries`` leaves is seeded (the real create mints a Runtime version).
Accounts: ada owns the agent and the canary, bob is another member, the administrator reads.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.optimization import canary_service, routing
from tests.graph.executor import World, assert_tables_explicit, params_for
from tests.model.test_canary_state_machine import (  # noqa: F401 — fixtures by name
    FINAL,
    VERDICTS,
    Seams,
    _agent,
    _assignments,
    _get,
    _new_canary,
    _online_ids,
    seams,
)
from tests.test_visibility import _active_agent, _dataset, ada, admin, app, bob  # noqa: F401

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
TREATMENT_WINS = VERDICTS[0]  # significant treatment-wins: advance and complete may proceed


class CanaryWorld(World):
    graph = "runtime_canary"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}
    #: N17-obs13 fixed in cn/r17-c: the publishes a live deploy job refuses (agents.md §1.2)
    #: stay in the neighbourhood set (the cross walk drives the refusal itself; the rollback
    #: edges are N16-obs116 gap edges and already in the set)
    FIXED: dict[str, list[str]] = {"N17-obs13": ["complete_final"]}

    def __init__(self, clients, ada_id: str, seams: Seams, dataset_id: str,
                 agent_id: str | None = None) -> None:
        super().__init__(clients)
        self.ada_id, self.seams, self.dataset_id = ada_id, seams, dataset_id
        tag = uuid.uuid4().hex[:6]
        # the cross-graph walks hand in the agent another world created (r17-b)
        self.agent_id = agent_id or _active_agent(f"g-canary-{tag}", created_by=ada_id)
        self.deploying_id = _active_agent(f"g-canary-dep-{tag}", created_by=ada_id)
        db = SessionLocal()
        try:
            db.get(Agent, self.deploying_id).status = "deploying"
            db.commit()
        finally:
            db.close()
        self.ids |= {self.agent_id, self.deploying_id}
        self.canary_id: str | None = None
        self.stage = 0

    # -- helpers -------------------------------------------------------------------
    def act(self, action: str, client=None, **body):
        payload = {"action": action, **body}
        return (client or self.ada).post(f"/api/runtime-canaries/{self.canary_id}/action",
                                         json=payload)

    def row(self):
        return _get(self.canary_id)

    def ok(self, res, edge) -> dict:
        body = self.expect(res, edge, status=202)
        row = self.row()
        assert row.running_action is None and row.error is None, (row.running_action, row.error)
        return body

    def create_body(self, agent_id: str) -> dict:
        return {"agent_id": agent_id, "candidate": {"system_prompt": "candidate prompt"}}

    # -- edges -----------------------------------------------------------------------
    def edge_create_inactive_agent(self, edge) -> None:
        res = self.ada.post("/api/runtime-canaries", json=self.create_body(self.deploying_id))
        self.expect(res, edge)

    def _seed(self, edge) -> None:
        self.canary_id = _new_canary(_agent(self.agent_id), self.ada_id)
        self.ids.add(self.canary_id)
        self.stage = 0
        assert self.row().status == edge.expect["canary_status"]

    edge_seed_running = _seed
    edge_create_after_cleaned = _seed

    def edge_create_already_running(self, edge) -> None:
        self.expect(self.ada.post("/api/runtime-canaries", json=self.create_body(self.agent_id)),
                    edge)

    def _stage_not_ready(self, action: str, edge, **body) -> None:
        self.expect(self.act(action, **body), edge)

    def edge_traffic_before_setup(self, edge) -> None:
        self._stage_not_ready("traffic", edge, dataset_id=self.dataset_id)

    def edge_setup(self, edge) -> None:
        self.ok(self.act("setup"), edge)
        row = self.row()
        assert int(row.artifacts["setup"]["ramp_stage"]) == 0
        assert len(_online_ids(self.canary_id)) == edge.expect["online_configs"]
        assert row.artifacts["abtest"]["execution_status"] == routing.RUNNING

    def edge_setup_twice(self, edge) -> None:
        self._stage_not_ready("setup", edge)

    def edge_verdict_before_traffic(self, edge) -> None:
        self._stage_not_ready("verdict", edge)

    def edge_traffic_without_dataset(self, edge) -> None:
        self.expect(self.act("traffic"), edge)

    def edge_traffic(self, edge) -> None:
        before = _assignments(self.canary_id)
        self.ok(self.act("traffic", dataset_id=self.dataset_id), edge)
        assert _assignments(self.canary_id) > before

    edge_traffic_s1 = edge_traffic
    edge_traffic_s2 = edge_traffic
    edge_verdict_before_traffic_s1 = edge_verdict_before_traffic
    edge_verdict_before_traffic_s2 = edge_verdict_before_traffic

    def edge_advance_before_verdict(self, edge) -> None:
        self._stage_not_ready("advance", edge)

    edge_advance_before_verdict_s1 = edge_advance_before_verdict
    edge_advance_before_verdict_s2 = edge_advance_before_verdict

    def _verdict(self, edge, client=None) -> None:
        self.seams.verdict_queue = [dict(TREATMENT_WINS)]
        self.ok(self.act("verdict", client=client), edge)
        _, current = canary_service._current_round(self.row())
        assert current["verdict"]["verdict"] == "treatment-wins"

    def edge_verdict(self, edge) -> None:
        self._verdict(edge)

    edge_verdict_s1 = edge_verdict
    edge_verdict_s2 = edge_verdict

    def edge_member_verdict(self, edge) -> None:
        self._verdict(edge, client=self.bob)

    edge_member_verdict_s1 = edge_member_verdict
    edge_member_verdict_s2 = edge_member_verdict

    def edge_other_member_advance(self, edge) -> None:
        body = self.expect(self.act("advance", client=self.bob), edge)
        # §8: `{created_by, created_by_name, <kind>_id, action}`
        detail = body["detail"]
        assert detail["canary_id"] == self.canary_id and detail["action"] == "advance"
        assert detail["created_by"] == self.ada_id and detail["created_by_name"] == "ada"

    edge_other_member_advance_s1 = edge_other_member_advance
    edge_other_member_advance_s2 = edge_other_member_advance

    def edge_complete_before_final(self, edge) -> None:
        assert self.stage < FINAL, "the graph reaches this edge at stages 0 / 1 only (N17-obs14)"
        self._stage_not_ready("complete", edge)

    edge_complete_before_final_s1 = edge_complete_before_final

    def edge_advance(self, edge) -> None:
        assert self.stage < FINAL, "the graph advances twice at most (N17-obs14: AC-E13-07)"
        self.ok(self.act("advance"), edge)
        self.stage += 1
        assert self.stage == edge.expect["stage"]
        row = self.row()
        assert int(row.artifacts["setup"]["ramp_stage"]) == self.stage
        # AC-E13-07: `weights [50,50] → [1,99]` — the contract's own ramp
        want = {1: {"C": 50, "T1": 50}, 2: {"C": 1, "T1": 99}}[self.stage]
        assert row.artifacts["setup"]["weights"] == want
        # "a new round pops the previous verdict"
        _, current = canary_service._current_round(row)
        assert current is None or not current.get("verdict")

    edge_advance_s1 = edge_advance

    def edge_advance_at_final(self, edge) -> None:
        assert self.stage == FINAL
        self._stage_not_ready("advance", edge)

    def edge_complete_final(self, edge) -> None:
        assert self.stage == FINAL, "the graph brings the canary to 1/99 first (N17-obs14)"
        self.ok(self.act("complete"), edge)
        row, agent = self.row(), _agent(self.agent_id)
        assert row.status == "completed"
        assert agent.version == row.artifacts["setup"]["v_candidate"]
        assert agent.spec["system_prompt"] == "candidate prompt"
        assert row.artifacts["abtest"]["execution_status"] == routing.STOPPED

    def _rollback(self, edge) -> None:
        self.ok(self.act("rollback"), edge)
        row = self.row()
        assert row.status == "rolled_back"
        if row.artifacts.get("abtest"):
            assert row.artifacts["abtest"]["execution_status"] == routing.STOPPED

    edge_rollback = _rollback
    edge_rollback_fresh = _rollback
    edge_rollback_s1 = _rollback
    edge_rollback_s2 = _rollback

    def edge_action_after_complete(self, edge) -> None:
        self._stage_not_ready("advance", edge)

    def edge_action_after_rollback(self, edge) -> None:
        self._stage_not_ready("traffic", edge, dataset_id=self.dataset_id)

    def _cleanup(self, edge) -> None:
        self.ok(self.act("cleanup"), edge)
        row = self.row()
        assert row.status == "cleaned"
        assert _online_ids(self.canary_id) == []
        assert _assignments(self.canary_id) == 0
        if row.artifacts.get("abtest"):
            assert row.artifacts["abtest"]["execution_status"] == routing.STOPPED

    edge_cleanup_completed = _cleanup
    edge_cleanup_rolled_back = _cleanup
    edge_cleanup_again = _cleanup


@pytest.fixture
def make_world(admin, ada, bob, seams):
    """A fresh world per call: its own agents and dataset (the walk shrinker replays on
    fresh worlds)."""
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return lambda: CanaryWorld(
        {"admin": admin, "ada": ada_client, "bob": bob_client}, ada_id, seams,
        _dataset(None, name=f"g-canary-ds-{uuid.uuid4().hex[:6]}"))


@pytest.fixture
def world(make_world) -> CanaryWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(CanaryWorld)


@pytest.mark.parametrize("path", params_for(CanaryWorld, requirements=("prime",), shard=(0, 2)))
def test_runtime_canary_path(world, path):
    """Every second prime path (shard 0 of 2); the other shard is
    ``test_graph_runtime_canary_primes_b.py``, the edge pairs / walks / neighbourhood are
    ``test_graph_runtime_canary_loops.py`` — the unrolled ramp made the canary graph the largest
    (233 prime paths, each ramping up to 9 edges) and one file broke the 60 s budget (rev-r17)."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(CanaryWorld, include_covered_pairs=True))
def test_soak_runtime_canary_every_edge_pair(world, path):
    world.run(path)

