# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the RUNTIME CANARY graph (round 7, r17-m; testing.md §6.9): the
cleanup's AWS seams — the endpoint delete, the live-version read, the candidate-zip delete and
the endpoints-gone wait — serve the recorded shapes mid-walk."""

from __future__ import annotations

import uuid

import pytest

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import CanaryFaults, make_client_dict
from tests.model.test_canary_state_machine import seams  # noqa: F401 — fixture by name
from tests.test_visibility import _dataset, ada, admin, app, bob  # noqa: F401 — fixtures by name


@pytest.fixture
def make_world(admin, ada, bob, seams):
    clients, ada_id = make_client_dict(admin, ada, bob)
    return lambda: CanaryFaults(clients, ada_id, seams,
                                _dataset(None, name=f"g-fcanary-ds-{uuid.uuid4().hex[:6]}"))


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_runtime_canary_fault_walk(make_world, family, seed):
    faults.run_fault_walk(make_world, CanaryFaults, GRAPHS["runtime_canary"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(CanaryFaults, "runtime_canary"))
def test_runtime_canary_refusal_seam_axis(make_world, edge_id, shape):
    run_axis_cell(make_world, "runtime_canary", edge_id, shape)
