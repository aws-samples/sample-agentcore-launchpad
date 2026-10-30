"""Bug-neighbourhood re-exploration (round 17, loop engine, r17-b — loop (a) of testing.md
§6.7, formalised).

A **pinned edge** is an edge a register row touches: the YAML's ``register:`` ids (contract
gaps), the world's ``XFAIL`` table (a reproduced finding, strict xfail) and the world's
``FIXED`` table (a finding whose fix landed — the edge stays in the set WITHOUT the xfail
mark, so a flipped pin never makes its neighbourhood vanish). Around every pinned edge ``p``
this module generates the paths a fix must immediately re-run:

* **pairs** — every edge pair ``(a, b)`` (``a.dst == b.src``) whose two edges both lie within
  **line-graph distance 2** of ``p`` (edges are adjacent when one ends where the other starts;
  the distance is the hop count over that adjacency, both directions);
* **windows** — every connected 3-edge sequence that contains ``p`` (``a>p>b``, ``p>a>b``,
  ``a>b>p``): the history one step longer than an edge pair, which the enumerated paths
  cover only by accident;
* **primes** — every prime path that contains ``p``.

Each requirement becomes a test path (BFS shortest prefix from the initial node, as in
``paths.walk_paths``) with its own test id ``<graph>/<register>/<kind><n>``, pinned
``xfail(strict=True)`` when it crosses an ``XFAIL`` edge — so the moment a fix flips the pin,
the whole neighbourhood runs green or fails loudly. ``report`` says which neighbourhood paths
the prime paths / edge pairs did NOT already contain as a contiguous sub-sequence (the
"newly covered" column of the loop-engine table in ``graph-coverage.md``).
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any

import pytest

from tests.graph.paths import Edge, Graph, TestPath, prime_paths, shortest_prefix, walk_paths

RADIUS = 2
REGISTER_ID_RE = re.compile(r"\bN\d+-obs\d+[a-z]?\b")


def pinned_edges(graph: Graph, world_cls: type | None = None) -> dict[str, list[str]]:
    """``register id → sorted edge ids`` from the YAML gap rows, the world's XFAIL table and
    the world's FIXED table (fixed findings keep their neighbourhood)."""
    out: dict[str, set[str]] = {}
    for e in graph.edges:
        if e.register:
            out.setdefault(e.register, set()).add(e.id)
    for edge_id, reason in (getattr(world_cls, "XFAIL", None) or {}).items():
        m = REGISTER_ID_RE.search(reason)
        out.setdefault(m.group(0) if m else f"xfail:{edge_id}", set()).add(edge_id)
    for rid, edge_ids in (getattr(world_cls, "FIXED", None) or {}).items():
        for edge_id in edge_ids:
            assert any(e.id == edge_id for e in graph.edges), (graph.name, rid, edge_id)
            out.setdefault(rid, set()).add(edge_id)
    return {rid: sorted(v) for rid, v in sorted(out.items())}


def line_distances(graph: Graph, edge_id: str) -> dict[str, int]:
    """Hop distance from ``edge_id`` to every edge over the undirected line graph."""
    by_src: dict[str, list[Edge]] = {}
    by_dst: dict[str, list[Edge]] = {}
    for e in graph.edges:
        by_src.setdefault(e.src, []).append(e)
        by_dst.setdefault(e.dst, []).append(e)
    dist = {edge_id: 0}
    queue = deque([graph.edge(edge_id)])
    while queue:
        cur = queue.popleft()
        for nxt in (*by_src.get(cur.dst, ()), *by_dst.get(cur.src, ())):
            if nxt.id not in dist:
                dist[nxt.id] = dist[cur.id] + 1
                queue.append(nxt)
    return dist


@dataclass(frozen=True)
class Requirement:
    graph: str
    register: str
    pin: str
    kind: str  # pair | window | prime
    edges: tuple[Edge, ...]

    @property
    def label(self) -> str:
        return ">".join(e.id for e in self.edges)


