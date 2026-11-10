# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the REFERENCE graph (round 6, r17-l; testing.md §6.9): the memory
store's S3 / OpenSearch purge, the KB force delete's cleanup steps and the agent's Runtime delete
serve the recorded shapes mid-walk."""

from __future__ import annotations

import pytest

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import ReferenceFaults, make_client_dict
from tests.graph.test_graph_reference import seams  # noqa: F401 — fixture by name
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name


@pytest.fixture
def make_world(admin, ada, bob, seams):
    clients, ada_id = make_client_dict(admin, ada, bob)
    return lambda: ReferenceFaults(clients, ada_id, seams)


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_reference_fault_walk(make_world, family, seed):
    faults.run_fault_walk(make_world, ReferenceFaults, GRAPHS["reference"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(ReferenceFaults, "reference"))
def test_reference_refusal_seam_axis(make_world, edge_id, shape):
    run_axis_cell(make_world, "reference", edge_id, shape)


def test_N17_obs93_memory_store_delete_errors_after_its_row_is_gone(make_world):
    """N17-obs93 (fixed in cn/r17-n): the memory store delete commits the ledger row's
    deletion, then the S3 purge raises the recorded AccessDenied — the member now gets 200
    with `cleanup.s3 = skipped` and a redacted warning, and the OpenSearch purge after it still
    runs (`_faulted_memory_delete` checks the body, the 404 and the seam-text oracle)."""
    world = make_world()
    graph = GRAPHS["reference"]
    world.step(graph.edge("agent_delete_releases_kb"))
    shape = faults.with_at(faults.SHAPES["s3_access_denied"], "list_objects_v2")
    world.fault_step(graph.edge("memory_delete_after_agent_gone"), shape,
                     expected_dst="agent_gone_memory_gone")
