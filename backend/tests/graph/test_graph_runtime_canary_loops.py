# ruff: noqa: F811  — pytest fixtures are imported by name from the canary world module
"""E13 — the loop-engine tests of the Runtime-canary graph (round 17, r17-b): random walks and
the N16-obs116 neighbourhood, split from ``test_graph_runtime_canary.py`` so each file stays
under the 60 s budget (the unrolled ramp made the canary graph the largest: 41 edges, 233
prime paths; rev-r17). The world and its fixtures are the canary module's.
"""

from __future__ import annotations

import os

import pytest

from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, params_for
from tests.graph.test_graph_runtime_canary import (  # noqa: F401 — fixtures by name
    CanaryWorld,
    make_world,
    seams,
    world,
)
from tests.graph.walks import run_walk, walk_seeds
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"


@pytest.mark.parametrize("path", params_for(CanaryWorld, requirements=("edge-pair",)))
def test_runtime_canary_pair_path(world, path):
    """The edge pairs no prime path contains (the primes stay in the canary module)."""
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_runtime_canary_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, CanaryWorld, GRAPHS["runtime_canary"], seed)

@pytest.mark.parametrize("path", neighbourhood.params_for(CanaryWorld, GRAPHS))
def test_runtime_canary_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(CanaryWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_runtime_canary_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    world.run(path)
