# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the AGENT graph (round 6, r17-l; testing.md §6.9): the deploy
stages, the Runtime delete and the chat producer serve the recorded seam shapes mid-walk."""

from __future__ import annotations

import pytest

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import AgentFaults, make_client_dict
from tests.graph.test_graph_agent import seams  # noqa: F401 — fixture by name
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name


@pytest.fixture
def make_world(admin, ada, bob, seams):
    clients, _ada_id = make_client_dict(admin, ada, bob)
    return lambda: AgentFaults(clients, seams)


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_agent_fault_walk(make_world, family, seed):
    """A seeded walk with 1–3 recorded shapes served at the deploy stages / the Runtime delete /
    the chat producer; the executor's oracles plus the fault layer's (no half-written row, the
    envelope, the terminal state, the resume / reattach path) after every step."""
    faults.run_fault_walk(make_world, AgentFaults, GRAPHS["agent"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(AgentFaults, "agent"))
def test_agent_refusal_seam_axis(make_world, edge_id, shape):
    """The refusal × seam-faulted axis: every refusal of the agent graph attempted with the shape
    armed on the delete and chat seams — decided on the ledger, the seam never reached."""
    run_axis_cell(make_world, "agent", edge_id, shape)


def test_N17_obs94_transport_timeout_on_the_agent_delete_is_a_bare_500(make_world):
    """N17-obs94 (fixed, cn/r17-o): a botocore ReadTimeoutError on DeleteAgentRuntime used to
    escape the error handlers as a bare 500; it is 504 `aws.timeout {error_type}` now, the row
    kept (api.md "Transport failures")."""
    world = make_world()
    graph = GRAPHS["agent"]
    world.step(graph.edge("create"))
    world.step(graph.edge("deploy_succeeds"))
    world.fault_step(
        graph.edge("delete_active"), faults.SHAPES["rt_read_timeout"], expected_dst="active"
    )


def test_N17_obs100_cold_republish_failure_after_a_deploy_stage_failure_stays_failed(make_world):
    """N17-obs100 (fixed in cn/r17-p): after a failure INSIDE the `deploy` stage the agent is
    `failed` (agents.md §4.3: UpdateAgentRuntime may have been accepted — the live state is
    unknown); a cold re-publish that then fails BEFORE `deploy` used to flip it back to
    `active` because `_keeps_live_version` found the older succeeded deployment. §4.2 / §4.3
    now say the agent has no verified live version until a later publish succeeds — the
    graph's `cold_redeploy_fails` (`failed`) holds."""
    world = make_world()
    graph = GRAPHS["agent"]
    world.step(graph.edge("create"))
    world.step(graph.edge("deploy_succeeds"))
    world.step(graph.edge("redeploy"))
    world.fault_step(
        graph.edge("redeploy_succeeds"),
        faults.with_at(faults.SHAPES["rt_conflict_modifying"], "deploy"),
        expected_dst="failed",
    )
    world.step(graph.edge("redeploy_from_failed"))
    world.step(graph.edge("cold_redeploy_fails"))
