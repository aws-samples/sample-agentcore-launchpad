"""Unit tests of the path generator (``tests/graph/paths.py``) and the graph files.

The generator is checked on hand-built graphs with known prime paths (the textbook shapes:
a chain, a diamond, a cycle, self-loops, a multi-edge) and then on every shipped YAML graph
for the structural properties that make the executor's numbers trustworthy: determinism,
simple paths only, every edge and every edge pair inside at least one test path, prefixes
that start at the initial node and connect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.graph.paths import (
    GRAPH_DIR,
    Edge,
    Graph,
    edge_pairs,
    is_simple,
    load_all,
    load_graph,
    prime_paths,
    render_report,
    shortest_prefix,
    summarize,
    validate,
    walk_paths,
)


def _graph(initial: str, edges: list[tuple[str, str, str]], *, refusals: set[str] = frozenset()):
    nodes = {initial}
    for _eid, src, dst in edges:
        nodes |= {src, dst}
    return Graph(
        name="t", module="E00", entity="test", initial=initial, contracts={"C": {
            "doc": "d", "section": "s", "quote": "q"}},
        nodes={n: {} for n in nodes},
        edges=[Edge(id=e, src=s, dst=d, kind="refusal" if e in refusals else "transition",
                    cites=("C",)) for e, s, d in edges],
        path=Path("t.yaml"),
    )


def _labels(paths) -> list[str]:
    return [">".join(e.id for e in p) for p in paths]


# ---------------------------------------------------------------------------
# prime paths on textbook shapes
# ---------------------------------------------------------------------------


def test_chain_has_one_prime_path():
    g = _graph("a", [("ab", "a", "b"), ("bc", "b", "c")])
    assert _labels(prime_paths(g)) == ["ab>bc"]


def test_diamond_has_two_prime_paths_sorted_by_edge_ids():
    g = _graph("a", [("ab", "a", "b"), ("ac", "a", "c"), ("bd", "b", "d"), ("cd", "c", "d")])
    assert _labels(prime_paths(g)) == ["ab>bd", "ac>cd"]


def test_cycle_prime_paths_close_the_loop_once_per_start_node():
    g = _graph("a", [("ab", "a", "b"), ("bc", "b", "c"), ("ca", "c", "a")])
    labels = _labels(prime_paths(g))
    # every rotation of the cycle is a maximal simple path (first == last); nothing else is
    assert labels == ["ab>bc>ca", "bc>ca>ab", "ca>ab>bc"]
    for p in prime_paths(g):
        assert is_simple(p)


def test_self_loops_are_their_own_prime_paths():
    g = _graph("a", [("ab", "a", "b"), ("loop", "b", "b"), ("bc", "b", "c")], refusals={"loop"})
    assert _labels(prime_paths(g)) == ["ab>bc", "loop"]
    # and the test path for the loop reaches b first
    tps = walk_paths(g)
    loop = next(tp for tp in tps if tp.requirement == "prime" and tp.label.endswith("loop"))
    assert loop.label == "ab>loop" and loop.prefix_len == 1


def test_multi_edges_between_two_nodes_give_distinct_paths():
    g = _graph("a", [("ab1", "a", "b"), ("ab2", "a", "b"), ("bc", "b", "c")])
    assert _labels(prime_paths(g)) == ["ab1>bc", "ab2>bc"]


def test_edge_pairs_and_their_test_paths_are_prefixed_from_the_initial_node():
    g = _graph("a", [("ab", "a", "b"), ("bc", "b", "c"), ("cb", "c", "b"), ("bb", "b", "b")],
               refusals={"bb"})
    pairs = {(x.id, y.id) for x, y in edge_pairs(g)}
    assert pairs == {("ab", "bc"), ("ab", "bb"), ("bc", "cb"), ("cb", "bc"), ("cb", "bb"),
                     ("bb", "bc"), ("bb", "bb")}
    tps = walk_paths(g)
    covered = set().union(*(tp.pairs for tp in tps))
    assert covered == pairs
    for tp in tps:
        assert tp.edges[0].src == "a"
        for x, y in zip(tp.edges, tp.edges[1:], strict=False):
            assert x.dst == y.src  # connected
    # the pairs the prime paths already contain are not generated again
    prime_pairs = set().union(*(tp.pairs for tp in tps if tp.requirement == "prime"))
    pair_only = [tp for tp in tps if tp.requirement == "edge-pair"]
    assert all(tp.pairs - prime_pairs for tp in pair_only)
    assert len(walk_paths(g, include_covered_pairs=True)) >= len(tps)


def test_shortest_prefix_is_bfs_in_id_order_and_skips_loops():
    g = _graph("a", [("ab", "a", "b"), ("aa", "a", "a"), ("bc", "b", "c"), ("ac", "a", "c")])
    assert shortest_prefix(g, "a") == ()
    assert [e.id for e in shortest_prefix(g, "c")] == ["ac"]
    assert [e.id for e in shortest_prefix(g, "b")] == ["ab"]


def test_generation_is_deterministic():
    g = load_graph(GRAPH_DIR / "agent.yaml")
    first = [(tp.id, tp.label) for tp in walk_paths(g)]
    second = [(tp.id, tp.label) for tp in walk_paths(load_graph(GRAPH_DIR / "agent.yaml"))]
    assert first == second


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_validate_names_every_structural_problem():
    g = _graph("a", [("ab", "a", "b"), ("bad", "b", "c")])
    g.edges.append(Edge(id="ab", src="a", dst="b", cites=("C",)))  # duplicate id
    g.edges.append(Edge(id="ref", src="b", dst="c", kind="refusal", cites=("C",)))  # not a loop
    g.edges.append(Edge(id="nocite", src="b", dst="c"))  # neither cites nor gap
    g.edges.append(Edge(id="gapnoid", src="b", dst="c", gap="x"))  # gap without register
    g.edges.append(Edge(id="unknown", src="b", dst="c", cites=("ZZ",)))  # unknown contract
    g.nodes["island"] = {}
    problems = "\n".join(validate(g))
    for needle in ("duplicate edge id 'ab'", "refusal edge 'ref' must be a self-loop",
                   "edge 'nocite' cites no contract", "gap edge 'gapnoid' has no register id",
                   "cites unknown contract 'ZZ'", "node 'island' is unreachable"):
        assert needle in problems, needle


# ---------------------------------------------------------------------------
# the shipped graphs
# ---------------------------------------------------------------------------

GRAPHS = load_all()


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_shipped_graph_is_structurally_sound(name):
    g = GRAPHS[name]
    assert validate(g) == []
    primes = prime_paths(g)
    assert primes and all(is_simple(p) for p in primes)
    tps = walk_paths(g)
    walked = {e.id for tp in tps for e in tp.edges}
    assert walked == {e.id for e in g.edges}, "an edge no test path walks"
    covered_pairs = set().union(*(tp.pairs for tp in tps))
    assert covered_pairs == {(a.id, b.id) for a, b in edge_pairs(g)}
    for tp in tps:
        assert tp.edges[0].src == g.initial
        assert all(x.dst == y.src for x, y in zip(tp.edges, tp.edges[1:], strict=False))
    ids = [tp.id for tp in tps]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_shipped_graph_cites_real_contract_files(name):
    g = GRAPHS[name]
    repo = GRAPH_DIR.parents[3]
    for ref, meta in g.contracts.items():
        assert (repo / meta["doc"]).is_file(), (name, ref, meta["doc"])
    for e in g.edges:
        if e.register:
            assert e.register.startswith("N16-obs"), e.id
        if e.ui is not None:
            assert set(e.ui) <= {"page", "view", "controls"} and e.ui["page"].startswith("/v2")


def test_report_counts_hits_and_lists_gaps():
    # `chat_session`'s last gap row (N16-obs88) became a `cites:` edge in cn/r17-a and the
    # N16-obs110 gaps of `agent` in cn/r16-q; the only gap rows left are N16-obs116's four
    # `runtime_canary` edges (DOC), which the report must still list
    g = GRAPHS["chat_session"]
    first = walk_paths(g)[0]
    hits = {"chat_session": {"edges": [e.id for e in first.edges], "paths": [first.id]}}
    summary = summarize(g, hits["chat_session"])
    assert summary["edges_covered"] == len({e.id for e in first.edges})
    assert summary["prime_paths_covered"] == 1
    assert summary["edge_pairs_covered"] == len(first.pairs)
    assert summary["gaps"] == []
    remaining = {
        name: {gap["register"] for gap in summarize(gr, {"edges": [], "paths": []})["gaps"]}
        for name, gr in GRAPHS.items()
    }
    assert {k: v for k, v in remaining.items() if v} == {"runtime_canary": {"N16-obs116"}}
    text = render_report(GRAPHS, hits)
    assert "| **total** |" in text and "N16-obs88" not in text and "N16-obs116" in text
    assert "`chat_session` uncovered" in text and "N16-obs110" not in text


# ---------------------------------------------------------------------------
# contract quotes are verbatim; the coverage report round-trips through the board parser
# ---------------------------------------------------------------------------

REPO_ROOT = GRAPH_DIR.parents[3]


def _alnum(text: str) -> str:
    """Normalisation for the quote check: letters and digits only, lower-cased — so markdown
    emphasis, backticks, line breaks and punctuation never decide, but every WORD must be the
    document's own, in the document's order."""
    import re

    return re.sub(r"[^0-9a-z]+", "", text.lower())


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_every_contract_quote_is_verbatim(name):
    """A quote is `…`-separated fragments; each fragment must appear in the cited document
    (a paraphrase would hide contract drift — rev-r16 on 3d109fd3)."""
    g = GRAPHS[name]
    docs: dict[str, str] = {}
    for ref, meta in g.contracts.items():
        text = docs.setdefault(meta["doc"], _alnum((REPO_ROOT / meta["doc"]).read_text("utf-8")))
        fragments = [f for f in meta["quote"].split("…") if _alnum(f)]
        assert fragments, (name, ref, "empty quote")
        for fragment in fragments:
            assert _alnum(fragment) in text, (name, ref, meta["doc"], fragment.strip()[:120])


def test_coverage_report_round_trips_through_the_board_parser(tmp_path):
    """`make graph-coverage` → docs/cn/reports/graph-coverage.md → scripts/quality_metrics.py
    `collect_graph`: every graph name (underscores included) and every number survive."""
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location(
        "quality_metrics_rt", REPO_ROOT / "scripts" / "quality_metrics.py")
    qm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qm)
    hits = {name: {"edges": [e.id for e in g.edges], "paths": [tp.id for tp in walk_paths(g)]}
            for name, g in GRAPHS.items()}
    report_dir = tmp_path / "docs" / "cn" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "graph-coverage.md").write_text(render_report(GRAPHS, hits), encoding="utf-8")
    parsed = qm.collect_graph(str(tmp_path))
    assert [r["graph"] for r in parsed["graphs"]] == sorted(GRAPHS)
    for row in parsed["graphs"]:
        expect = summarize(GRAPHS[row["graph"]], hits[row["graph"]])
        assert (row["edges"], row["edges_covered"]) == (expect["edges"], expect["edges_covered"])
        assert (row["prime_paths"], row["prime_paths_covered"]) == (
            expect["prime_paths"], expect["prime_paths_covered"])
        assert row["gaps"] == len(expect["gaps"])
    total = parsed["total"]
    assert total["edges"] == sum(len(g.edges) for g in GRAPHS.values())
    assert total["edges_covered"] == total["edges"]
    assert total["prime_paths_covered"] == total["prime_paths"]
    json.dumps(parsed)  # the snapshot the board writes is JSON-serialisable
