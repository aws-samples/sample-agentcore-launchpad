"""Lifecycle graphs → test paths (round 16, channel A, r16-o).

The YAML files under ``tests/graph/graphs/`` describe one lifecycle graph per entity: nodes
are the states a row can be in, edges are the actions a client (or the platform) takes, and
every edge cites the contract sentence that names its outcome. This module turns a graph
into deterministic **test requirements**:

* **prime paths** — every maximal simple path (no node repeats except a closing cycle); a
  refusal self-loop is a prime path of its own ("reach the state, attempt the refused
  action");
* **edge pairs** — every adjacent pair ``(e1, e2)`` with ``e1.dst == e2.src``, the
  neighbourhood walk the bug loop replays around a register row.

A **test path** is a requirement prefixed with the shortest route from the graph's initial
node (BFS over edges in id order, so the output never depends on dict ordering). The
executor (``test_graph_paths.py``) walks every test path through the real routes.

Run ``uv run python -m tests.graph.paths`` for the per-graph numbers and the contract-gap
rows; ``--hits <json>`` merges a recorded coverage file into the report.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GRAPH_DIR = Path(__file__).resolve().parent / "graphs"


@dataclass(frozen=True)
class Edge:
    id: str
    src: str
    dst: str
    kind: str = "transition"  # transition | refusal
    via: str = ""
    expect: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    cites: tuple[str, ...] = ()
    gap: str | None = None
    register: str | None = None
    ui: dict[str, Any] | None = field(default=None, hash=False, compare=False)

    @property
    def is_refusal(self) -> bool:
        return self.kind == "refusal"

    @property
    def is_loop(self) -> bool:
        return self.src == self.dst


@dataclass
class Graph:
    name: str
    module: str
    entity: str
    initial: str
    contracts: dict[str, dict[str, str]]
    nodes: dict[str, dict[str, Any]]
    edges: list[Edge]
    path: Path

    @property
    def terminal(self) -> set[str]:
        return {n for n, meta in self.nodes.items() if meta.get("terminal")}

    def edge(self, edge_id: str) -> Edge:
        for e in self.edges:
            if e.id == edge_id:
                return e
        raise KeyError(edge_id)

    def out_edges(self, node: str) -> list[Edge]:
        return [e for e in self.edges if e.src == node]

    def in_edges(self, node: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == node]

    @property
    def gaps(self) -> list[Edge]:
        return [e for e in self.edges if e.gap]


@dataclass(frozen=True)
class TestPath:
    graph: str
    index: int
    requirement: str  # prime | edge-pair
    edges: tuple[Edge, ...]
    prefix_len: int  # how many leading edges are the BFS prefix (not part of the requirement)

    @property
    def id(self) -> str:
        return f"{self.graph}/{self.requirement[0].upper()}{self.index:02d}"

    @property
    def label(self) -> str:
        return ">".join(e.id for e in self.edges)

    @property
    def requirement_edges(self) -> tuple[Edge, ...]:
        return self.edges[self.prefix_len:]

    @property
    def pairs(self) -> set[tuple[str, str]]:
        return {(a.id, b.id) for a, b in zip(self.edges, self.edges[1:], strict=False)}


# ---------------------------------------------------------------------------
# loading / validation
# ---------------------------------------------------------------------------


def load_graph(path: Path) -> Graph:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    nodes = {n["id"]: {k: v for k, v in n.items() if k != "id"} for n in raw["nodes"]}
    edges = [
        Edge(
            id=e["id"], src=e["from"], dst=e["to"], kind=e.get("kind", "transition"),
            via=e.get("via", ""), expect=dict(e.get("expect") or {}),
            cites=tuple(e.get("cites") or ()), gap=e.get("gap"), register=e.get("register"),
            ui=e.get("ui"),
        )
        for e in raw["edges"]
    ]
    graph = Graph(
        name=raw["graph"], module=str(raw.get("module", "")), entity=raw.get("entity", ""),
        initial=raw["initial"], contracts=dict(raw.get("contracts") or {}), nodes=nodes,
        edges=edges, path=path,
    )
    for problem in validate(graph):
        raise ValueError(f"{path.name}: {problem}")
    return graph


def validate(graph: Graph) -> list[str]:
    """Structural rules every graph file must satisfy (asserted by walk_paths.py too)."""
    problems: list[str] = []
    if graph.initial not in graph.nodes:
        problems.append(f"initial node {graph.initial!r} is not declared")
    seen: set[str] = set()
    for e in graph.edges:
        if e.id in seen:
            problems.append(f"duplicate edge id {e.id!r}")
        seen.add(e.id)
        for node in (e.src, e.dst):
            if node not in graph.nodes:
                problems.append(f"edge {e.id!r} names undeclared node {node!r}")
        if e.kind not in ("transition", "refusal"):
            problems.append(f"edge {e.id!r} has unknown kind {e.kind!r}")
        if e.is_refusal and not e.is_loop:
            problems.append(f"refusal edge {e.id!r} must be a self-loop ({e.src} → {e.dst})")
        if not e.cites and not e.gap:
            problems.append(f"edge {e.id!r} cites no contract and declares no gap")
        for ref in e.cites:
            if ref not in graph.contracts:
                problems.append(f"edge {e.id!r} cites unknown contract {ref!r}")
        if e.gap and not e.register:
            problems.append(f"gap edge {e.id!r} has no register id")
        if e.register and not e.gap:
            problems.append(f"edge {e.id!r} names a register id without a gap text")
    for ref, meta in graph.contracts.items():
        for key in ("doc", "section", "quote"):
            if not meta.get(key):
                problems.append(f"contract {ref!r} lacks {key!r}")
    reachable = _reachable(graph)
    for node in graph.nodes:
        if node not in reachable:
            problems.append(f"node {node!r} is unreachable from {graph.initial!r}")
    return problems


def _reachable(graph: Graph) -> set[str]:
    seen = {graph.initial}
    queue = deque([graph.initial])
    while queue:
        node = queue.popleft()
        for e in graph.out_edges(node):
            if e.dst not in seen:
                seen.add(e.dst)
                queue.append(e.dst)
    return seen


def load_all(directory: Path = GRAPH_DIR) -> dict[str, Graph]:
    graphs = [load_graph(p) for p in sorted(directory.glob("*.yaml"))]
    return {g.name: g for g in graphs}


# ---------------------------------------------------------------------------
# requirements
# ---------------------------------------------------------------------------


def _nodes_of(path: tuple[Edge, ...]) -> list[str]:
    return [path[0].src, *(e.dst for e in path)]


def is_simple(path: tuple[Edge, ...]) -> bool:
    """No node repeats, except that the last node may equal the first (a closing cycle)."""
    nodes = _nodes_of(path)
    inner = nodes[:-1]
    if len(set(inner)) != len(inner):
        return False
    return nodes[-1] not in inner[1:]


def _closed(path: tuple[Edge, ...]) -> bool:
    nodes = _nodes_of(path)
    return nodes[0] == nodes[-1]


def _forward_extensions(graph: Graph, path: tuple[Edge, ...]) -> list[tuple[Edge, ...]]:
    if _closed(path):
        return []
    nodes = _nodes_of(path)
    out = []
    for e in sorted(graph.out_edges(nodes[-1]), key=lambda x: x.id):
        if e.dst not in nodes or e.dst == nodes[0]:
            out.append((*path, e))
    return out


def _backward_extendable(graph: Graph, path: tuple[Edge, ...]) -> bool:
    if _closed(path):
        return False
    nodes = _nodes_of(path)
    return any(
        e.src not in nodes or e.src == nodes[-1] for e in graph.in_edges(nodes[0])
    )


def simple_paths(graph: Graph) -> list[tuple[Edge, ...]]:
    """Every simple edge path, by DFS from each edge (deterministic: edges in id order)."""
    found: list[tuple[Edge, ...]] = []
    stack = [(e,) for e in sorted(graph.edges, key=lambda x: x.id)]
    while stack:
        path = stack.pop()
        found.append(path)
        stack.extend(reversed(_forward_extensions(graph, path)))
    return found


def prime_paths(graph: Graph) -> list[tuple[Edge, ...]]:
    """Maximal simple paths — not extendable at either end — longest first, then by ids."""
    primes = [
        p for p in simple_paths(graph)
        if not _forward_extensions(graph, p) and not _backward_extendable(graph, p)
    ]
    return sorted(primes, key=lambda p: (-len(p), tuple(e.id for e in p)))


def edge_pairs(graph: Graph) -> list[tuple[Edge, Edge]]:
    pairs = [
        (a, b) for a in graph.edges for b in graph.edges if a.dst == b.src
    ]
    return sorted(pairs, key=lambda ab: (ab[0].id, ab[1].id))


def shortest_prefix(graph: Graph, node: str) -> tuple[Edge, ...] | None:
    """BFS from the initial node over edges in id order; ``()`` for the initial node itself."""
    if node == graph.initial:
        return ()
    parent: dict[str, tuple[str, Edge]] = {}
    seen = {graph.initial}
    queue = deque([graph.initial])
    while queue:
        cur = queue.popleft()
        for e in sorted(graph.out_edges(cur), key=lambda x: x.id):
            if e.dst in seen or e.is_loop:
                continue
            seen.add(e.dst)
            parent[e.dst] = (cur, e)
            if e.dst == node:
                out: list[Edge] = []
                walk = node
                while walk != graph.initial:
                    prev, edge = parent[walk]
                    out.append(edge)
                    walk = prev
                return tuple(reversed(out))
            queue.append(e.dst)
    return None


def walk_paths(graph: Graph, *, include_covered_pairs: bool = False) -> list[TestPath]:
    """Prime-path requirements first (P01…), then the edge pairs no prime test path already
    contains as a contiguous pair (E01…). ``include_covered_pairs`` keeps every pair (soak)."""
    out: list[TestPath] = []
    covered_pairs: set[tuple[str, str]] = set()
    for i, prime in enumerate(prime_paths(graph), start=1):
        prefix = shortest_prefix(graph, prime[0].src)
        assert prefix is not None, (graph.name, prime[0].id)
        tp = TestPath(graph.name, i, "prime", (*prefix, *prime), len(prefix))
        covered_pairs |= tp.pairs
        out.append(tp)
    j = 0
    for a, b in edge_pairs(graph):
        if (a.id, b.id) in covered_pairs and not include_covered_pairs:
            continue
        prefix = shortest_prefix(graph, a.src)
        assert prefix is not None, (graph.name, a.id)
        j += 1
        out.append(TestPath(graph.name, j, "edge-pair", (*prefix, a, b), len(prefix)))
    return out


# ---------------------------------------------------------------------------
# coverage report
# ---------------------------------------------------------------------------


def summarize(graph: Graph, hits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Counts for one graph; ``hits`` = ``{"edges": [...], "paths": [...]}`` recorded by a run."""
    hits = hits or {}
    hit_edges = set(hits.get("edges") or ())
    hit_paths = set(hits.get("paths") or ())
    primes = prime_paths(graph)
    paths = walk_paths(graph)
    prime_ids = [tp.id for tp in paths if tp.requirement == "prime"]
    pair_ids = [tp.id for tp in paths if tp.requirement == "edge-pair"]
    pairs = edge_pairs(graph)
    hit_pairs = {
        pair for tp in paths if tp.id in hit_paths for pair in tp.pairs
    }
    return {
        "graph": graph.name,
        "module": graph.module,
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "edges_covered": len([e for e in graph.edges if e.id in hit_edges]),
        "edges_uncovered": sorted(e.id for e in graph.edges if e.id not in hit_edges),
        "prime_paths": len(primes),
        "prime_paths_covered": len([p for p in prime_ids if p in hit_paths]),
        "prime_paths_uncovered": [
            tp.label for tp in paths if tp.requirement == "prime" and tp.id not in hit_paths
        ],
        "edge_pairs": len(pairs),
        "edge_pairs_covered": len([(a.id, b.id) for a, b in pairs if (a.id, b.id) in hit_pairs]),
        "pair_paths": len(pair_ids),
        "gaps": [
            {"edge": e.id, "register": e.register, "gap": " ".join(e.gap.split())}
            for e in graph.gaps
        ],
    }


