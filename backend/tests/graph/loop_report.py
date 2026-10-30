"""The loop-engine section of ``docs/cn/reports/graph-coverage.md`` (round 17, r17-b).

Appended by ``tests/graph/conftest.py`` after the r16-o coverage table when
``LAUNCHPAD_GRAPH_REPORT`` is set (``make graph-coverage``): per graph the random walks the
recording run completed (``paths`` hits named ``W<seed>``), the walk findings registered so far
(``walks.FINDINGS``), the neighbourhood paths generated around the pinned edges and how many of
them no prime path / edge pair had covered before (``neighbourhood.summarize``), and the
cross-graph walks completed (``X<seed>`` hits, recorded under the ``reference`` graph). The
quality board reads the table (``scripts/quality_metrics.py`` → ``graph_walks``,
``graph_walk_findings``, ``neighbourhood_paths``, ``graph_cross_walks``).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import yaml

from tests.graph import crossgraph, faults, neighbourhood, walks
from tests.graph.paths import Graph

ROUNDS_FILE = Path(__file__).resolve().parent / "loop_rounds.yaml"
STOP_STREAK = 3  # the user's rule: three consecutive zero-yield rounds end the loop


def load_rounds(path: Path = ROUNDS_FILE) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rounds = sorted(raw.get("rounds") or [], key=lambda r: int(r["round"]))
    for i, r in enumerate(rounds):
        r["findings"] = list(r.get("findings") or [])
        streak = 0
        for prev in rounds[: i + 1][::-1]:
            if prev.get("findings"):
                break
            streak += 1
        r["streak"] = streak
    return rounds


def loop_state(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """The loop's position under the user's rule: the latest round, its method and yield, the
    zero-yield streak, whether the loop stops (streak == 3) and whether the next round must
    switch method (the latest round found nothing)."""
    if not rounds:
        return {"round": None, "method": None, "findings": 0, "streak": 0, "stop": False,
                "switch_method": False}
    last = rounds[-1]
    return {
        "round": int(last["round"]), "method": str(last.get("method") or ""),
        "findings": len(last["findings"]), "streak": last["streak"],
        "stop": last["streak"] >= STOP_STREAK, "switch_method": not last["findings"],
    }


def render_rounds(rounds: list[dict[str, Any]]) -> str:
    """One line per round the board parses:
    ``- round N · <method> · findings K (ids) · streak S``."""
    lines = ["## Loop rounds (user rule 2026-10-19: stop after three consecutive zero-yield "
             "rounds)",
             "",
             "After a round that found bugs the next round keeps its method; after a zero-yield "
             "round the next round must use a different method / idea; the loop stops only after "
             "three consecutive zero-yield rounds (any finding resets the streak). Prime-path "
             "coverage and capture–recapture are reference metrics.", ""]
    for r in rounds:
        ids = ", ".join(r["findings"])
        lines.append(f"- round {r['round']} · {r['method']} · findings {len(r['findings'])}"
                     f"{' (' + ids + ')' if ids else ''} · streak {r['streak']}"
                     f"{' · STOP' if r['streak'] >= STOP_STREAK else ''}")
    state = loop_state(rounds)
    if state["round"] is not None:
        nxt = ("the loop STOPS" if state["stop"] else
               "next round: a DIFFERENT method" if state["switch_method"] else
               "next round: the same method")
        lines.append("")
        lines.append(f"State: round {state['round']} yielded {state['findings']} finding(s), "
                     f"zero-yield streak {state['streak']}/{STOP_STREAK} — {nxt}.")
    lines.append("")
    return "\n".join(lines)

WORLDS: dict[str, tuple[str, str]] = {
    "agent": ("tests.graph.test_graph_agent", "AgentWorld"),
    "architect_proposal": ("tests.graph.test_graph_architect_proposal", "ProposalWorld"),
    "chat_session": ("tests.graph.test_graph_chat_session", "ChatWorld"),
    "eval_run": ("tests.graph.test_graph_eval_run", "EvalRunWorld"),
    "memory_record": ("tests.graph.test_graph_memory_record", "MemoryRecordWorld"),
    "reference": ("tests.graph.test_graph_reference", "ReferenceWorld"),
    "runtime_canary": ("tests.graph.test_graph_runtime_canary", "CanaryWorld"),
    "workspace": ("tests.graph.test_graph_workspace", "WorkspaceWorld"),
}
WALK_ID = re.compile(r"/W\d{4}$")
CROSS_ID = re.compile(r"^(?P<pairing>[a-z_]+)/X\d{4}$")
FAULT_ID = re.compile(r"/F\d{4}-[a-z0-9_]+$")  # round 6: a fault-injected walk (faults.py)


def world_class(graph: str) -> type | None:
    spec = WORLDS.get(graph)
    if spec is None:
        return None
    module, name = spec
    return getattr(importlib.import_module(module), name)


def summarize(graph: Graph, hits: dict[str, Any] | None) -> dict[str, Any]:
    paths = set((hits or {}).get("paths") or ())
    world = world_class(graph.name)
    nb = neighbourhood.summarize(graph, world)
    cross = {}
    for pid in paths:
        m = CROSS_ID.match(pid)
        if m:
            cross[m.group("pairing")] = cross.get(m.group("pairing"), 0) + 1
    return {
        "graph": graph.name,
        "walks": sum(1 for p in paths if WALK_ID.search(p)),
        "walk_findings": sorted(rid for rid, g, _label in walks.FINDINGS if g == graph.name),
        "fault_walks": sum(1 for p in paths if FAULT_ID.search(p)),
        "fault_findings": sorted(rid for rid, g, _label in faults.FINDINGS if g == graph.name),
        "neighbourhood_paths": nb["paths"],
        "neighbourhood_new": nb["newly_covered"],
        "pins": nb["pins"],
        "cross_walks": cross,
        "cross_findings": sorted(rid for rid, _pairing, _label in crossgraph.FINDINGS)
        if graph.name == "reference" else [],
        "fixed_pins": dict(getattr(world, "FIXED", None) or {}) if world else {},
    }


def render(graphs: dict[str, Graph], hits: dict[str, dict[str, Any]]) -> str:
    rows = [summarize(g, hits.get(name)) for name, g in sorted(graphs.items())]
    lines = [
        "## Loop engine (round 17, r17-b — testing.md §6.8)",
        "",
        "Seeded random walks (20–60 edges, refusal loops included; 8 seeds per graph in verify,",
        "200 under `LAUNCHPAD_SOAK=1`), the bug-neighbourhood paths generated around every pinned",
        "edge (edge pairs within distance 2, 3-edge windows through the pin, prime paths through",
        "it), the cross-graph walks on the reference graph's dependencies (agent × canary,",
        "agent × chat, dataset × run; the delete injected mid-walk) and, since round 6, the",
        "fault-injected walks (the recorded seam shapes served mid-walk, testing.md §6.9). A walk",
        "finding is a register row a walk opened (fixed or pinned).",
        "",
        "| graph | walks | walk findings | neighbourhood paths | neighbourhood newly covered "
        "| cross-graph walks | cross findings | fault walks | fault findings |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tot = {"walks": 0, "wf": 0, "np": 0, "nn": 0, "cw": 0, "cf": 0, "fw": 0, "ff": 0}
    for r in rows:
        cw = sum(r["cross_walks"].values())
        cross = ", ".join(f"{k} {v}" for k, v in sorted(r["cross_walks"].items())) or "—"
        lines.append(
            f"| `{r['graph']}` | {r['walks']} | {len(r['walk_findings'])}"
            f"{' (' + ', '.join(r['walk_findings']) + ')' if r['walk_findings'] else ''} | "
            f"{r['neighbourhood_paths']} | {r['neighbourhood_new']} | {cw}"
            f"{' (' + cross + ')' if cw else ''} | {len(r['cross_findings'])}"
            f"{' (' + ', '.join(r['cross_findings']) + ')' if r['cross_findings'] else ''} | "
            f"{r['fault_walks']} | {len(r['fault_findings'])}"
            f"{' (' + ', '.join(r['fault_findings']) + ')' if r['fault_findings'] else ''} |"
        )
        tot["walks"] += r["walks"]
        tot["wf"] += len(r["walk_findings"])
        tot["np"] += r["neighbourhood_paths"]
        tot["nn"] += r["neighbourhood_new"]
        tot["cw"] += cw
        tot["cf"] += len(r["cross_findings"])
        tot["fw"] += r["fault_walks"]
        tot["ff"] += len(r["fault_findings"])
    lines.append(
        f"| **total** | {tot['walks']} | {tot['wf']} | {tot['np']} | {tot['nn']} | {tot['cw']} "
        f"| {tot['cf']} | {tot['fw']} | {tot['ff']} |")
    lines.append("")
    pins = [(r["graph"], rid, edges, rid in r["fixed_pins"])
            for r in rows for rid, edges in sorted(r["pins"].items())]
    if pins:
        lines.append("Pinned edges the neighbourhood re-explores: " + "; ".join(
            f"`{g}` {rid}{' (FIXED — kept)' if fixed else ''} "
            f"({', '.join(f'`{e}`' for e in edges)})" for g, rid, edges, fixed in pins) + ".")
        lines.append("")
    lines.append(render_rounds(load_rounds()))
    return "\n".join(lines)
