# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the EVALUATION RUN graph (round 6, r17-l; testing.md §6.9): the
worker's replay invoke and judge serve the recorded shapes at `dispatch_completes`."""

from __future__ import annotations

import uuid

import pytest

import app.evaluation.routers as eval_routers
import app.evaluation.service as svc
from app.core.db import SessionLocal
from app.models.ledger import Agent
from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import EvalRunFaults, make_client_dict
from tests.graph.test_graph_eval_run import ITEMS, HeldQueue
from tests.test_visibility import _active_agent, _dataset, ada, admin, app, bob  # noqa: F401


@pytest.fixture
def make_world(monkeypatch, admin, ada, bob):
    clients, ada_id = make_client_dict(admin, ada, bob)

    def make() -> EvalRunFaults:
        queue = HeldQueue()
        monkeypatch.setattr(svc, "run_queue", queue)
        monkeypatch.setattr(eval_routers, "run_queue", queue)
        tag = uuid.uuid4().hex[:6]
        agent_id = _active_agent(f"g-feval-{tag}", created_by=ada_id)
        deploying = _active_agent(f"g-feval-dep-{tag}", created_by=ada_id)
        db = SessionLocal()
        try:
            db.get(Agent, deploying).status = "deploying"
            db.commit()
        finally:
            db.close()
        dataset = _dataset(ada_id, f"g-fds-{tag}", items=list(ITEMS))
        return EvalRunFaults(clients, monkeypatch, ada_id, agent_id, deploying, dataset, queue)

    return make


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_eval_run_fault_walk(make_world, family, seed):
    faults.run_fault_walk(make_world, EvalRunFaults, GRAPHS["eval_run"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(EvalRunFaults, "eval_run"))
def test_eval_run_refusal_seam_axis(make_world, edge_id, shape):
    run_axis_cell(make_world, "eval_run", edge_id, shape)


@pytest.mark.parametrize("shape_name", ["s3_slow_down", "s3_internal_error", "s3_access_denied"])
@pytest.mark.parametrize("at", ["store_status", "store_results"])
def test_eval_poller_store_read_fault_probe(make_world, shape_name, at):
    """Round 8 (r17-r): one S3 fault on the poller's artefact-store read (`status.json` /
    `results.jsonl`) while the worker job runs — `execute_run` never raises, the row carries the
    failure; `recheck_run` re-attaches by `batch_eval_id` and settles from `done.json`
    (evaluation-engine.md §5). The world's `_faulted_store_read` records what the member saw."""
    world = make_world()
    graph = GRAPHS["eval_run"]
    world.step(graph.edge("create"))
    out = world.fault_step(graph.edge("dispatch_completes"),
                           faults.with_at(faults.SHAPES[shape_name], at), expected_dst="completed")
    print("OBS", shape_name, at, "|", out.observation)


@pytest.mark.parametrize("shape_name", ["s3_slow_down", "s3_internal_error"])
@pytest.mark.parametrize("at", ["store_status", "store_results"])
def test_N17_obs123_a_transient_poller_read_fault_does_not_fail_the_run(make_world, shape_name,
                                                                        at):
    """N17-obs123 (fixed in cn/r17-t): ONE transient S3 fault on the poller's `status.json` /
    `results.jsonl` read is retried in place (`service._read_store`, the shared predicate, the
    bounded `STORE_READ_BACKOFF_S`) — the run completes with no `recheck`; before the fix the
    ClientError escaped `_poll` and the run was `failed` while the worker job completed."""
    world = make_world()
    graph = GRAPHS["eval_run"]
    world.step(graph.edge("create"))
    out = world.fault_step(graph.edge("dispatch_completes"),
                           faults.with_at(faults.SHAPES[shape_name], at), expected_dst="completed")
    assert not out.observation, out.observation  # the run completed without a recheck
