"""Unit tests of the loop engine's generators (round 17, r17-b): random walks and the shrinker
(``walks.py``), the neighbourhood requirements (``neighbourhood.py``), the cross-graph plan and
the status-aware dangling-reference oracle (``crossgraph.py``). Hand-built graphs with known
answers first, then the shipped graphs for the structural properties the executors rely on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.optimization.models import RuntimeCanary
from tests.graph import crossgraph, neighbourhood, walks
from tests.graph.paths import Edge, Graph, load_all, walk_paths
from tests.test_visibility import _active_agent, _canary

GRAPHS = load_all()


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


# ---------------------------------------------------------------------------
# random walks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GRAPHS))
def test_walks_are_deterministic_connected_and_in_budget(name):
    g = GRAPHS[name]
    for seed in range(12):
        w1, w2 = walks.random_walk(g, seed), walks.random_walk(g, seed)
        assert w1 == w2
        assert w1.edges and len(w1.edges) <= w1.requested <= walks.MAX_LEN
        assert w1.requested >= walks.MIN_LEN
        assert w1.edges[0].src == g.initial
        assert all(a.dst == b.src for a, b in zip(w1.edges, w1.edges[1:], strict=False))
        if len(w1.edges) < w1.requested:  # ended early: a dead end, never a choice
            assert not g.out_edges(w1.edges[-1].dst)


def test_walks_exclude_pinned_edges_and_reach_refusals():
    g = GRAPHS["chat_session"]
    seen: set[str] = set()
    for seed in range(40):
        w = walks.random_walk(g, seed, exclude=frozenset({"v1_send_ended"}))
        assert "v1_send_ended" not in {e.id for e in w.edges}
        seen |= {e.id for e in w.edges}
    assert any(g.edge(e).is_refusal for e in seen), "walks must attempt refusals"
    assert len(seen) > len(g.edges) // 2


def test_walk_seeds_are_a_prefix_of_the_soak_seeds(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_SOAK", raising=False)
    monkeypatch.delenv("LAUNCHPAD_LOOP_ROUND", raising=False)
    verify = walks.walk_seeds()
    monkeypatch.setenv("LAUNCHPAD_SOAK", "1")
    soak = walks.walk_seeds()
    assert verify == soak[: len(verify)] and len(soak) == walks.SOAK_SEEDS


def test_round_two_draws_a_fresh_seed_window(monkeypatch):
    """The user's rule: a later round is never a replay — round r soaks seeds
    [(r-1)·200, r·200) and cross seeds [(r-1)·60, r·60); the verify shape stays fixed."""
    monkeypatch.setenv("LAUNCHPAD_SOAK", "1")
    monkeypatch.setenv("LAUNCHPAD_LOOP_ROUND", "2")
    assert walks.walk_seeds() == list(range(200, 400))
    assert crossgraph.cross_seeds(soak=True) == list(range(60, 120))
    assert crossgraph.cross_seeds(soak=False) == list(range(6))
    monkeypatch.delenv("LAUNCHPAD_SOAK")
    assert walks.walk_seeds() == list(range(8))
    # round-2 cross plans add the redeploy injection; round-1 plans replay unchanged
    assert crossgraph.plan("agent_x_canary", 3).redeploy_at == -1
    p = crossgraph.plan("agent_x_canary", 61)
    assert p.length // 3 <= p.redeploy_at <= 2 * p.length // 3
    assert crossgraph.plan("agent_x_run", 0).redeploy_at >= 0  # the new pairing always has it
    # round 3: the visibility flip (to isolated, then back) on seeds ≥ 120 and the new pairings
    assert crossgraph.plan("agent_x_canary", 61).flip_at == -1
    p3 = crossgraph.plan("agent_x_canary", 125)
    assert p3.length // 3 <= p3.flip_at <= 2 * p3.length // 3 < p3.length
    assert p3.flip_back_at >= 2 * p3.length // 3
    assert crossgraph.plan("canary_x_dataset", 0).flip_at >= 0
    monkeypatch.setenv("LAUNCHPAD_SOAK", "1")
    monkeypatch.setenv("LAUNCHPAD_LOOP_ROUND", "3")
    assert walks.walk_seeds() == list(range(400, 600))
    assert crossgraph.cross_seeds(soak=True) == list(range(120, 180))


class _FakeWorld:
    """Fails at edge ``bad`` once ``trigger`` was walked at least ``times`` times before it —
    a history-dependent failure the shrinker must keep while removing unrelated cycles."""

    XFAIL: dict[str, str] = {}

    def __init__(self, bad: str, trigger: str, times: int) -> None:
        self.bad, self.trigger, self.times = bad, trigger, times
        self.count = 0
        self.steps: list[str] = []

    def step(self, edge: Edge) -> None:
        self.steps.append(edge.id)
        if edge.id == self.trigger:
            self.count += 1
        if edge.id == self.bad and self.count >= self.times:
            raise AssertionError(f"{self.bad} after {self.count} × {self.trigger}")

    def close(self) -> None:
        pass


def test_shrink_keeps_the_history_the_failure_needs_and_drops_the_rest():
    # a — ab → b ⟲ loop, b — bc → c, c — ca → a ; `bad` fails once `loop` ran twice
    g = _graph("a", [("ab", "a", "b"), ("loop", "b", "b"), ("bc", "b", "c"), ("ca", "c", "a"),
                     ("bad", "c", "c")], refusals={"loop", "bad"})
    seq = tuple(g.edge(x) for x in
                ("ab", "loop", "bc", "ca", "ab", "bc", "ca", "ab", "loop", "bc", "bad", "ca"))
    failure = walks.run_sequence(_FakeWorld("bad", "loop", 2), seq)
    assert failure is not None and failure.edge_id == "bad" and failure.index == 10
    minimal, final, replays = walks.shrink(lambda: _FakeWorld("bad", "loop", 2), seq, failure)
    assert final.edge_id == "bad" and minimal[-1].id == "bad"
    labels = [e.id for e in minimal]
    assert labels.count("loop") == 2, labels  # the two triggers survive
    assert "ca" not in labels or labels.count("ca") < 2  # the detour cycle is gone
    assert len(minimal) <= 6, labels
    assert replays <= walks.SHRINK_BUDGET
    # the minimal sequence still fails, and nothing shorter obtained by removing one cycle does
    assert walks.run_sequence(_FakeWorld("bad", "loop", 2), minimal) is not None
    for i, j in walks._cycles(minimal):
        if j < len(minimal):
            shorter = (*minimal[:i], *minimal[j:])
            got = walks.run_sequence(_FakeWorld("bad", "loop", 2), shorter)
            assert got is None or got.edge_id != "bad" or got.index != len(shorter) - 1


def test_repro_hash_matches_the_board_convention():
    assert walks.repro_hash("  A>b   >C ") == walks.repro_hash("a>b >c")
    assert len(walks.repro_hash("x")) == 8


# ---------------------------------------------------------------------------
# neighbourhood
# ---------------------------------------------------------------------------


def test_line_distances_count_hops_over_shared_nodes():
    g = _graph("a", [("ab", "a", "b"), ("bc", "b", "c"), ("cd", "c", "d"), ("de", "d", "e"),
                     ("bb", "b", "b")], refusals={"bb"})
    d = neighbourhood.line_distances(g, "bc")
    assert d == {"bc": 0, "ab": 1, "cd": 1, "bb": 1, "de": 2}


def test_neighbourhood_requirements_on_a_chain_with_a_pinned_loop():
    g = _graph("a", [("ab", "a", "b"), ("bc", "b", "c"), ("cd", "c", "d"), ("pin", "c", "c")],
               refusals={"pin"})
    reqs = neighbourhood.requirements(g, "pin", "N17-obs99")
    by_kind = {k: sorted(r.label for r in reqs if r.kind == k) for k in ("pair", "window", "prime")}
    assert by_kind["prime"] == ["pin"]
    assert "bc>pin>cd" in by_kind["window"] and "pin>pin>cd" in by_kind["window"]
    assert "ab>bc>pin" in by_kind["window"]
    assert {"bc>pin", "pin>cd", "pin>pin", "ab>bc", "bc>cd"} <= set(by_kind["pair"])
    paths = neighbourhood.test_paths(g)  # no world: the YAML pins only
    assert paths == []  # the synthetic graph has no `register:` edge
    cls = type("W", (), {"XFAIL": {"pin": "N17-obs99: pinned (synthetic)"}})
    paths = neighbourhood.test_paths(g, cls)
    assert paths and all(tp.edges[0].src == "a" for _r, tp in paths)
    assert all(tp.id.startswith("t/N17-obs99/") for _r, tp in paths)
    labels = {tp.label for _r, tp in paths}
    assert len(labels) == len(paths)  # no two test ids walk the same prefixed path
    by_label = {r.label: r for r in reqs}
    assert neighbourhood.previously_covered(g, by_label["bc>pin"])  # an enumerated edge pair
    # `ab>bc>pin>cd` (the pair pin>cd prefixed) already contains bc>pin>cd; pin>pin>cd is new
    assert neighbourhood.previously_covered(g, by_label["bc>pin>cd"])
    assert not neighbourhood.previously_covered(g, by_label["pin>pin>cd"])
    kinds = {tp.label: r.kind for r, tp in paths}
    # most specific wins: the self-loop prime path claims `ab>bc>pin` (not the prefixed pair
    # bc>pin), the window claims `ab>bc>pin>pin>cd`
    assert kinds["ab>bc>pin"] == "prime" and kinds["ab>bc>pin>pin>cd"] == "window"


def test_pinned_edges_read_the_yaml_register_ids_and_the_xfail_table():
    canary = neighbourhood.pinned_edges(GRAPHS["runtime_canary"])
    assert set(canary) == {"N16-obs116"}
    assert {"rollback", "rollback_fresh", "action_after_complete", "action_after_rollback",
            "rollback_s1", "rollback_s2"} == set(canary["N16-obs116"])
    cls = type("W", (), {"XFAIL": {"v1_send_ended": "N16-obs88: /v1 re-opens"}})
    chat = neighbourhood.pinned_edges(GRAPHS["chat_session"], cls)
    assert chat == {"N16-obs88": ["v1_send_ended"]}
    assert neighbourhood.pinned_edges(GRAPHS["agent"]) == {}


@pytest.mark.parametrize("name", ["chat_session", "runtime_canary", "architect_proposal"])
def test_shipped_neighbourhoods_are_connected_and_report_new_coverage(name):
    g = GRAPHS[name]
    from tests.graph import loop_report

    cls = loop_report.world_class(name)
    summary = neighbourhood.summarize(g, cls)
    assert summary["paths"] > 0 and summary["newly_covered"] > 0
    enumerated = {tuple(e.id for e in tp.edges) for tp in walk_paths(g, include_covered_pairs=True)}
    for req, tp in neighbourhood.test_paths(g, cls):
        assert tp.edges[0].src == g.initial
        assert all(a.dst == b.src for a, b in zip(tp.edges, tp.edges[1:], strict=False))
        assert any(e.id in neighbourhood.pinned_edges(g, cls)[req.register] for e in req.edges) \
            or req.kind == "pair"
        if req.kind == "pair":
            assert tuple(e.id for e in tp.edges) in enumerated or neighbourhood.previously_covered(
                g, req)


# ---------------------------------------------------------------------------
# cross-graph
# ---------------------------------------------------------------------------


def test_cross_plan_is_deterministic_and_pick_is_order_independent():
    for pairing in crossgraph.PAIRINGS:
        a, b = crossgraph.plan(pairing, 7), crossgraph.plan(pairing, 7)
        assert a == b and a.length // 3 <= a.inject_at <= 2 * a.length // 3
    import random

    g = GRAPHS["agent"]
    edges = g.out_edges("active")
    r1, r2 = random.Random(3), random.Random(3)
    assert crossgraph.pick(r1, edges) == crossgraph.pick(r2, list(reversed(edges)))


def test_dangling_references_sees_a_deleted_agent_under_a_running_canary():
    agent_id = _active_agent("x-dangle")
    canary_id = _canary(None, champion_agent_id=agent_id, challenger_agent_id=agent_id,
                        status="running")
    ids = {"agent_ids": {agent_id}, "canary_ids": {canary_id}, "run_ids": set()}
    assert crossgraph.dangling_references(SessionLocal, **ids) == []
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = "deleted"
        db.commit()
    finally:
        db.close()
    out = crossgraph.dangling_references(SessionLocal, **ids)
    assert len(out) == 1 and "deleted agent" in out[0] and "agents.md §1.1" in out[0]
    db = SessionLocal()
    try:
        db.get(RuntimeCanary, canary_id).status = "cleaned"  # settled canaries never block
        db.commit()
    finally:
        db.close()
    assert crossgraph.dangling_references(SessionLocal, **ids) == []


def test_loop_rounds_yaml_parses_with_a_strict_yaml_loader():
    """N17-obs162: the round ledger is hand-written prose inside YAML scalars; an unquoted
    ``: `` broke ``make graph-coverage`` on the merged tip while every per-branch gate passed
    because nothing here parsed the file with PyYAML. Now the gate does."""
    import pathlib

    import yaml

    path = pathlib.Path(__file__).with_name("loop_rounds.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("rounds"), "loop_rounds.yaml must carry rounds"
