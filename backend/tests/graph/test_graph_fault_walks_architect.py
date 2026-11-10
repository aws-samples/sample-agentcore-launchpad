# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the ARCHITECT PROPOSAL graph (round 9, r17-s; testing.md §6.9): the
CLEAR job's AWS readbacks — GetAgentRuntime (`_wait_gone`), ListAgentRuntimeEndpoints,
DeleteAgentRuntime and the residue sweep's role step — serve the recorded shapes at
`clear_deployed`; the retry is the recovery oracle."""

from __future__ import annotations

import pytest

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import ProposalFaults, make_client_dict
from tests.test_assistant import (  # noqa: F401 — autouse fixtures + helpers by name
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)
from tests.test_assistant_purge_dependencies import cloud  # noqa: F401 — fixture by name
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name


@pytest.fixture
def make_world(admin, ada, bob, cloud, ready, harness):
    clients, _ada_id = make_client_dict(admin, ada, bob)
    return lambda: ProposalFaults(clients, harness, cloud)


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_architect_proposal_fault_walk(make_world, family, seed):
    faults.run_fault_walk(make_world, ProposalFaults, GRAPHS["architect_proposal"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(ProposalFaults, "architect_proposal"))
def test_architect_proposal_refusal_seam_axis(make_world, edge_id, shape):
    run_axis_cell(make_world, "architect_proposal", edge_id, shape)


@pytest.mark.parametrize("at", ["agent_readback", "foreign_endpoints", "agent_delete",
                                "residue_role"])
@pytest.mark.parametrize("shape_name", ["rt_access_denied", "runtime_throttle_client",
                                        "rt_conflict_modifying", "rt_read_timeout"])
def test_clear_deployed_fault_probe(make_world, at, shape_name):
    """Every (seam, shape) once on the shortest prefix to `deployed` — the walks draw a subset."""
    shape = faults.SHAPES[shape_name]
    if at == "residue_role" and shape.kind != "client":
        pytest.skip("the residue step reports a text, not an exception")
    world = make_world()
    graph = GRAPHS["architect_proposal"]
    from tests.graph.paths import shortest_prefix

    for step in shortest_prefix(graph, "deployed") or ():
        world.step(step)
    out = world.fault_step(graph.edge("clear_deployed"), faults.with_at(shape, at),
                           expected_dst="cleared")
    print("OBS", at, shape_name, "|", out.observation or "-")