def render_report(graphs: dict[str, Graph], hits: dict[str, dict[str, Any]], *,
                  title: str = "Lifecycle graph coverage") -> str:
    rows = [summarize(g, hits.get(name)) for name, g in sorted(graphs.items())]
    total = {
        k: sum(r[k] for r in rows)
        for k in ("nodes", "edges", "edges_covered", "prime_paths", "prime_paths_covered",
                  "edge_pairs", "edge_pairs_covered")
    }
    lines = [
        f"# {title}",
        "",
        "Generated by `make graph-coverage` (`backend/tests/graph`, round 16 r16-o). Edges and",
        "prime paths of every lifecycle graph, and how many the hermetic executor walked in the",
        "recording run. A gap row is an edge the contract does not name (a register finding).",
        "",
        "| graph | module | nodes | edges | edges covered | prime paths | prime paths covered "
        "| edge pairs | pairs covered | gaps |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['graph']}` | {r['module']} | {r['nodes']} | {r['edges']} | "
            f"{r['edges_covered']} | "
            f"{r['prime_paths']} | {r['prime_paths_covered']} | {r['edge_pairs']} | "
            f"{r['edge_pairs_covered']} | {len(r['gaps'])} |"
        )
    lines.append(
        f"| **total** | — | {total['nodes']} | {total['edges']} | {total['edges_covered']} | "
        f"{total['prime_paths']} | {total['prime_paths_covered']} | {total['edge_pairs']} | "
        f"{total['edge_pairs_covered']} | {sum(len(r['gaps']) for r in rows)} |"
    )
    lines.append("")
    for r in rows:
        if r["edges_uncovered"] or r["prime_paths_uncovered"]:
            lines.append(f"**`{r['graph']}` uncovered** — edges: "
                         f"{', '.join(f'`{e}`' for e in r['edges_uncovered']) or 'none'}; "
                         f"prime paths: {len(r['prime_paths_uncovered'])}")
            for label in r["prime_paths_uncovered"]:
                lines.append(f"- `{label}`")
            lines.append("")
    gaps = [(r["graph"], g) for r in rows for g in r["gaps"]]
    if gaps:
        lines.append("## Contract gaps (edges no contract sentence names)")
        lines.append("")
        lines.append("| graph | edge | register | gap |")
        lines.append("|---|---|---|---|")
        for name, g in gaps:
            lines.append(f"| `{name}` | `{g['edge']}` | {g['register']} | {g['gap']} |")
        lines.append("")
    return "\n".join(lines)


def merge_hits(shards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, set[str]]] = {}
    for shard in shards:
        for name, rec in shard.items():
            slot = merged.setdefault(name, {"edges": set(), "paths": set()})
            slot["edges"] |= set(rec.get("edges") or ())
            slot["paths"] |= set(rec.get("paths") or ())
    return {n: {"edges": sorted(v["edges"]), "paths": sorted(v["paths"])}
            for n, v in merged.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hits", help="JSON produced by the executor's --graph-report run")
    ap.add_argument("--paths", action="store_true", help="list every test path")
    args = ap.parse_args(argv)
    graphs = load_all()
    hits: dict[str, Any] = {}
    if args.hits:
        hits = json.loads(Path(args.hits).read_text(encoding="utf-8"))
    print(render_report(graphs, hits))
    if args.paths:
        for name, g in sorted(graphs.items()):
            print(f"\n## {name}")
            for tp in walk_paths(g):
                print(f"{tp.id}  {tp.label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
