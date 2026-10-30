"""The refusal × non-happy-state matrix (round 4 of the loop engine, r17-i).

Every refusal edge of a lifecycle graph names ONE reason to refuse. The product, however, is
asked while several reasons hold at once: the agent is `deploying` AND the caller is another
member AND the workspace is `isolated` AND the body is empty. Rounds 1–3 kept finding that the
contracts enumerate refusals but never order them (N17-obs12 / 15 / 17 / 30). This module makes
that systematic: a **gate** is one contract-cited reason with the answer it names; a **cell** is
one refusal edge attempted under one combination of perturbations; ``resolve`` says which gates
apply to the cell and, where the contracts (or an already registered row) order them, which
answer is expected. A cell whose applicable gates are NOT ordered anywhere is an **unordered
pair** — a DOC gap to register once per pair (deduped by ``pair_hash``); a cell where a hidden
id answers anything but the kind's 404 is a **leak** (PRODUCT); a cell whose refusal writes is
caught by the executor's digest. The driver lives in ``test_graph_refusal_matrix.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# perturbations
# ---------------------------------------------------------------------------

AGENT_STATES = ("active", "redeploying", "deleted")  # reached through the agent graph's edges
#: round 5 (r17-k): the agent graph's OWN refusals at `absent` / `deploying` (a FIRST deploy in
#: flight) / `redeploying_cold` (a re-publish from `failed`) run on a SECOND fresh agent the
#: driver stages per cell set — every refusal edge of the agent graph has cells now
SECOND_AGENT_STATES = ("absent", "deploying", "redeploying_cold")
LIVE_JOB_STATES = ("redeploying", "deploying", "redeploying_cold")  # agents.md §1: a job owns it
CALLERS = ("ada", "bob", "admin")  # creator · other member · administrator
VISIBILITY = ("shared", "isolated")
BODIES = ("valid", "empty")


@dataclass(frozen=True)
class Cell:
    pairing: str
    graph: str
    edge: str
    agent_state: str
    caller: str
    visibility: str
    body: str

    @property
    def label(self) -> str:
        return (f"{self.graph}.{self.edge} | agent {self.agent_state} · {self.caller} · "
                f"{self.visibility} · body {self.body}")


@dataclass(frozen=True)
class Gate:
    name: str
    status: int
    code: str
    cite: str


@dataclass
class Resolution:
    gates: list[Gate] = field(default_factory=list)  # applicable, most specific first
    expected: Gate | None = None  # when the applicable gates are ordered by a contract / row
    unordered: tuple[str, ...] = ()  # gate names whose relative order nothing writes
    base: tuple[int | None, str | None] = (None, None)  # the edge's own expectation


# ---------------------------------------------------------------------------
# the gates — every one cites the sentence that names its answer
# ---------------------------------------------------------------------------

HIDDEN = "hidden"          # users-and-ownership.md §7
DELETED = "agent_deleted"  # api.md Console Agents API › Error codes
DEPLOYING = "agent_deploying"  # agents.md §1 / N16-obs74 ruling / agents.md §1.2
OWNERSHIP = "ownership"    # users-and-ownership.md §4 / §8, N16-obs70/71 ruling
BODY = "body"              # the platform envelope: a body the schema rejects is 422
BASE = "base"              # the refusal edge's own contract answer

#: Gate pairs whose order IS written — by a contract sentence (`contract`) or by a register row
#: that recorded the code's order as a DOC gap (`row`). Any other pair of applicable gates is an
#: unordered pair, i.e. a new DOC gap.
KNOWN_ORDER: dict[tuple[str, str], str] = {
    (HIDDEN, DELETED): "contract: §7 — a hidden id answers the kind's not-found on every route",
    (HIDDEN, DEPLOYING): "contract: §7",
    (HIDDEN, OWNERSHIP): "contract: §7 (the hidden id's 404 replaces the 403)",
    (HIDDEN, BODY): "contract: §7 — before any write / validation",
    (HIDDEN, BASE): "contract: §7",
    (DELETED, OWNERSHIP): "contract: api.md — a deleted agent answers 404 on every route exactly "
                          "like /v1",
    (DELETED, DEPLOYING): "contract: a deleted row is never deploying",
    (OWNERSHIP, BASE): "contract: §8 — creator-or-admin before any side effect",
    # api.md "Refusal precedence" (cn/r17-j ruling): ownership-first everywhere — a non-owner
    # learns no state; the chat route was reordered for it (N17-obs12, PRODUCT)
    (OWNERSHIP, DEPLOYING): "contract: api.md 'Refusal precedence' — ownership before the "
                            "status / deploy-job gates on every route",
    # a body the schema rejects never reaches the handler: the 422 precedes every handler
    # gate and is the same answer for any id (framework; api.md "Errors use the platform
    # envelope") — N17-obs17's "status before body" was the SEMANTIC empty-evaluators check
    (BODY, HIDDEN): "framework: schema validation precedes the handler",
    (BODY, DELETED): "framework: schema validation precedes the handler",
    (BODY, DEPLOYING): "framework: schema validation precedes the handler",
    (BODY, OWNERSHIP): "framework: schema validation precedes the handler",
    (BODY, BASE): "framework: schema validation precedes the handler",
}


def known_order(first: str, second: str, graph: str, edge: str) -> bool:
    """Is `first` written to answer before `second` on this route? `KNOWN_ORDER` holds the
    route-independent pairs; the status gate's precedence over a route's OWN refusal is known
    only where a register row / the api.md precedence table recorded it (chat sends: status
    before the session's own state; N17-obs15: the canary create) — elsewhere it is an
    observation the matrix records (round 4)."""
    if (first, second) in KNOWN_ORDER:
        return True
    if (first, second) == (BASE, DEPLOYING) and edge in OWNERSHIP_EDGES:
        # the edge's own answer IS an ownership refusal: ownership-first (api.md "Refusal
        # precedence", cn/r17-j — N17-obs12 fixed, N17-obs51 written)
        return True
    if (first, second) == (DEPLOYING, BASE):
        return (graph == "chat_session" and edge in (CHAT_SENDS | CHAT_V1)) or \
            (graph == "runtime_canary" and edge in CANARY_CREATE) or \
            (graph == "eval_run" and edge.startswith("create"))
    if (first, second) == (DELETED, BASE):
        # api.md: "every /api/agents/{id}* and /api/chat/{id}* route answers a deleted row 404";
        # the eval / canary CREATE gate is §23 / E13 §3 — the rerun route is round 4's finding
        return graph in ("agent", "chat_session") or edge.startswith("create")
    return False


#: Unordered pairs a register row already records — the report tags them with the id; a pair
#: not listed here is a NEW DOC gap when the matrix observes it.
#: The shared invoke chain's own gate order (services/invoke.py, both entrances): the agent status,
#: then the ended-session 409 (N16-obs88 ruling: before any AWS call). The former policy gate's
#: 403 `policy.denied` left the chain with the Global parity ruling (2026-10-27).
#: api.md "Refusal precedence" must list the /v1 row in this order — test_v1_row_follows_the_chain.
V1_CHAIN_ORDER: tuple[str, ...] = ("agent.not_active", "chat.session_ended")

#: Since cn/r17-j every registered pair is also WRITTEN: api.md "Refusal precedence — the gate
#: order per route group" is the one table the six DOC rows (N17-obs12 / 15 / 17 / 30 / 50 / 51)
#: resolve to (docs follow code); the ids stay so the report keeps naming the row.
PRECEDENCE_TABLE = "api.md 'Refusal precedence'"
REGISTERED_PAIRS: dict[tuple[str, tuple[str, ...]], str] = {
    ("eval_run", (DEPLOYING, BASE)): f"N17-obs50 — {PRECEDENCE_TABLE}",
    ("eval_run", (DELETED, BASE)): f"N17-obs50 — {PRECEDENCE_TABLE}",
    ("agent", (DEPLOYING, OWNERSHIP, BASE)): f"N17-obs51 — {PRECEDENCE_TABLE}",
    ("agent", (DEPLOYING, OWNERSHIP)): f"N17-obs51 — {PRECEDENCE_TABLE}",
}


def kind_not_found(graph: str, edge: str) -> tuple[int, str]:
    """The kind's existing not-found a hidden id answers (users-and-ownership.md §7) — for the
    create routes the hidden AGENT is what the body names, so the answer is the create gate's
    400 (`a hidden agent → 400 agent.not_active`, evaluation-engine.md §23 / E13 §3)."""
    if graph in ("agent", "chat_session"):
        return 404, "agent.not_found"
    if graph == "runtime_canary":
        return (400, "canary.agent_not_active") if edge.startswith("create") \
            else (404, "canary.not_found")
    if graph == "eval_run":
        if edge.startswith("create"):
            return 400, "agent.not_active"
        if edge.startswith("dataset_"):
            return 404, "dataset.not_found"
        return 404, "run.not_found"
    raise KeyError(graph)


# which refusal edges are "sends" (chat), "publishes" (canary), creates …
CHAT_SENDS = {"send_while_live", "send_ended", "other_member_post", "first_turn"}
CHAT_V1 = {"v1_send_ended"}
CANARY_PUBLISH = {"complete_before_final", "complete_before_final_s1", "advance_at_final"}
CANARY_CREATE = {"create_already_running", "create_inactive_agent"}
EVAL_CREATE_LIKE = {"create_inactive_agent", "create_no_evaluators", "rerun_sessions_scope",
                    "rerun_dataset_gone", "rerun_deleted"}
AGENT_MUTATIONS = {"member_redeploy_other", "member_delete_other", "delete_while_deploying",
                   "redeploy_while_deploying", "delete_while_redeploying",
                   "delete_while_cold_redeploying", "redeploy_deleted", "delete_deleted"}
BODY_ROUTES = {  # edges whose request carries a JSON body the schema can reject
    "chat_session": CHAT_SENDS | CHAT_V1,
    "runtime_canary": CANARY_CREATE | {"traffic_without_dataset", "setup_twice",
                                       "verdict_before_traffic", "advance_before_verdict",
                                       "other_member_advance", "complete_before_final",
                                       "action_after_complete", "action_after_rollback",
                                       "advance_at_final"},
    "eval_run": {"create_inactive_agent", "create_no_evaluators"},
    "agent": {"member_redeploy_other", "redeploy_while_deploying", "redeploy_deleted"},
}
# bob's own refusals: the ownership gate IS the base answer there
OWNERSHIP_EDGES = {"member_redeploy_other", "member_delete_other", "other_member_history",
                   "other_member_stop", "other_member_post", "other_member_reattach",
                   "other_member_advance", "other_member_advance_s1", "other_member_advance_s2"}


def gates_for(cell: Cell, base: tuple[int | None, str | None], *,
              owned_by_caller: bool) -> list[Gate]:
    """The applicable gates of a cell, most specific first (the order the contracts write)."""
    out: list[Gate] = []
    g, e = cell.graph, cell.edge
    if cell.body == "empty" and e in BODY_ROUTES.get(g, set()):
        out.append(Gate(BODY, 422, "validation",
                        "api.md: the platform envelope — a body the schema rejects is 422, "
                        "before the handler"))
    if cell.caller == "bob" and cell.visibility == "isolated" and not owned_by_caller \
            and not (g == "agent" and e.startswith("create")):
        # the agent graph's create refusals address a NAME, not a hidden id (§7 is about ids);
        # whether the duplicate-name 409 leaks the hidden row's id is `name_leak`'s check
        status, code = kind_not_found(g, e)
        out.append(Gate(HIDDEN, status, code,
                        "users-and-ownership.md §7: a hidden id answers the kind's existing "
                        "not-found"))
    if cell.agent_state == "deleted":
        if g in ("agent", "chat_session"):
            out.append(Gate(DELETED, 404, "agent.not_found",
                            "api.md Console Agents API › Error codes: a deleted agent answers "
                            "404 on every /api/agents/{id}* and /api/chat/{id}* route"))
        elif g == "eval_run" and e in EVAL_CREATE_LIKE:
            out.append(Gate(DELETED, 400, "agent.not_active",
                            "evaluation-engine.md §23: the same gate as POST /api/eval/runs"))
        elif g == "runtime_canary" and e in CANARY_CREATE:
            out.append(Gate(DELETED, 400, "canary.agent_not_active", "E13 §3"))
    # api.md 'Refusal precedence' (ownership-first, cn/r17-j): the creator-or-admin gate / the
    # personal session's 404 is listed BEFORE the status gates, after the hidden / deleted 404s.
    # An OWNERSHIP_EDGES edge's own answer IS the ownership refusal — it takes that slot too.
    status, code = base
    base_listed = False
    if cell.caller == "bob" and not owned_by_caller and e in OWNERSHIP_EDGES \
            and status is not None:
        out.append(Gate(BASE, status, code or "", "the refusal edge's own contract sentence "
                        "(an ownership refusal: ordered before the status gates)"))
        base_listed = True
    if cell.caller == "bob" and not owned_by_caller and e not in OWNERSHIP_EDGES:
        if g == "agent" and e in AGENT_MUTATIONS:
            out.append(Gate(OWNERSHIP, 403, "agent.forbidden",
                            "users-and-ownership.md §4: agent redeploy / delete are "
                            "creator-or-admin"))
        elif g == "chat_session":
            out.append(Gate(OWNERSHIP, 404, "chat.session_not_found",
                            "N16-obs70/71 ruling: chat sessions are personal in both modes"))
        elif g == "runtime_canary" and not e.startswith("create") \
                and not e.startswith("verdict") and not e.startswith("member_verdict"):
            out.append(Gate(OWNERSHIP, 403, "canary.forbidden",
                            "users-and-ownership.md §8: every canary action but verdict is "
                            "creator-or-admin"))
        elif g == "eval_run" and e.startswith("dataset_"):
            out.append(Gate(OWNERSHIP, 403, "dataset.forbidden",
                            "users-and-ownership.md §8: dataset PUT / DELETE are creator-or-admin"))
    if cell.agent_state in LIVE_JOB_STATES:
        if g == "chat_session" and e in (CHAT_SENDS | CHAT_V1):
            out.append(Gate(DEPLOYING, 409, "agent.not_active",
                            "N16-obs74 ruling / api.md: a non-active agent refuses the turn"))
        elif g == "agent" and e in AGENT_MUTATIONS:
            out.append(Gate(DEPLOYING, 409, "agent.deploy_in_progress",
                            "agents.md §1: a deploy job owns its agent until it is terminal"))
        elif g == "eval_run" and e in EVAL_CREATE_LIKE:
            out.append(Gate(DEPLOYING, 400, "agent.not_active", "evaluation-engine.md §23"))
        elif g == "runtime_canary" and e in CANARY_CREATE:
            out.append(Gate(DEPLOYING, 400, "canary.agent_not_active", "E13 §3"))
        elif g == "runtime_canary" and e in CANARY_PUBLISH and e != "advance_at_final":
            out.append(Gate(DEPLOYING, 409, "agent.deploy_in_progress",
                            "agents.md §1.2 (N17-obs13): a publish under a live deploy job"))
    if status is not None and not base_listed:
        out.append(Gate(BASE, status, code or "", "the refusal edge's own contract sentence"))
    return out


def resolve(cell: Cell, base: tuple[int | None, str | None], *,
            owned_by_caller: bool) -> Resolution:
    gates = gates_for(cell, base, owned_by_caller=owned_by_caller)
    res = Resolution(gates=gates, base=base)
    if not gates:
        return res
    if len(gates) == 1:
        res.expected = gates[0]
        return res
    first = gates[0]
    others = gates[1:]
    if all((o.status, o.code) == (first.status, first.code) for o in others):
        res.expected = first  # every applicable gate names the same answer: nothing to order
        return res
    if all(known_order(first.name, o.name, cell.graph, cell.edge) for o in others):
        res.expected = first
    else:
        res.unordered = tuple(g.name for g in gates if g is not first
                              and not known_order(first.name, g.name, cell.graph, cell.edge))
        res.unordered = (first.name, *res.unordered)
    return res


def pair_hash(graph: str, pair: tuple[str, ...], observed_code: str) -> str:
    """The register's `repro-hash` for an unordered gate pair on a route family."""
    text = f"{graph}: {' > '.join(pair)} -> observed {observed_code}"
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


@dataclass
class Observation:
    cell: Cell
    status: int
    code: str | None
    resolution: Resolution

    @property
    def gate_codes(self) -> set[str]:
        return {g.code for g in self.resolution.gates}

    def classify(self) -> str:
        """`ok` · `unordered` (a DOC gap candidate) · `leak` (PRODUCT: a hidden id answered
        something else) · `unexpected` (none of the applicable gates' answers)."""
        gates = self.resolution.gates
        hidden = next((g for g in gates if g.name == HIDDEN), None)
        if hidden is not None and (self.status, self.code) != (hidden.status, hidden.code) \
                and not (gates[0].name == BODY and self.status == 422):
            return "leak"
        if self.resolution.expected is not None:
            exp = self.resolution.expected
            if (self.status, self.code) == (exp.status, exp.code) or (
                    exp.name == BODY and self.status == 422):
                return "ok"
            return "unexpected"
        if self.resolution.unordered:
            codes = self.gate_codes
            if self.code in codes or (self.status == 422 and "validation" in codes):
                return "unordered"
            return "unexpected"
        return "ok" if 400 <= self.status < 500 else "unexpected"


def render(observations: list[Observation]) -> dict[str, Any]:
    """Counts per class, the unordered gate pairs (with the winner the product chose) and the
    leaks — what the report / register rows are written from."""
    out: dict[str, Any] = {"cells": len(observations), "by_class": {}, "unordered": {}, "leaks": [],
                           "unexpected": []}
    for o in observations:
        c = o.classify()
        out["by_class"][c] = out["by_class"].get(c, 0) + 1
        if c == "unordered":
            winner = next((g.name for g in o.resolution.gates if g.code == o.code
                           or (g.name == BODY and o.status == 422)), "?")
            key = (o.cell.graph, o.resolution.unordered)
            slot = out["unordered"].setdefault(key, {"winner": winner, "cells": [],
                                                      "registered": REGISTERED_PAIRS.get(key),
                                                      "hash": pair_hash(o.cell.graph,
                                                                        o.resolution.unordered,
                                                                        o.code or str(o.status))})
            slot["cells"].append(o.cell.label)
        elif c == "leak":
            out["leaks"].append((o.cell.label, o.status, o.code))
        elif c == "unexpected":
            out["unexpected"].append((o.cell.label, o.status, o.code,
                                      [(g.name, g.code) for g in o.resolution.gates]))
    return out
