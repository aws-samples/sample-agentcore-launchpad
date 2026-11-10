# ruff: noqa: F811  — pytest fixtures are imported by name from the sibling suites
"""Refusal × non-happy-state matrix (round 4 of the loop engine, r17-i; `refusal_matrix.py`).

One composite world per (pairing, refusal edge): the dependent graph is brought to the edge's
source node through its BFS prefix while the shared agent is `active`; then the agent moves
through REAL transitions — `redeploy` (a live update job: `redeploying`), `redeploy_succeeds`,
`delete_active` (`deleted`) — and at every state the refusal edge's action is attempted by every
caller (creator · other member · administrator) under both visibility modes and, where the
request carries a body, with an empty body. Per cell: the per-table digest must be unchanged
(a refusal writes nothing), a hidden id must answer the kind's 404 (users-and-ownership.md §7 —
anything else is a leak, PRODUCT), the answer must be one of the applicable gates' (else
`unexpected`, a finding to shrink), and a cell whose applicable gates no sentence orders is an
`unordered` pair recorded for the register (DOC). Verify runs every state × caller × visibility
with a valid body plus the empty-body cells for the creator under `shared`; `LAUNCHPAD_SOAK=1`
runs the full grid. `LAUNCHPAD_MATRIX_REPORT=<path.jsonl>` appends one JSON line per test with
the observations (what the round's register rows are written from).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.graph import refusal_matrix as rm
from tests.graph.executor import GRAPHS, ledger_digest
from tests.graph.paths import Edge, shortest_prefix
from tests.graph.test_graph_agent import AgentWorld
from tests.graph.test_graph_chat_session import BUDGET_S
from tests.graph.test_graph_crossgraph import (  # noqa: F401 — fixtures by name
    AgentCanary,
    AgentChat,
    AgentRun,
    Cross,
    _clients,
    agent_hooks,
    chat_producer,
)
from tests.model.test_canary_state_machine import seams  # noqa: F401
from tests.test_visibility import ada, admin, app, bob  # noqa: F401

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
REPORT = os.environ.get("LAUNCHPAD_MATRIX_REPORT")

#: pairing → (dependent graph, which graphs' refusal edges this pairing drives)
PAIRINGS = {
    "agent_x_chat": ("chat_session", ("chat_session", "agent")),
    "agent_x_canary": ("runtime_canary", ("runtime_canary",)),
    "agent_x_run": ("eval_run", ("eval_run",)),
}
# edges the matrix cannot attempt generically (the live-turn gate is a thread; v1 needs the
# creator's key — attempted for ada only; the memory-record graph has no agent)
SKIP_EDGES = {"invalid_session_id"}
#: Refusal edges whose source node is outside AGENT_STATES (`deploying` is the FIRST deploy of a
#: fresh agent, `redeploying_cold` a re-publish from `failed`, `absent` the pre-create state) —
#: the shared agent of a pairing is created active, so these are not reachable by `agent_to`
#: and have no matrix cells. Listed here with the reason so the vocabulary test and the report
#: see them (rev-r17 on cn/r17-i: they used to be 0 cells silently).
OUTSIDE_STATES: dict[str, str] = {}  # round 5: the second fresh agent reaches them all
CANARY_ACTION = {
    "traffic_before_setup": ("traffic", True), "setup_twice": ("setup", False),
    "verdict_before_traffic": ("verdict", False), "traffic_without_dataset": ("traffic", False),
    "advance_before_verdict": ("advance", False), "other_member_advance": ("advance", False),
    "complete_before_final": ("complete", False), "verdict_before_traffic_s1": ("verdict", False),
    "advance_before_verdict_s1": ("advance", False), "other_member_advance_s1": ("advance", False),
    "complete_before_final_s1": ("complete", False),
    "verdict_before_traffic_s2": ("verdict", False),
    "advance_before_verdict_s2": ("advance", False),
    "other_member_advance_s2": ("advance", False), "advance_at_final": ("advance", False),
    "action_after_complete": ("advance", False), "action_after_rollback": ("traffic", True),
}


def cells_for(pairing: str, graph: str, edge: Edge) -> list[tuple[str, str, str]]:
    """(caller, visibility, body) combinations for one edge: the ownership edges are the other
    member's by definition (the creator / administrator would simply succeed and mutate the
    world); every other edge is attempted by all three callers."""
    callers = ("bob",) if edge.id in rm.OWNERSHIP_EDGES else rm.CALLERS
    if edge.id in rm.CHAT_V1:
        callers = ("ada",)  # the /v1 key is the creator's
    if edge.id == "admin_reattach":
        callers = ("admin", "bob")  # the creator's reattach is her own turn (200)
    out = []
    for caller in callers:
        for vis in rm.VISIBILITY:
            out.append((caller, vis, "valid"))
            if edge.id in rm.BODY_ROUTES.get(graph, set()) and (SOAK or (caller == "ada"
                                                                        and vis == "shared")):
                out.append((caller, vis, "empty"))
    return out


# ---------------------------------------------------------------------------
# raw attempts
# ---------------------------------------------------------------------------


def _bounded_get(client: TestClient, url: str, **kw: Any):
    box: dict = {}

    def go():
        box["res"] = client.get(url, **kw)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout=BUDGET_S)
    assert "res" in box, f"{url} gave no answer within {BUDGET_S:.0f} s"
    return box["res"]


def attempt(x: Cross, graph: str, edge: Edge, caller: str, body: str, target: Any = None):
    """Perform the refusal edge's request as `caller` with a valid or an empty body; `target`
    is the agent world the agent-graph edges address (the shared one, or the second fresh agent
    the driver stages for `absent` / `deploying` / `redeploying_cold`)."""
    client = x.clients[caller]
    empty = body == "empty"
    if graph == "agent":
        w = target or x.a
        if edge.id in ("member_redeploy_other", "redeploy_while_deploying", "redeploy_deleted"):
            return client.post(f"/api/agents/{w.agent_id}/redeploy", json={} if empty else w.spec())
        if edge.id in ("member_delete_other", "delete_while_deploying", "delete_while_redeploying",
                       "delete_while_cold_redeploying", "delete_deleted"):
            return client.delete(f"/api/agents/{w.agent_id}")
        if edge.id.startswith("chat_"):
            return client.post(f"/api/chat/{w.agent_id}", json={} if empty else {"prompt": "hi"})
        if edge.id == "create_duplicate_while_deploying":
            return client.post("/api/agents", json={} if empty else w.spec())
        if edge.id == "create_name_reserved":
            return client.post("/api/agents", json={} if empty else w.spec("launchpad-eval-worker"))
    if graph == "chat_session":
        w = x.b
        base, sid = w.base, w.sid
        if edge.id in rm.CHAT_SENDS:
            return client.post(base, json={} if empty else {"prompt": "p", "session_id": sid})
        if edge.id in rm.CHAT_V1:
            return client.post(f"/v1/agents/{w.agent_id}/invoke", headers={"X-Api-Key": w.api_key},
                               json={} if empty else {"prompt": "v1", "session_id": sid})
        if edge.id in ("other_member_stop",):
            return client.post(f"{base}/sessions/{sid}/stop")
        if edge.id in ("other_member_history",):
            return _bounded_get(client, f"{base}/history", params={"session_id": sid})
        if edge.id == "reattach_unknown_turn":
            return _bounded_get(client, f"{base}/sessions/{sid}/turns/{w.turn + 5}/stream")
        if edge.id in ("other_member_reattach", "admin_reattach"):
            return _bounded_get(client, f"{base}/sessions/{sid}/turns/1/stream")
    if graph == "runtime_canary":
        w = x.b
        if edge.id in rm.CANARY_CREATE:
            agent = w.deploying_id if edge.id == "create_inactive_agent" else w.agent_id
            return client.post("/api/runtime-canaries", json={} if empty else w.create_body(agent))
        action, with_dataset = CANARY_ACTION[edge.id]
        payload = {} if empty else {"action": action,
                                    **({"dataset_id": w.dataset_id} if with_dataset else {})}
        return client.post(f"/api/runtime-canaries/{w.canary_id}/action", json=payload)
    if graph == "eval_run":
        w = x.b
        if edge.id == "create_inactive_agent":
            return client.post("/api/eval/runs",
                               json={} if empty else w.body(agent_id=w.deploying_id))
        if edge.id == "create_no_evaluators":
            return client.post("/api/eval/runs", json={} if empty else w.body(evaluators=[]))
        if edge.id.startswith("dataset_delete"):
            return client.delete(f"/api/eval/datasets/{w.dataset_id}")
        if edge.id.startswith("stop_"):
            return client.post(f"/api/eval/runs/{w.run_id}/stop")
        if edge.id.startswith("delete_"):
            return client.delete(f"/api/eval/runs/{w.run_id}")
        if edge.id.startswith("recheck_"):
            return client.post(f"/api/eval/runs/{w.run_id}/recheck")
        if edge.id == "rerun_sessions_scope":
            return client.post(f"/api/eval/runs/{w.sessions_run}/rerun")
        if edge.id.startswith("rerun_"):
            return client.post(f"/api/eval/runs/{w.run_id}/rerun")
        if edge.id == "compare_with_queued":
            return client.get(f"/api/eval/runs/compare?a={w.other}&b={w.run_id}")
    raise AssertionError(f"no raw attempt for {graph}.{edge.id}")


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------


def make_cross(pairing: str, fixtures: dict) -> Cross:
    clients, ada_id = fixtures["clients"], fixtures["ada_id"]
    if pairing == "agent_x_chat":
        producer, plane = fixtures["chat_producer"]
        return AgentChat(clients, 0, fixtures["agent_hooks"], fixtures["app"], producer, plane)
    if pairing == "agent_x_canary":
        return AgentCanary(clients, 0, fixtures["agent_hooks"], ada_id, fixtures["seams"])
    return AgentRun(clients, 0, fixtures["agent_hooks"], fixtures["monkeypatch"], ada_id)


def reach(x: Cross, graph_b: str, node: str) -> bool:
    """Bring the dependent world to `node` through its BFS prefix (agent active)."""
    prefix = shortest_prefix(GRAPHS[graph_b], node)
    assert prefix is not None
    for e in prefix:
        x.b.before(e)
        x.step_b(e)
    return True


def agent_to(x: Cross, state: str) -> bool:
    """Move the shared agent to `state` through the agent graph's own edges (refused moves —
    a redeploy / delete under a running canary or an ACTIVE run — make the state unreachable
    for this world: the cells are skipped, never faked)."""
    graph = GRAPHS["agent"]
    before = x.node_a
    if state == "redeploying":
        x.step_a(graph.edge("redeploy"))
    elif state == "active" and x.node_a == "redeploying":
        x.step_a(graph.edge("redeploy_succeeds"))
    elif state == "deleted":
        if x.node_a == "redeploying":
            x.step_a(graph.edge("redeploy_succeeds"))
        x.step_a(graph.edge("delete_active"))
    return x.node_a != before or x.node_a == state


def owned_by(caller: str, pairing: str, graph: str, edge: Edge) -> bool:
    """Does the caller own the resource the edge addresses? ada created everything; the
    canary world's dataset is an administrator's (NULL creator) — visible to every member."""
    if caller in ("ada", "admin"):
        return True
    return False


def second_agent(x: Cross, state: str) -> AgentWorld:
    """A fresh agent of ada's brought to `state` through the agent graph's own edges: `absent`
    (nothing created), `deploying` (POST accepted, the first deploy job queued) or
    `redeploying_cold` (first deploy failed, re-published)."""
    graph = GRAPHS["agent"]
    w = AgentWorld(x.clients, x.hooks)
    if state in ("deploying", "redeploying_cold"):
        w.step(graph.edge("create"))
    if state == "redeploying_cold":
        w.step(graph.edge("deploy_fails"))
        w.step(graph.edge("redeploy_from_failed"))
    return w


def name_leak(x: Cross, edge: Edge, cell: rm.Cell, response) -> None:
    """users-and-ownership.md §7 / agents.md §4.4: under `isolated` the duplicate-name
    `409 agent.name_exists` is unavoidable for a hidden holder (the name invariant is
    workspace-wide) and masks the holder — `detail.agent_id: null`; the hidden row's id in
    `detail` is a leak (PRODUCT)."""
    if edge.id != "create_duplicate_while_deploying" or cell.caller != "bob" \
            or cell.visibility != "isolated" or response.status_code != 409:
        return
    detail = (response.json().get("detail") or {})
    leaked = detail.get("agent_id")
    assert not leaked, ("hidden agent id leaked through agent.name_exists", detail, cell.label)


def drive_second_agent(x: Cross, pairing: str, graph: str, edge: Edge,
                       skipped: list[dict[str, str]]) -> list[rm.Observation]:
    """The agent graph's refusals whose node the shared agent never visits: cells on a second
    fresh agent per (state) — one world, every caller × visibility × body."""
    observations: list[rm.Observation] = []
    state = edge.src
    x.setup()
    w = second_agent(x, state)
    base = (edge.expect.get("status"), edge.expect.get("code"))
    for caller, vis, body in cells_for(pairing, graph, edge):
        if vis == "isolated" and not x.isolated:
            x.flip("isolated")
        elif vis == "shared" and x.isolated:
            x.flip("shared")
        cell = rm.Cell(pairing, graph, edge.id, state, caller, vis, body)
        res = rm.resolve(cell, base, owned_by_caller=owned_by(caller, pairing, graph, edge))
        before = ledger_digest()
        response = attempt(x, graph, edge, caller, body, target=w)
        status, code = response.status_code, None
        try:
            code = response.json().get("code")
        except ValueError:
            pass
        after = ledger_digest()
        if 400 <= status < 500:
            changed = sorted(k for k in after if before.get(k) != after[k])
            assert not changed, (cell.label, status, code, "refusal wrote", changed)
        name_leak(x, edge, cell, response)
        observations.append(rm.Observation(cell, status, code, res))
        if status < 400:
            x.note(f"{cell.label} → {status} (success; cells stop)")
            break
    if x.isolated:
        x.flip("shared")
    return observations


def drive(x: Cross, pairing: str, graph: str, edge: Edge,
          skipped: list[dict[str, str]] | None = None) -> list[rm.Observation]:
    """Every (state, reason) the edge could NOT be attempted in lands in ``skipped`` (and the
    world's trace) — a skipped cell is reported, never silently absent (rev-r17 on cn/r17-i:
    504 observed of 741 grid cells with nothing saying why)."""
    observations: list[rm.Observation] = []
    skipped = skipped if skipped is not None else []
    graph_b = PAIRINGS[pairing][0]
    if graph == "agent" and edge.src in rm.SECOND_AGENT_STATES:
        return drive_second_agent(x, pairing, graph, edge, skipped)
    x.setup()
    if graph == graph_b:
        reach(x, graph_b, edge.src)
        x.b.before(edge)  # staging the edge needs (a second completed run for a compare)
    base = (edge.expect.get("status"), edge.expect.get("code"))
    for state in rm.AGENT_STATES:
        if state != "active":
            try:
                agent_to(x, state)
            except AssertionError as exc:
                x.note(f"{state} unreachable: {str(exc)[:80]}")
                skipped.append({"state": state, "reason": f"unreachable: {str(exc)[:120]}"})
                continue
            if (state == "redeploying" and x.node_a != "redeploying") or \
                    (state == "deleted" and x.node_a != "deleted"):
                # refused by a cross rule — a running canary / an ACTIVE run owns the agent
                # (agents.md §1.1 / §1.3, N17-obs16 / obs18): the state is unreachable for
                # this world by design, and the report says so
                reason = (f"refused by the cross rule: the agent stays {x.node_a} "
                          f"(a running canary / ACTIVE run owns it)")
                x.note(f"{state} skipped — {reason}")
                skipped.append({"state": state, "reason": reason})
                continue
        if graph == "agent":
            # the agent graph's own refusals belong to one node: attempt them only there
            want = {"active": "active", "redeploying": "redeploying", "deleted": "deleted"}[state]
            if edge.src != want:
                skipped.append({"state": state,
                                "reason": f"the agent graph's edge belongs to node `{edge.src}`"})
                continue
        for caller, vis, body in cells_for(pairing, graph, edge):
            if vis == "isolated" and not x.isolated:
                x.flip("isolated")
            elif vis == "shared" and x.isolated:
                x.flip("shared")
            cell = rm.Cell(pairing, graph, edge.id, state, caller, vis, body)
            res = rm.resolve(cell, base, owned_by_caller=owned_by(caller, pairing, graph, edge))
            before = ledger_digest()
            response = attempt(x, graph, edge, caller, body)
            status = response.status_code
            code = None
            try:
                code = response.json().get("code")
            except ValueError:
                pass
            after = ledger_digest()
            changed = sorted(k for k in after if before.get(k) != after[k])
            if 400 <= status < 500:
                assert not changed, (cell.label, status, code, "refusal wrote", changed)
            obs = rm.Observation(cell, status, code, res)
            observations.append(obs)
            if status < 400:
                # a success mutates the world: stop this edge's cells here
                x.note(f"{cell.label} → {status} (success; cells stop)")
                return observations
        if x.isolated:
            x.flip("shared")
    return observations


def _report(pairing: str, graph: str, edge: str, observations: list[rm.Observation],
            skipped: list[dict[str, str]] | None = None) -> None:
    if not REPORT:
        return
    line = {
        "pairing": pairing, "graph": graph, "edge": edge,
        # rev-r17 on cn/r17-i: the states this edge was NOT attempted in, with the reason — the
        # loop-engine table reads `skipped` next to `cells` (real coverage, not grid size)
        "skipped": list(skipped or []),
        "outside_states": OUTSIDE_STATES.get(edge),
        "cells": [{"state": o.cell.agent_state, "caller": o.cell.caller, "vis": o.cell.visibility,
                   "body": o.cell.body, "status": o.status, "code": o.code,
                   "class": o.classify(), "gates": [g.name for g in o.resolution.gates],
                   "expected": o.resolution.expected.code if o.resolution.expected else None,
                   "unordered": list(o.resolution.unordered),
                   "registered": rm.REGISTERED_PAIRS.get((o.cell.graph, o.resolution.unordered))}
                  for o in observations],
    }
    with Path(REPORT).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _params() -> list[Any]:
    out = []
    for pairing, (_graph_b, graphs) in PAIRINGS.items():
        for graph in graphs:
            for edge in GRAPHS[graph].edges:
                if edge.is_refusal and edge.id not in SKIP_EDGES:
                    out.append(pytest.param(pairing, graph, edge,
                                            id=f"{pairing}/{graph}.{edge.id}"))
    return out


@pytest.mark.parametrize("pairing,graph,edge", _params())
def test_refusal_matrix(pairing, graph, edge, request, app, admin, ada, bob, agent_hooks,
                        monkeypatch):
    fixtures: dict[str, Any] = {"app": app, "agent_hooks": agent_hooks, "monkeypatch": monkeypatch}
    fixtures["clients"], fixtures["ada_id"] = _clients(admin, ada, bob)
    if pairing == "agent_x_chat":
        fixtures["chat_producer"] = request.getfixturevalue("chat_producer")
    if pairing == "agent_x_canary":
        fixtures["seams"] = request.getfixturevalue("seams")
    x = make_cross(pairing, fixtures)
    try:
        skipped: list[dict[str, str]] = []
        observations = drive(x, pairing, graph, edge, skipped)
    finally:
        x.close()
    _report(pairing, graph, edge.id, observations, skipped)
    summary = rm.render(observations)
    assert summary["leaks"] == [], ("hidden id leaked", summary["leaks"], x.trace[-6:])
    assert summary["unexpected"] == [], ("unexpected answer", summary["unexpected"], x.trace[-6:])
    # an unordered pair no register row records yet is a NEW DOC gap: fail with the cells so
    # the round registers it (dedupe by the pair's repro-hash)
    new = {k: v for k, v in summary["unordered"].items() if not v["registered"]}
    assert new == {}, ("unregistered unordered gate pair(s)", new)


def test_matrix_vocabulary_is_consistent():
    """Every edge the drivers attempt resolves; the known-order table names real gates."""
    names = {rm.HIDDEN, rm.DELETED, rm.DEPLOYING, rm.OWNERSHIP, rm.BODY, rm.BASE}
    for a, b in rm.KNOWN_ORDER:
        assert {a, b} <= names
    for _pairing, (_g, graphs) in PAIRINGS.items():
        for graph in graphs:
            for edge in GRAPHS[graph].edges:
                if edge.is_refusal and edge.id not in SKIP_EDGES | set(OUTSIDE_STATES):
                    cell = rm.Cell("p", graph, edge.id, "redeploying", "bob", "isolated", "valid")
                    res = rm.resolve(cell, (edge.expect.get("status"), edge.expect.get("code")),
                                     owned_by_caller=False)
                    if graph == "agent" and edge.id.startswith("create"):
                        # a NAME has no hidden id: the create refusals keep their own answer
                        assert all(g.name != rm.HIDDEN for g in res.gates), (graph, edge.id)
                        continue
                    assert res.gates and res.gates[0].name == rm.HIDDEN, (graph, edge.id)
                    assert res.expected is not None and res.expected.name == rm.HIDDEN
                    cell = rm.Cell("p", graph, edge.id, "active", "bob", "isolated", "empty")
                    res = rm.resolve(cell, (edge.expect.get("status"), edge.expect.get("code")),
                                     owned_by_caller=False)
                    if edge.id in rm.BODY_ROUTES.get(graph, set()):
                        assert res.gates[0].name == rm.BODY and res.expected.name == rm.BODY
    assert uuid  # the module is imported for the sibling worlds' tags


def test_v1_row_follows_the_chain():
    """rev-r17 on cn/r17-j: the api.md "Refusal precedence" `/v1` row (en + zh) lists the codes in
    the order services/invoke.py answers them — session_ended before the policy gate."""
    repo = Path(__file__).resolve().parents[3]
    for doc in ("docs/api.md", "docs/api.zh-CN.md"):
        text = (repo / doc).read_text(encoding="utf-8")
        row = next(line for line in text.splitlines()
                   if line.startswith("| `/v1/agents/{id}/invoke[-stream]` |"))
        positions = [row.index(code) for code in rm.V1_CHAIN_ORDER]
        assert positions == sorted(positions), (doc, row)
