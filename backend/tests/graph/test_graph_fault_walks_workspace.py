# ruff: noqa: F811  — pytest fixtures are imported by name from the world module
"""Fault-injected walks over the WORKSPACE graph (r17-ws; testing.md §6.9 / §6.10): the
bootstrap stages' AWS seams serve the recorded / botocore shapes mid-walk, plus the per-stage
injection matrix the lead asked for — AccessDenied, AssumeRole failure, region-unsupported,
interruption mid-stage (and a 5xx) at EVERY stage with a seam, deterministic and in verify."""

from __future__ import annotations

import pytest

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import make_client_dict
from tests.graph.fault_seams_workspace import STAGE_OF_EDGE, WorkspaceFaults
from tests.graph.paths import shortest_prefix
from tests.graph.test_graph_workspace import seams  # noqa: F401 — fixture by name
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

#: the lead's four injections per stage (+ the documented 5xx), on one canonical edge per stage
MATRIX_FAMILIES = ("access_denied", "region_unsupported", "interrupted", "5xx")
MATRIX_EDGES = (
    "validate_only", "run_validate", "run_iam", "run_model_credential", "run_storage",
    "run_codebuild", "run_gateway", "run_registry", "run_skill_lab", "run_observability",
    "run_policy",
)


@pytest.fixture
def make_world(admin, ada, bob, seams):
    clients, _ada_id = make_client_dict(admin, ada, bob)
    return lambda: WorkspaceFaults(clients, seams)


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_workspace_fault_walk(make_world, family, seed):
    """A seeded walk with 1–3 shapes served at the stages' seams; the executor's oracles plus
    the fault layer's (stage status persisted, no half-recorded ids, the job resumable, no
    secret in the error text) after every step."""
    faults.run_fault_walk(make_world, WorkspaceFaults, GRAPHS["workspace"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(WorkspaceFaults, "workspace"))
def test_workspace_refusal_seam_axis(make_world, edge_id, shape):
    """Every refusal of the workspace graph attempted with the shape armed on EVERY fake
    method — decided on the ledger, no AWS call made."""
    run_axis_cell(make_world, "workspace", edge_id, shape)


def _matrix_params() -> list:
    graph = GRAPHS["workspace"]
    out = []
    for edge_id in MATRIX_EDGES:
        edge = graph.edge(edge_id)
        for family in MATRIX_FAMILIES:
            for shape in WorkspaceFaults.fault_shapes(edge, family):
                out.append(pytest.param(edge_id, shape,
                                        id=f"{STAGE_OF_EDGE[edge_id]}-{family}-{shape.label}"))
    return out


@pytest.mark.parametrize(("edge_id", "shape"), _matrix_params())
def test_workspace_stage_injection(make_world, edge_id, shape):
    """One cell per (stage, family, seam): bring a fresh world to the edge's source node
    through the BFS prefix, serve the shape at the stage's seam, judge the outcome
    (``WorkspaceFaults.fault``) and prove the recovery reaches the planned node."""
    graph = GRAPHS["workspace"]
    edge = graph.edge(edge_id)
    world = make_world()
    try:
        for step in shortest_prefix(graph, edge.src) or ():
            world.step(step)
        try:
            world.fault_step(edge, shape, expected_dst=edge.dst)
        except AssertionError as exc:
            known = faults.known_symptom(exc)
            if known:
                pytest.xfail(f"{known} — {edge_id}@{shape.label}: {str(exc)[:200]}")
            raise
    finally:
        world.close()


# ---------------------------------------------------------------------------
# the r17-ws findings, flipped (the former strict pins drive the minimal sequence positively)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edge_id", ["run_validate", "validate_only"])
def test_a_transport_timeout_at_a_probe_is_a_transient_verdict_not_a_region_one(make_world,
                                                                                 edge_id):
    """N17-obs132 (fixed cn/r17-v): the minimal sequence `register>run_validate@rt_read_timeout/
    ecr.describe_repositories` — the probe is retried once, the stage fails with the service,
    the operation and the redacted cause and says "transient — retry"; the region sentence is
    reserved for the missing-endpoint codes (the world's `_expected_detail_fragments`)."""
    world = make_world()
    graph = GRAPHS["workspace"]
    try:
        world.step(graph.edge("register"))
        shape = faults.with_at(faults.SHAPES["rt_read_timeout"], "ecr.describe_repositories")
        edge = graph.edge(edge_id)
        outcome = world.fault_step(edge, shape, expected_dst=edge.dst)  # the oracles judge
        assert outcome.dst == edge.dst
        # the one retry: the armed seam saw the ECR describe exactly twice (the healed run
        # after the recovery is not counted — the seam records armed calls only)
        assert world.seam_calls.count("ecr.describe_repositories") == 2, world.seam_calls
    finally:
        world.close()


@pytest.mark.parametrize("edge_id", ["run_validate", "validate_only"])
def test_an_unreachable_endpoint_at_a_probe_is_a_network_verdict_not_a_region_one(make_world,
                                                                                   edge_id):
    """N17-obs200 (fixed cn/r17-w): `ws_endpoint_unreachable` (botocore `EndpointConnectionError`
    — the endpoint resolved, the connect failed) served at `ecr.describe_repositories` — the probe
    is retried once, the stage fails with "unreachable from this host … network/DNS, not the
    region" (the world's `_expected_detail_fragments` for the `endpoint` kind), never the region
    sentence and never the plain transient one; the recovery reaches the planned node."""
    world = make_world()
    graph = GRAPHS["workspace"]
    try:
        world.step(graph.edge("register"))
        shape = faults.with_at(faults.SHAPES["ws_endpoint_unreachable"],
                               "ecr.describe_repositories")
        assert shape.family == "timeout"  # not the region family any more
        edge = graph.edge(edge_id)
        outcome = world.fault_step(edge, shape, expected_dst=edge.dst)
        assert outcome.dst == edge.dst
        assert world.seam_calls.count("ecr.describe_repositories") == 2, world.seam_calls
    finally:
        world.close()