def requirements(graph: Graph, pin: str, register: str, *,
                 radius: int = RADIUS) -> list[Requirement]:
    dist = line_distances(graph, pin)
    near = {eid for eid, d in dist.items() if d <= radius}
    out: list[Requirement] = []
    seen: set[str] = set()

    def add(kind: str, edges: tuple[Edge, ...]) -> None:
        req = Requirement(graph.name, register, pin, kind, edges)
        if req.label not in seen:
            seen.add(req.label)
            out.append(req)

    # most specific first: when two requirements prefix to the same test path, the prime path
    # or the window claims it and the pair (a re-run of an enumerated path) is dropped
    ordered = sorted(graph.edges, key=lambda e: e.id)
    for prime in prime_paths(graph):
        if any(e.id == pin for e in prime):
            add("prime", prime)
    p = graph.edge(pin)
    into = [e for e in ordered if e.dst == p.src]
    out_of = [e for e in ordered if e.src == p.dst]
    for a in into:
        for b in out_of:
            add("window", (a, p, b))
    for a in out_of:
        for b in ordered:
            if b.src == a.dst:
                add("window", (p, a, b))
    for b in into:
        for a in ordered:
            if a.dst == b.src:
                add("window", (a, b, p))
    for a in ordered:
        for b in ordered:
            if a.dst == b.src and a.id in near and b.id in near:
                add("pair", (a, b))
    return out


def test_paths(graph: Graph, world_cls: type | None = None) -> list[tuple[Requirement, TestPath]]:
    """Every neighbourhood requirement of every pinned edge as a prefixed test path; ids are
    ``<graph>/<register>/<kind><n>`` (n per register, in generation order)."""
    out: list[tuple[Requirement, TestPath]] = []
    for register, pins in pinned_edges(graph, world_cls).items():
        n = 0
        walked: set[str] = set()  # full prefixed paths: a prefixed pair can equal a window
        for pin in pins:
            for req in requirements(graph, pin, register):
                prefix = shortest_prefix(graph, req.edges[0].src)
                assert prefix is not None, (graph.name, req.label)
                full = ">".join(e.id for e in (*prefix, *req.edges))
                if full in walked:
                    continue
                walked.add(full)
                n += 1
                tp = TestPath(f"{graph.name}/{register}", n, req.kind, (*prefix, *req.edges),
                              len(prefix))
                out.append((req, tp))
    return out


def _contains(seq: tuple[str, ...], sub: tuple[str, ...]) -> bool:
    k = len(sub)
    return any(seq[i:i + k] == sub for i in range(len(seq) - k + 1))


def previously_covered(graph: Graph, req: Requirement) -> bool:
    """Did some enumerated test path (prime paths + every edge pair, the soak set) already
    contain the requirement as a contiguous sub-sequence?"""
    want = tuple(e.id for e in req.edges)
    for tp in walk_paths(graph, include_covered_pairs=True):
        if _contains(tuple(e.id for e in tp.edges), want):
            return True
    return False


def summarize(graph: Graph, world_cls: type | None = None) -> dict[str, Any]:
    pairs = test_paths(graph, world_cls)
    new = [req for req, _tp in pairs if not previously_covered(graph, req)]
    return {
        "graph": graph.name,
        "pins": pinned_edges(graph, world_cls),
        "paths": len(pairs),
        "by_kind": {k: sum(1 for r, _ in pairs if r.kind == k)
                    for k in ("pair", "window", "prime")},
        "newly_covered": len(new),
        "newly_covered_labels": [r.label for r in new],
    }


#: rev-r17 trim (cn/r17-d): in verify only the WINDOWS run — the prime paths through a
#: pinned / FIXED edge are already every prime shard's own tests (100 % redundant here), and
#: the distance-2 pairs are re-runs of enumerated paths; both run under LAUNCHPAD_SOAK=1.
VERIFY_KINDS = ("window",)
SOAK_KINDS = ("prime", "pair")


def params_for(world_cls: type, graphs: dict[str, Graph],
               kinds: tuple[str, ...] = VERIFY_KINDS) -> list[Any]:
    """``pytest.param`` list mirroring ``executor.params_for``: skip on undriven edges, strict
    xfail through a pinned finding; empty when the graph has no pinned edge. ``kinds`` selects
    the requirement kinds: the windows in verify, the prime paths and the distance-2 pairs under
    soak (rev-r17: the canary's obs116 pairs alone were 200 re-runs per verify, its primes
    another ~240 duplicates of the prime shards)."""
    graph = graphs[world_cls.graph]
    driven = {name[5:] for name in dir(world_cls) if name.startswith("edge_")}
    out = []
    for req, tp in test_paths(graph, world_cls):
        if req.kind not in kinds:
            continue
        marks: list[Any] = []
        missing = [e.id for e in tp.edges if e.id not in driven]
        if missing:
            marks.append(pytest.mark.skip(reason=f"undriven edge(s) {missing}"))
        else:
            pinned = [e.id for e in tp.edges if e.id in world_cls.XFAIL]
            if pinned:
                marks.append(pytest.mark.xfail(
                    strict=True, reason="; ".join(world_cls.XFAIL[p] for p in pinned)))
        out.append(pytest.param(tp, id=f"{tp.id}[{tp.label}]", marks=marks))
    return out
