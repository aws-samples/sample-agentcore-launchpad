"""Shared plumbing of the per-graph fault-walk test files (round 6, r17-l): the refusal × seam-
faulted axis parameters and its driver. One test file per graph (the world modules each export a
fixture named ``seams``, so the graphs cannot share one module)."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

from tests.graph import faults
from tests.graph.executor import GRAPHS
from tests.graph.paths import shortest_prefix

#: the refusal axis runs three families in verify (one per surface class: a 5xx, a transport
#: timeout, an AccessDenied), every family under LAUNCHPAD_SOAK=1
AXIS_VERIFY_FAMILIES = ("5xx", "timeout", "access_denied")


def axis_params(world_cls: type, graph_name: str) -> list[Any]:
    """One cell per (refusal edge reachable from the initial node, family): the family's first
    shape the world can serve at that edge (the seam must never be reached, so one shape per
    family keeps the grid honest and bounded)."""
    graph = GRAPHS[graph_name]
    families = faults.FAMILIES if os.environ.get("LAUNCHPAD_SOAK") == "1" else AXIS_VERIFY_FAMILIES
    out = []
    for edge in sorted(graph.edges, key=lambda e: e.id):
        if not edge.is_refusal or edge.id in world_cls.XFAIL:
            continue
        if shortest_prefix(graph, edge.src) is None:
            continue
        for fam in families:
            options = world_cls.fault_shapes(edge, fam)
            if options:
                out.append(pytest.param(edge.id, options[0],
                                        id=f"{edge.id}-{fam}-{options[0].label}"))
    return out


def run_axis_cell(make_world: Callable[[], Any], graph_name: str, edge_id: str,
                  shape: faults.Shape) -> None:
    """Bring a fresh world to the refusal's source node through the BFS prefix, then attempt the
    refusal with the shape armed on every seam of the world (``FaultMixin.fault_step`` → the
    world's refusal handler: seam untouched, nothing written, the contract's answer)."""
    graph = GRAPHS[graph_name]
    edge = graph.edge(edge_id)
    world = make_world()
    try:
        for step in shortest_prefix(graph, edge.src) or ():
            world.step(step)
        try:
            world.fault_step(edge, shape, expected_dst=edge.dst)
        except AssertionError as exc:
            known = faults.known_symptom(exc, edge_id)  # a registered symptom xfails as in a walk
            if known:
                pytest.xfail(f"{known} — {edge_id}@{shape.label}: {str(exc)[:200]}")
            raise
    finally:
        world.close()
