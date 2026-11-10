# ruff: noqa: F811  — pytest fixtures are imported by name from the canary world module
"""E13 — the second shard of the Runtime-canary graph's prime paths (round 17, r17-b; per-file
budget after the ramp unroll, rev-r17). The world and its fixtures are the canary module's."""

from __future__ import annotations

import pytest

from tests.graph.executor import params_for
from tests.graph.test_graph_runtime_canary import (  # noqa: F401 — fixtures by name
    CanaryWorld,
    make_world,
    seams,
    world,
)
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name


@pytest.mark.parametrize("path", params_for(CanaryWorld, requirements=("prime",), shard=(1, 2)))
def test_runtime_canary_path_b(world, path):
    world.run(path)
