# ruff: noqa: F811  — pytest fixtures are imported by name from the sibling suites
"""Cross-graph walks: two lifecycle graphs interleaved on one shared entity (round 17, r17-b).

Three pairings (``tests/graph/crossgraph.py``): the Runtime-canary graph and the chat-session
graph on the agent the agent graph created (its delete injected in the middle of the other
graph's walk), and the evaluation-run graph with an administrator's ``?force=true`` dataset
delete injected while the run is ACTIVE. Every side keeps its own world and its own graph
expectations; the composite adds the cross rules the contracts name:

* ``DELETE /api/agents/{id}`` → 409 ``agent.in_use`` while a canary on it is `running`
  (agents.md §1.1), else 200 — the delete's refusals are enumerated (403 forbidden, 409
  deploy_in_progress, 409 in_use), so a live chat turn does not refuse it;
* after the agent is deleted, every ``/api/chat/{id}*`` route and ``/v1`` answer 404
  ``agent.not_found`` (api.md "Error codes") and write nothing;
* after the forced dataset delete, a new run / a rerun on it is the dataset kind's 404
  (evaluation-engine.md §23 "the same gate as POST /api/eval/runs … a hidden dataset → 404";
  api.md "the kind's existing not-found"), the member's `force` is 403 (§22), and the run
  already queued keeps its own copy of the items (§22) — so the dispatch still completes.

After EVERY step: both worlds' own-id ledger scans and the status-aware
``dangling_references`` oracle.
"""

from __future__ import annotations

import os
import random
import threading
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.evaluation.routers as eval_routers
import app.evaluation.service as svc
import app.routers.agents as agents_router
import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import pipeline
from app.deployer.pipeline import STAGE_ORDER, StageResult
from app.models.ledger import Agent
from app.services import knowledge_backend, live_turns, memory_backend
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from tests.conftest import set_default_resources, ws_ctx
from tests.graph import coverage
from tests.graph.crossgraph import (
    PAIRINGS,
    Plan,
    RegisteredDangling,
    RegisteredSymptom,
    cross_seeds,
    dangling_references,
    pick,
    plan,
)
from tests.graph.executor import GRAPHS, ledger_digest
from tests.graph.paths import Edge
from tests.graph.test_graph_agent import AgentWorld, _write
from tests.graph.test_graph_chat_session import BUDGET_S, ChatWorld, StopPlane, _frames
from tests.graph.test_graph_eval_run import ITEMS, EvalRunWorld, HeldQueue
from tests.graph.test_graph_runtime_canary import CanaryWorld
from tests.graph.walks import repro_hash
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.model.test_canary_state_machine import seams  # noqa: F401 — the canary seams
from tests.property.test_chat_concurrency import Producer
from tests.test_memory_selfhosted import SUBSTRATE, T0
from tests.test_visibility import PEER, _dataset, ada, admin, app, bob  # noqa: F401

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
AGENT_LIVE_NODES = ("active", "failed")  # where DELETE is a graph edge, not a 409 deploy guard
AGENT_JOB_NODES = ("deploying", "redeploying", "redeploying_cold")  # a deploy job is live
CANARY_SEEDS = frozenset({"seed_running", "create_after_cleaned"})  # test-side seeding edges
CHAT_SKIP_AFTER_DELETE = frozenset({"invalid_session_id", "turn_completes"})
CHAT_SENDS = frozenset({"first_turn", "next_turn", "console_turn_after_v1", "send_while_live",
                        "send_ended"})
CHAT_V1 = frozenset({"v1_turn", "v1_turn_again", "v1_send_ended"})

#: Registered cross-graph findings: ``(register id, predicate(cross, side, edge), reason)``.
#: A walk xfails only when its failing step matches a predicate AND the failure is the
#: registered symptom itself (a ``RegisteredSymptom``: the observed status the register row
#: names). A 409 that still writes, a dangling-reference trip or any other oracle failure at
#: the same step is NOT the finding and fails with its trace + repro-hash.
PINS: list[tuple[str, Any, str]] = [  # N17-obs13 / obs16 / obs18 fixed
]
#: a dedicated pin test takes its `reason` from here by register id, never by position
#: (rev-r17: `PINS[1][2]` became an IndexError the day one pin was removed)
PIN_REASONS: dict[str, str] = {rid: reason for rid, _match, reason in PINS}
#: Dangling-reference violations a register row already names (substring of the scan line);
#: a walk whose oracle trips on one of these xfails on the id (``Cross.check`` raises
#: ``RegisteredDangling``); any other dangling line is a NEW finding.
REGISTERED_DANGLING: dict[str, str] = {}  # N17-obs18 / obs31 fixed — no registered oracle line
#: agents.md §1.2 (N17-obs13): the canary actions that publish the champion's live version —
#: refusal self-loops under a live deploy job in every agent × canary walk
CANARY_PUBLISHES = ("complete_final", "rollback", "rollback_fresh")
#: users-and-ownership.md §7 — under `isolated` a member sees a resource only when `created_by`
#: is theirs, NULL or a current administrator's; "A hidden id answers the kind's existing
#: not-found". Every other-member edge of the pairings, with the not-found it reads once the
#: administrator flipped the workspace mid-walk (round 3, r17-g).
MEMBER_EDGES: dict[str, dict[str, str]] = {
    "agent": {"member_redeploy_other": "agent.not_found",
              "member_delete_other": "agent.not_found"},
    "chat_session": {"other_member_history": "agent.not_found",
                     "other_member_stop": "agent.not_found",
                     "other_member_post": "agent.not_found",
                     "other_member_reattach": "agent.not_found"},
    "runtime_canary": {"member_verdict": "canary.not_found",
                       "member_verdict_s1": "canary.not_found",
                       "member_verdict_s2": "canary.not_found",
                       "other_member_advance": "canary.not_found",
                       "other_member_advance_s1": "canary.not_found",
                       "other_member_advance_s2": "canary.not_found"},
}
VISIBILITY_URL = f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/settings/visibility"


# ---------------------------------------------------------------------------
# seams (local copies: the sibling modules' fixtures share the name `seams`)
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_hooks(monkeypatch):
    """The agent world's seams (``test_graph_agent.seams``): inline deploy job with recording
    fake stages, no-op Runtime delete / residue sweep."""
    launched: list[str] = []
    hooks: dict[str, object] = {}
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    monkeypatch.setattr(pipeline, "start_deploy_async", lambda jid: launched.append(jid))

    def stage(ctx, agent, name):
        hook = hooks.get(name)
        if hook is not None:
            hook(ctx, agent)
        if name == "deploy":
            _write(agent.id, resource_id=f"rt{agent.id[:10]}",
                   arn=f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/"
                       f"rt{agent.id[:10]}")
        return StageResult(detail=f"{name} done (fake)")

    monkeypatch.setitem(
        pipeline._METHODS, "zip_runtime",
        {s: (lambda ctx, agent, _s=s: stage(ctx, agent, _s)) for s in STAGE_ORDER},
    )
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, ws: None)
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    # BELOW the shared invoke chain (rev-r17): the data-plane wrapper answers, so the chat
    # route runs the real `invoke_agent_events` with every gate the chain owns
    monkeypatch.setattr(
        invoke_service.rt, "stream_runtime_events",
        lambda _client, _arn, prompt, **_kw: iter([{"event": "delta", "data": {"text": "ok"}}]),
    )
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: StopPlane())
    return hooks


@pytest.fixture
def chat_producer(monkeypatch, agent_hooks):
    """The chat world's seams on top of the agent's (requested after ``agent_hooks`` so the
    gate-able producer wins the ``invoke_agent_events`` seam)."""
    producer = Producer()
    plane = StopPlane()
    monkeypatch.setattr(chat_service, "invoke_agent_events", producer)
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    real_start = live_turns.LiveTurnRegistry.start

    def recording_start(registry, live, target, args, **kw):
        started = real_start(registry, live, target, args, **kw)
        producer.record(started)
        return started

    monkeypatch.setattr(live_turns.LiveTurnRegistry, "start", recording_start)
    # the `/v1` sync seam sits BELOW the shared chain too (as test_graph_chat_session.seams
    # after cn/r17-a): the route runs the real `invoke_agent_text` — the ended-session 409 of
    # N16-obs88 included — and the stub only answers the runtime call
    monkeypatch.setattr(
        invoke_service.rt, "invoke_runtime_text",
        lambda _client, _arn, prompt, *, session_id, **_kw: {
            "text": f"v1 says {prompt}", "session_id": session_id,
        },
    )
    return producer, plane


# ---------------------------------------------------------------------------
# the composite
# ---------------------------------------------------------------------------


class Cross:
    """Two worlds, one plan. Subclasses set the graphs, the shared-entity rules and the
    injected delete; ``run`` drives the plan with the oracles after every step."""

    pairing: str = ""
    graph_a: str = ""
    graph_b: str = ""

    def __init__(self, clients: dict[str, TestClient], seed: int) -> None:
        self.clients = clients
        self.plan: Plan = plan(self.pairing, seed)
        self.rng = random.Random(f"{self.pairing}:{seed}:edges")
        self.node_a = GRAPHS[self.graph_a].initial
        self.node_b = GRAPHS[self.graph_b].initial
        self.injected = False
        self.redeployed = False
        self.isolated = False
        self.flipped = 0  # 0 → 1 (isolated) → 2 (back to shared)
        self.current: tuple[str, Edge] | None = None
        self.trace: list[str] = []
        self.a: Any = None
        self.b: Any = None

    # -- helpers ---------------------------------------------------------------------
    @property
    def admin(self) -> TestClient:
        return self.clients["admin"]

    @property
    def ada(self) -> TestClient:
        return self.clients["ada"]

    def note(self, text: str) -> None:
        self.trace.append(text)

    def expect(self, res: Any, status: int, code: str | None = None, *, what: str,
               symptom: int | None = None) -> dict | None:
        """Assert the contract's status / code. ``symptom`` is the status a register row
        names as the observed deviation: that exact answer raises ``RegisteredSymptom`` (what
        a pin may xfail on); any other mismatch is an ordinary failure."""
        if symptom is not None and res.status_code == symptom and res.status_code != status:
            raise RegisteredSymptom((what, res.status_code, res.text[:400]))
        assert res.status_code == status, (what, res.status_code, res.text[:400])
        body = res.json() if res.text else None
        if code is not None:
            assert body is not None and body.get("code") == code, (what, body)
        return body

    def refusal(self, fn, *, what: str):
        """Run a refused call and assert it wrote nothing (the executor's digest oracle)."""
        before = ledger_digest()
        out = fn()
        after = ledger_digest()
        changed = sorted(k for k in after if before.get(k) != after[k])
        assert not changed, (f"refused {what!r} changed ledger table(s): {changed}")
        return out

    def enabled(self, side: str) -> list[Edge]:
        graph = GRAPHS[self.graph_a if side == "a" else self.graph_b]
        world = self.a if side == "a" else self.b
        node = self.node_a if side == "a" else self.node_b
        edges = [e for e in graph.out_edges(node) if e.id not in world.XFAIL]
        return self.filter(side, edges)

    def filter(self, side: str, edges: list[Edge]) -> list[Edge]:
        return edges

    def ids(self) -> dict[str, set[str]]:
        return {"agent_ids": set(), "canary_ids": set(), "run_ids": set()}

    def agent_status(self, agent_id: str) -> str:
        db = SessionLocal()
        try:
            return db.get(Agent, agent_id).status
        finally:
            db.close()

    def forced_datasets(self) -> set[str]:
        return set()

    def check(self, what: str) -> None:
        for world in (self.a, self.b):
            world.check_invariants(Edge(id=what, src="", dst="", cites=("x",)))
        notes: list[str] = []
        dangling = dangling_references(SessionLocal, forced_datasets=self.forced_datasets(),
                                       notes=notes, **self.ids())
        for text in notes:
            if text not in self.trace:
                self.note(text)
        if dangling and all(any(k in line for k in REGISTERED_DANGLING.values())
                            for line in dangling):
            raise RegisteredDangling((what, dangling, self.trace[-6:]))
        assert dangling == [], (what, dangling, self.trace[-6:])

    # -- round 3: the visibility flip and the hidden-id rule -----------------------------------
    def flip(self, to: str) -> None:
        """users-and-ownership.md §7: the administrator sets `resource_visibility`; members
        cannot (403). The next step of a member walker answers per the mode at that step."""
        res = self.admin.put(VISIBILITY_URL, json={"resource_visibility": to})
        assert res.status_code == 200 and res.json()["resource_visibility"] == to, res.text
        self.isolated = to == "isolated"
        self.flipped += 1
        self.note(f"inject: visibility → {to}")

    def member_call(self, side: str, edge: Edge) -> Any:
        """The other member's (bob's) raw request for a MEMBER_EDGES edge of the side's graph."""
        bob = self.clients["bob"]
        world = self.a if side == "a" else self.b
        graph = self.graph_a if side == "a" else self.graph_b
        if graph == "agent":
            if edge.id == "member_redeploy_other":
                return bob.post(f"/api/agents/{world.agent_id}/redeploy", json=world.spec())
            return bob.delete(f"/api/agents/{world.agent_id}")
        if graph == "chat_session":
            base, sid = world.base, world.sid
            if edge.id == "other_member_post":
                return world.post(bob)
            if edge.id == "other_member_stop":
                return bob.post(f"{base}/sessions/{sid}/stop")
            if edge.id == "other_member_history":
                return bob.get(f"{base}/history", params={"session_id": sid})
            return bob.get(f"{base}/sessions/{sid}/turns/1/stream")
        if graph == "runtime_canary":
            action = "verdict" if edge.id.startswith("member_verdict") else "advance"
            return world.act(action, client=bob)
        raise AssertionError((graph, edge.id))

    def member_hidden(self, side: str, edge: Edge) -> None:
        graph = self.graph_a if side == "a" else self.graph_b
        code = MEMBER_EDGES[graph][edge.id]
        what = f"{edge.id} by the other member under isolated visibility"
        self.refusal(lambda: self.expect(self.member_call(side, edge), 404, code, what=what),
                     what=what)

    # -- the plan ----------------------------------------------------------------------
    def setup(self) -> None:
        raise NotImplementedError

    def inject(self) -> bool:
        """Attempt the injected delete; True when it fired (else retried next step)."""
        raise NotImplementedError

    def inject_redeploy(self) -> bool:
        """Round 2: attempt the injected redeploy of the shared agent; True when it fired.
        Pairings without a redeploy injection return True at once."""
        return True

    def step_a(self, edge: Edge) -> None:
        self.a.step(edge)
        self.node_a = edge.dst

    def step_b(self, edge: Edge) -> None:
        self.b.step(edge)
        self.node_b = edge.dst

    def run(self) -> None:
        self.setup()
        self.check("setup")
        for i, side in enumerate(self.plan.sides):
            if i >= self.plan.inject_at and not self.injected:
                if self.inject():
                    self.injected = True
                    self.check("inject")
                    continue
            if 0 <= self.plan.redeploy_at <= i and not self.redeployed:
                if self.inject_redeploy():
                    self.redeployed = True
                    self.check("inject_redeploy")
                    continue
            if 0 <= self.plan.flip_at <= i and self.flipped == 0:
                self.flip("isolated")
                self.check("flip")
                continue
            if 0 <= self.plan.flip_back_at <= i and self.flipped == 1:
                self.flip("shared")
                self.check("flip_back")
                continue
            edges = self.enabled(side)
            if not edges:
                side = "b" if side == "a" else "a"
                edges = self.enabled(side)
                if not edges:
                    self.note("both sides exhausted")
                    break
            edge = pick(self.rng, edges)
            self.note(f"{side}:{edge.id}")
            self.current = (side, edge)
            graph = self.graph_a if side == "a" else self.graph_b
            if self.isolated and edge.id in MEMBER_EDGES.get(graph, {}):
                self.member_hidden(side, edge)  # §7: the hidden id's not-found; node unchanged
            else:
                (self.step_a if side == "a" else self.step_b)(edge)
            self.check(f"{side}:{edge.id}")
        coverage.record_path("reference", self.plan.id)

    def close(self) -> None:
        for world in (self.a, self.b):
            if world is not None:
                world.close()
        if self.isolated:  # never leave the worker's workspace isolated for the next test
            self.admin.put(VISIBILITY_URL, json={"resource_visibility": "shared"})


# ---------------------------------------------------------------------------
# agent × runtime canary
# ---------------------------------------------------------------------------


class AgentCanary(Cross):
    pairing = "agent_x_canary"
    graph_a = "agent"
    graph_b = "runtime_canary"

    def __init__(self, clients, seed, hooks, ada_id, canary_seams) -> None:
        super().__init__(clients, seed)
        self.hooks, self.ada_id, self.canary_seams = hooks, ada_id, canary_seams

    def setup(self) -> None:
        self.a = AgentWorld(self.clients, self.hooks)
        graph = GRAPHS["agent"]
        self.a.step(graph.edge("create"))
        self.a.step(graph.edge("deploy_succeeds"))
        self.node_a = "active"
        self.shared_agent = self.a.agent_id
        self.b = CanaryWorld(self.clients, self.ada_id, self.canary_seams,
                             _dataset(None, name=f"x-canary-ds-{uuid.uuid4().hex[:6]}"),
                             agent_id=self.shared_agent)

    def ids(self) -> dict[str, set[str]]:
        canaries = {self.b.canary_id} if self.b.canary_id else set()
        return {"agent_ids": {self.shared_agent}, "canary_ids": canaries, "run_ids": set()}

    def agent_deleted(self) -> bool:
        return self.agent_status(self.shared_agent) == "deleted"

    def filter(self, side: str, edges: list[Edge]) -> list[Edge]:
        if side == "a" and not self.injected:
            edges = [e for e in edges if e.id not in ("delete_active", "delete_failed")]
        if side == "b" and self.agent_deleted():
            edges = [e for e in edges if e.id not in CANARY_SEEDS]
        return edges

    def canary_running(self) -> bool:
        return self.b.canary_id is not None and self.b.row().status == "running"

    def cross_delete(self, edge: Edge) -> None:
        """The agent's DELETE under the canary guard (agents.md §1.1): 409 `agent.in_use`
        for members and administrators alike while the canary runs — nothing written; else
        the agent graph's own delete edge."""
        if self.a.agent_id == self.shared_agent and self.canary_running():
            for client in (self.ada, self.admin):
                body = self.refusal(
                    lambda c=client: self.expect(
                        c.delete(f"/api/agents/{self.shared_agent}"), 409, "agent.in_use",
                        what="agent delete under a running canary"),
                    what="agent delete under a running canary")
                assert self.b.canary_id in {c["id"] for c in body["detail"]["canaries"]}, body
            assert not self.agent_deleted() and self.b.row().status == "running"
            self.note(f"delete refused (canary running at {self.node_b})")
            return
        self.a.step(edge)
        self.node_a = edge.dst
        self.note(f"agent deleted (canary at {self.node_b})")

    def cross_redeploy(self, edge: Edge) -> None:
        """agents.md §1.1 (N17-obs16, fixed in cn/r17-d): a re-publish under a running canary
        is the delete's 409 `agent.in_use {canaries}` — members and administrators alike,
        nothing written (row stays on its spec / version / status, no job, no snapshot) — so a
        later `complete` can never revert production to a candidate built on an older base;
        else the agent graph's own redeploy edge."""
        if self.a.agent_id == self.shared_agent and self.canary_running():
            status_before = self.agent_status(self.shared_agent)
            for client in (self.ada, self.admin):
                body = self.refusal(
                    lambda c=client: self.expect(
                        c.post(f"/api/agents/{self.shared_agent}/redeploy", json=self.a.spec()),
                        409, "agent.in_use", what="agent redeploy under a running canary"),
                    what="agent redeploy under a running canary")
                assert self.b.canary_id in {c["id"] for c in body["detail"]["canaries"]}, body
            assert self.agent_status(self.shared_agent) == status_before
            assert self.b.row().status == "running"
            self.note(f"redeploy refused (canary running at {self.node_b})")
            return
        self.a.step(edge)
        self.node_a = edge.dst

    def inject_redeploy(self) -> bool:
        if self.node_a != "active" or self.a.agent_id != self.shared_agent:
            return False
        edge = GRAPHS["agent"].edge("redeploy")
        self.current = ("a", edge)
        self.note("inject: redeploy")
        self.cross_redeploy(edge)
        return True

    def publish_on_stale_base(self, edge: Edge) -> None:
        """agents.md §1.2 (N17-obs16 belt): once the champion's ledger version moved past the
        one setup recorded, `complete` / `rollback` are `409 canary.stale_base {setup_version,
        current_version}` — nothing written, the canary stays `running` for `cleanup`."""
        w = self.b
        action = "complete" if edge.id == "complete_final" else "rollback"
        before = self.a.row()
        setup_version = w.row().artifacts["setup"]["agent_version"]
        assert str(before.version) != str(setup_version), (before.version, setup_version)
        what = f"canary {action} on a stale base"
        body = self.refusal(lambda: self.expect(
            w.act(action, **({"allow_non_significant": True} if action == "complete" else {})),
            409, "canary.stale_base", what=what), what=what)
        as_text = (lambda v: None if v is None else str(v))  # noqa: E731 — the API's own shape
        assert body["detail"]["setup_version"] == as_text(setup_version), body
        assert body["detail"]["current_version"] == as_text(before.version), body
        after = self.a.row()
        assert (after.spec, after.version) == (before.spec, before.version)
        assert w.row().status == "running"
        self.note(f"{action} refused: stale base {setup_version} → {before.version}")

    def step_a(self, edge: Edge) -> None:
        if edge.id in ("delete_active", "delete_failed"):
            self.cross_delete(edge)
            return
        if edge.id in ("redeploy", "redeploy_from_failed"):
            self.cross_redeploy(edge)
            return
        super().step_a(edge)

    def step_b(self, edge: Edge) -> None:
        if edge.id in CANARY_PUBLISHES and self.node_a in AGENT_JOB_NODES \
                and self.a.agent_id == self.shared_agent:
            self.publish_during_live_job(edge)
            return
        if edge.id == "create_already_running" and self.a.agent_id == self.shared_agent \
                and self.agent_status(self.shared_agent) != "active":
            # N17-obs15: the create's agent-status gate answers before its already-running
            # gate (API_CANARY_CREATE names both, not their order) — 400 `canary.agent_not_active`
            # while the agent redeploys; nothing written either way
            self.refusal(lambda: self.expect(
                self.ada.post("/api/runtime-canaries", json=self.b.create_body(self.shared_agent)),
                400, "canary.agent_not_active", what="second canary while the agent redeploys"),
                what="second canary while the agent redeploys")
            return
        super().step_b(edge)

    def publish_during_live_job(self, edge: Edge) -> None:
        """agents.md §1 / §1.2 (N17-obs13, fixed in cn/r17-c) — "A deploy job owns its agent
        until it is terminal": the canary's `complete` (promote) and `rollback` publish the
        champion's live version like a redeploy (architecture.md "Spec snapshots"), so while
        the agent's job is `queued` / `running` they answer the shared 409
        `agent.deploy_in_progress {job_id, stage, job_status}` and write nothing — the agent
        row keeps its spec / version, the canary stays `running` at its stage, no snapshot
        row appears. The canary node does not move (a refusal self-loop in the cross walk)."""
        from tests.model.test_canary_state_machine import FINAL

        w = self.b
        action = "complete" if edge.id == "complete_final" else "rollback"
        if action == "complete":
            assert w.stage == FINAL
        before = self.a.row()
        spec_before, version_before = before.spec, before.version
        what = f"canary {action} while the agent's deploy job is live"
        body = self.refusal(lambda: self.expect(
            w.act(action, **({"allow_non_significant": True} if action == "complete" else {})),
            409, "agent.deploy_in_progress", what=what), what=what)
        assert set(body["detail"]) == {"job_id", "stage", "job_status"}, body
        assert body["detail"]["job_status"] in ("queued", "running"), body
        after = self.a.row()
        assert (after.spec, after.version) == (spec_before, version_before)
        row = w.row()
        assert row.status == "running" and row.running_action is None and row.error is None
        self.note(f"{action} refused (agent {self.node_a}, job {body['detail']['job_status']})")

    def inject(self) -> bool:
        if self.node_a not in AGENT_LIVE_NODES or self.a.agent_id != self.shared_agent:
            return False
        edge = GRAPHS["agent"].edge("delete_active" if self.node_a == "active" else "delete_failed")
        self.cross_delete(edge)
        return True


# ---------------------------------------------------------------------------
# agent × chat session
# ---------------------------------------------------------------------------


class AgentChat(Cross):
    pairing = "agent_x_chat"
    graph_a = "agent"
    graph_b = "chat_session"

    def __init__(self, clients, seed, hooks, app, producer, plane) -> None:
        super().__init__(clients, seed)
        self.hooks, self.app, self.producer, self.plane = hooks, app, producer, plane

    def setup(self) -> None:
        self.a = AgentWorld(self.clients, self.hooks)
        graph = GRAPHS["agent"]
        self.a.step(graph.edge("create"))
        self.a.step(graph.edge("deploy_succeeds"))
        self.node_a = "active"
        self.shared_agent = self.a.agent_id
        tab = TestClient(self.app, client=PEER)
        tab.cookies = self.ada.cookies
        key = self.ada.post("/api/apikeys", json={"name": f"x-v1-{uuid.uuid4().hex[:6]}"})
        assert key.status_code == 201, key.text
        self.b = ChatWorld(self.clients, self.shared_agent, self.producer, self.plane, tab,
                           key.json()["key"])

    def ids(self) -> dict[str, set[str]]:
        return {"agent_ids": {self.shared_agent}, "canary_ids": set(), "run_ids": set()}

    def agent_deleted(self) -> bool:
        return self.agent_status(self.shared_agent) == "deleted"

    def filter(self, side: str, edges: list[Edge]) -> list[Edge]:
        if side == "a" and not self.injected:
            edges = [e for e in edges if e.id not in ("delete_active", "delete_failed")]
        if side == "b" and self.agent_deleted():
            edges = [e for e in edges if e.id not in CHAT_SKIP_AFTER_DELETE]
        if side == "b" and self.agent_status(self.shared_agent) != "active":
            # a gated turn cannot go live on a non-active agent; `turn_completes` needs one
            edges = [e for e in edges if e.id != "turn_completes" or self.b.gate is not None]
        return edges

    def inject_redeploy(self) -> bool:
        """Round 2: a redeploy mid-session — api.md names no chat refusal for it (N16-obs74:
        the row flips to `deploying`, chat answers 409 `agent.not_active` meanwhile); a live
        gated turn keeps running on the previous version."""
        if self.node_a != "active" or self.a.agent_id != self.shared_agent:
            return False
        edge = GRAPHS["agent"].edge("redeploy")
        self.current = ("a", edge)
        self.note(f"inject: redeploy (chat at {self.node_b}, live turn: {self.b.gate is not None})")
        self.step_a(edge)
        return True

    def inject(self) -> bool:
        if self.node_a not in AGENT_LIVE_NODES or self.a.agent_id != self.shared_agent:
            return False
        live = self.b.gate is not None
        edge = GRAPHS["agent"].edge("delete_active" if self.node_a == "active" else "delete_failed")
        # API_DELETE enumerates the refusals (403 forbidden, 409 deploy_in_progress / in_use):
        # a live chat turn is not one of them, so the delete answers 200 even mid-turn
        self.step_a(edge)
        self.note(f"inject: agent deleted (chat at {self.node_b}, live turn: {live})")
        if live:
            self.b.gate.set()
            self.b.thread.join(timeout=BUDGET_S)
            assert not self.b.thread.is_alive(), "the live turn never ended after the delete"
            self.note(f"live turn after delete → {self.b.box.get('status')} "
                      f"{self.b.box.get('text', '')[-120:]!r}")
            self.producer.wait_finished(self.shared_agent, self.b.sid)
            self.b.gate = self.b.thread = None
        return True

    def step_b(self, edge: Edge) -> None:
        status = self.agent_status(self.shared_agent)
        if status == "active":
            super().step_b(edge)
            return
        w = self.b
        if status != "deleted":
            # OBS74 ruling (agent graph `chat_while_*deploying`) / api.md `/v1` error codes: a
            # non-active agent answers the OWNER's every send 409 `agent.not_active` and writes
            # nothing — whatever the session's own state (api.md "Refusal precedence")
            if edge.id == "other_member_post":
                # N17-obs12 (PRODUCT, fixed in cn/r17-j — ownership-first everywhere): another
                # member's post into ada's session reads the session's 404 BEFORE the status
                # gate, so a non-owner never learns that the agent is deploying
                self.refusal(lambda: self.expect(
                    w.post(self.clients["bob"]), 404, "chat.session_not_found",
                    what=f"other member's post on a {status} agent"),
                    what=f"other member's post on a {status} agent")
                return
            if edge.id in CHAT_SENDS:
                self.refusal(lambda: self.expect(w.post(self.ada), 409, "agent.not_active",
                                                 what=f"chat {edge.id} on a {status} agent"),
                             what=f"chat {edge.id} on a {status} agent")
                return
            if edge.id in CHAT_V1:
                self.refusal(lambda: self.expect(
                    self.ada.post(f"/v1/agents/{w.agent_id}/invoke",
                                  json={"prompt": "while deploying", "session_id": w.sid},
                                  headers={"X-Api-Key": w.api_key}), 409, "agent.not_active",
                    what=f"/v1 {edge.id} on a {status} agent"),
                    what=f"/v1 {edge.id} on a {status} agent")
                return
            super().step_b(edge)  # reads, stops, reattaches: the session's own contract
            return
        # API_AGENT_ERRORS: every /api/chat/{id}* route and /v1 answer a deleted agent 404
        # `agent.not_found`, before any write
        base, sid = w.base, w.sid

        def call():
            if edge.id in ("first_turn", "next_turn", "console_turn_after_v1", "send_while_live",
                           "send_ended"):
                return w.post(self.ada)
            if edge.id == "other_member_post":
                return w.post(self.clients["bob"])
            if edge.id in ("stop_open", "stop_after_v1", "stop_ended"):
                return self.ada.post(f"{base}/sessions/{sid}/stop")
            if edge.id == "other_member_stop":
                return self.clients["bob"].post(f"{base}/sessions/{sid}/stop")
            if edge.id == "history_ended":
                return self.ada.get(f"{base}/history", params={"session_id": sid})
            if edge.id == "other_member_history":
                return self.clients["bob"].get(f"{base}/history", params={"session_id": sid})
            if edge.id in ("v1_turn", "v1_turn_again", "v1_send_ended"):
                return self.ada.post(f"/v1/agents/{w.agent_id}/invoke",
                                     json={"prompt": "after delete", "session_id": sid},
                                     headers={"X-Api-Key": w.api_key})
            client = {"admin_reattach": self.admin, "other_member_reattach": self.clients["bob"]
                      }.get(edge.id, self.ada)
            box: dict = {}

            def go():
                box["res"] = client.get(f"{base}/sessions/{sid}/turns/1/stream",
                                        params={"from": 0})

            t = threading.Thread(target=go, name="x-reattach", daemon=True)
            t.start()
            t.join(timeout=BUDGET_S)
            assert "res" in box, f"reattach {edge.id} on a deleted agent gave no answer in time"
            return box["res"]

        self.refusal(lambda: self.expect(call(), 404, "agent.not_found",
                                         what=f"chat {edge.id} after the agent delete"),
                     what=f"chat {edge.id} after the agent delete")


# ---------------------------------------------------------------------------
# dataset × evaluation run
# ---------------------------------------------------------------------------


class DatasetRun(Cross):
    pairing = "dataset_x_run"
    graph_a = "eval_run"
    graph_b = "eval_run"  # one graph; side b is the dataset's forced delete (the injection)

    def __init__(self, clients, seed, monkeypatch, ada_id) -> None:
        super().__init__(clients, seed)
        self.monkeypatch, self.ada_id = monkeypatch, ada_id
        self.forced = False

    def setup(self) -> None:
        queue = HeldQueue()
        self.monkeypatch.setattr(svc, "run_queue", queue)
        self.monkeypatch.setattr(eval_routers, "run_queue", queue)
        tag = uuid.uuid4().hex[:6]
        from tests.test_visibility import _active_agent

        agent_id = _active_agent(f"x-eval-{tag}", created_by=self.ada_id)
        deploying = _active_agent(f"x-eval-dep-{tag}", created_by=self.ada_id)
        db = SessionLocal()
        try:
            db.get(Agent, deploying).status = "deploying"
            db.commit()
        finally:
            db.close()
        dataset = _dataset(self.ada_id, f"x-ds-{tag}", items=list(ITEMS))
        self.a = EvalRunWorld(self.clients, self.monkeypatch, self.ada_id, agent_id, deploying,
                              dataset, queue)
        self.b = self.a
        self.node_b = self.node_a

    def ids(self) -> dict[str, set[str]]:
        runs = {i for i in self.a.ids if i not in (self.a.agent_id, self.a.dataset_id)}
        return {"agent_ids": {self.a.agent_id}, "canary_ids": set(), "run_ids": runs}

    def forced_datasets(self) -> set[str]:
        return {self.a.dataset_id} if self.forced else set()

    def enabled(self, side: str) -> list[Edge]:
        graph = GRAPHS["eval_run"]
        edges = [e for e in graph.out_edges(self.node_a) if e.id not in self.a.XFAIL]
        if self.forced:
            edges = [e for e in edges if not e.id.startswith("compare") or self.a.other]
        return edges

    def inject(self) -> bool:
        if self.node_a != "queued":
            return False
        ds = f"/api/eval/datasets/{self.a.dataset_id}?force=true"
        # §22: "A **member's** `force` answers **403 `auth.forbidden`**" — and writes nothing
        self.refusal(lambda: self.expect(self.ada.delete(ds), 403, "auth.forbidden",
                                         what="member force delete"), what="member force delete")
        body = self.expect(self.admin.delete(ds), 200,
                           what="admin force delete of a replayed dataset")
        assert body["deleted"] is True
        self.forced = True
        self.a.dataset_deleted = True
        self.note(f"inject: admin force-deleted the dataset under a {self.node_a} run")
        return True

    def step_b(self, edge: Edge) -> None:
        self.step_a(edge)

    def step_a(self, edge: Edge) -> None:
        if not self.forced:
            super().step_a(edge)
            self.node_b = self.node_a
            return
        w = self.a
        if edge.id in ("create", "rerun_completed", "rerun_stopped"):
            # EE_23 / API_NOT_FOUND: the gate of POST /api/eval/runs on a missing dataset
            if edge.id == "create":
                call = lambda: self.ada.post("/api/eval/runs", json=w.body())  # noqa: E731
            else:
                call = lambda: self.ada.post(f"/api/eval/runs/{w.run_id}/rerun")  # noqa: E731
            self.refusal(lambda: self.expect(call(), 404, "dataset.not_found",
                                             what=f"{edge.id} after the forced dataset delete"),
                         what=f"{edge.id} after the forced dataset delete")
            return  # the node does not move: nothing was queued
        if edge.id in ("dataset_delete_while_queued", "dataset_delete_after_completed"):
            self.refusal(lambda: self.expect(
                self.ada.delete(f"/api/eval/datasets/{w.dataset_id}"), 404, "dataset.not_found",
                what=f"{edge.id} after the forced dataset delete"),
                what=f"{edge.id} after the forced dataset delete")
            self.node_a = edge.dst
            self.node_b = self.node_a
            return
        super().step_a(edge)
        self.node_b = self.node_a


# ---------------------------------------------------------------------------
# agent × evaluation run (round 2, r17-e — the dangling oracle's ACTIVE-run rule)
# ---------------------------------------------------------------------------


class AgentRun(Cross):
    """The run graph on the agent the agent graph created; the agent's delete (and a redeploy)
    injected while runs are queued / finished. Cross rules the contracts name: a run on a
    non-active agent is `400 agent.not_active` (evaluation-engine.md §23 — create AND rerun
    go through the same gate); the agent's delete lists no run among its refusals (agents.md
    §1 / §1.1), so it answers 200 even with a queued run — and the oracle then says whether
    an ACTIVE run is left naming a deleted agent (no contract sentence: a gap when it fires)."""

    pairing = "agent_x_run"
    graph_a = "agent"
    graph_b = "eval_run"

    def __init__(self, clients, seed, hooks, monkeypatch, ada_id) -> None:
        super().__init__(clients, seed)
        self.hooks, self.monkeypatch, self.ada_id = hooks, monkeypatch, ada_id

    def setup(self) -> None:
        self.a = AgentWorld(self.clients, self.hooks)
        graph = GRAPHS["agent"]
        self.a.step(graph.edge("create"))
        self.a.step(graph.edge("deploy_succeeds"))
        self.node_a = "active"
        self.shared_agent = self.a.agent_id
        queue = HeldQueue()
        self.monkeypatch.setattr(svc, "run_queue", queue)
        self.monkeypatch.setattr(eval_routers, "run_queue", queue)
        tag = uuid.uuid4().hex[:6]
        from tests.test_visibility import _active_agent

        deploying = _active_agent(f"x-run-dep-{tag}", created_by=self.ada_id)
        db = SessionLocal()
        try:
            db.get(Agent, deploying).status = "deploying"
            db.commit()
        finally:
            db.close()
        dataset = _dataset(self.ada_id, f"x-run-ds-{tag}", items=list(ITEMS))
        self.b = EvalRunWorld(self.clients, self.monkeypatch, self.ada_id, self.shared_agent,
                              deploying, dataset, queue)

    def ids(self) -> dict[str, set[str]]:
        runs = {i for i in self.b.ids if i not in (self.b.agent_id, self.b.dataset_id)}
        return {"agent_ids": {self.shared_agent}, "canary_ids": set(), "run_ids": runs}

    def agent_active(self) -> bool:
        return self.agent_status(self.shared_agent) == "active" \
            and self.a.agent_id == self.shared_agent

    def filter(self, side: str, edges: list[Edge]) -> list[Edge]:
        if side == "a" and not self.injected:
            edges = [e for e in edges if e.id not in ("delete_active", "delete_failed")]
        if side == "b" and not self.agent_active() and not self.b.other:
            # these edges' `before()` stages a second completed run (compare needs two) —
            # impossible on a non-active agent (400), so they wait until `other` exists
            edges = [e for e in edges if not e.id.startswith("compare")
                     and e.id != "dataset_delete_after_completed"]
        return edges

    def active_runs(self) -> list[str]:
        """The ids of ACTIVE runs naming the shared agent (`evaluation.service.ACTIVE_STATUSES`)."""
        from app.evaluation.models import EvalRun

        db = SessionLocal()
        try:
            return [r.id for r in db.query(EvalRun)
                    .filter(EvalRun.agent_id == self.shared_agent,
                            EvalRun.status.in_(svc.ACTIVE_STATUSES))
                    .order_by(EvalRun.created_at.asc()).all()]
        finally:
            db.close()

    def cross_delete(self, edge: Edge) -> None:
        """agents.md §1.3 (N17-obs18, fixed in cn/r17-f): the agent's DELETE while an evaluation
        run on it is ACTIVE is `409 agent.in_use {canaries: [], runs: [{id, name, status}]}` for
        members and administrators alike — nothing written, the run stays as it was, so it can
        never dispatch against a torn-down Runtime; else the agent graph's own delete edge."""
        active = self.active_runs()
        if self.a.agent_id == self.shared_agent and active:
            status_before = self.agent_status(self.shared_agent)
            for client in (self.ada, self.admin):
                body = self.refusal(
                    lambda c=client: self.expect(
                        c.delete(f"/api/agents/{self.shared_agent}"), 409, "agent.in_use",
                        what="agent delete under an ACTIVE run"),
                    what="agent delete under an ACTIVE run")
                assert [r["id"] for r in body["detail"]["runs"]] == active, body
                assert body["detail"]["canaries"] == [], body
            assert self.agent_status(self.shared_agent) == status_before
            assert self.active_runs() == active
            self.note(f"delete refused (ACTIVE run(s) {active} at {self.node_b})")
            return
        self.a.step(edge)
        self.node_a = edge.dst
        self.note(f"agent deleted (run at {self.node_b})")

    def step_a(self, edge: Edge) -> None:
        if edge.id in ("delete_active", "delete_failed"):
            self.cross_delete(edge)
            return
        super().step_a(edge)

    def inject(self) -> bool:
        if self.node_a not in AGENT_LIVE_NODES or self.a.agent_id != self.shared_agent:
            return False
        edge = GRAPHS["agent"].edge("delete_active" if self.node_a == "active" else "delete_failed")
        self.current = ("a", edge)
        self.note(f"inject: agent delete (run at {self.node_b})")
        self.step_a(edge)  # §1.3: refused while a run is ACTIVE, else the agent graph's edge
        return True

    def inject_redeploy(self) -> bool:
        if self.node_a != "active" or self.a.agent_id != self.shared_agent:
            return False
        edge = GRAPHS["agent"].edge("redeploy")
        self.current = ("a", edge)
        self.note(f"inject: redeploy (run at {self.node_b})")
        self.step_a(edge)
        return True

    def step_b(self, edge: Edge) -> None:
        if self.agent_active():
            super().step_b(edge)
            return
        w = self.b
        if edge.id in ("create", "create_no_evaluators", "rerun_completed", "rerun_stopped",
                       "rerun_dataset_gone"):
            # EE_23: the same gate as POST /api/eval/runs — a non-active agent → 400; the
            # status gate answers BEFORE the body validation, so `evaluators: []` on a
            # deploying / deleted agent reads 400 `agent.not_active`, not 422
            # `run.no_evaluators`, and a rerun whose dataset is gone reads 400 before the
            # dataset's 404 (N17-obs17, DOC — the precedence class of obs12 / obs15)
            if edge.id == "create":
                call = lambda: self.ada.post("/api/eval/runs", json=w.body())  # noqa: E731
            elif edge.id == "create_no_evaluators":
                call = lambda: self.ada.post("/api/eval/runs", json=w.body(evaluators=[]))  # noqa: E731
            else:
                call = lambda: self.ada.post(f"/api/eval/runs/{w.run_id}/rerun")  # noqa: E731
            self.refusal(lambda: self.expect(call(), 400, "agent.not_active",
                                             what=f"{edge.id} on a non-active agent"),
                         what=f"{edge.id} on a non-active agent")
            return
        super().step_b(edge)


# ---------------------------------------------------------------------------
# agent × knowledge base / memory store (round 3, r17-g)
# ---------------------------------------------------------------------------


class KbMemAgentWorld(AgentWorld):
    """The agent world whose spec mounts a knowledge base and pins a managed memory store;
    `kb_ids` is mutable so a forced KB delete (which strips the mount) is mirrored before the
    next redeploy re-posts the spec."""

    def __init__(self, clients, hooks, kb_id: str, memory_id: str) -> None:
        super().__init__(clients, hooks)
        self.kb_ids: list[str] = [kb_id]
        self.memory_id = memory_id

    def spec(self, name: str | None = None) -> dict:
        base = super().spec(name)
        base["protocol"] = "http"
        base["knowledge_bases"] = [{"kb_id": k} for k in self.kb_ids]
        if self.memory_id:  # a deleted store is not managed any more: never re-posted
            base["memory"] = {"memory_id": self.memory_id}
        return base


class AgentKbMem(AgentChat):
    """The chat graph on an agent that mounts a KB and pins a memory store; besides the delete
    / redeploy / visibility injections of `AgentChat`, the KB and the memory are deleted
    mid-walk (api.md Console Knowledge Bases API: `409 kb.has_attached_agents` while agents
    mount it, `force=true` strips it from every mounted agent's spec first; Console Memory
    API: `409 memory.in_use` while a live agent's spec pins it). Oracles: the shared ledger
    scan (no agent pins a KB / memory that no longer exists), the agent's read model after a
    force (the mount is gone), the chat memory rail of the open session (200 while the agent
    is active), and every refusal writing nothing."""

    pairing = "agent_x_kbmem"

    def __init__(self, clients, seed, hooks, app, producer, plane, fakes) -> None:
        super().__init__(clients, seed, hooks, app, producer, plane)
        self.fakes = fakes
        self.kb_gone = self.memory_gone = False
        self.kbmem_steps: list[str] = []

    def setup(self) -> None:
        tag = uuid.uuid4().hex[:6]
        ws = ws_ctx(SUBSTRATE, region="cn-northwest-1", id=DEFAULT_WORKSPACE_ID)
        kb = knowledge_backend.get_backend().create_kb(ws, f"x-kb-{tag}", "", {"mode": "upload"})
        self.kb_id = kb["kb_id"]
        res = self.admin.post("/api/memory/resources", json={"name": f"x_mem_{tag}"})
        assert res.status_code == 201, res.text
        self.memory_id = res.json()["id"]
        self.a = KbMemAgentWorld(self.clients, self.hooks, self.kb_id, self.memory_id)
        graph = GRAPHS["agent"]
        self.a.step(graph.edge("create"))
        self.a.step(graph.edge("deploy_succeeds"))
        self.node_a = "active"
        self.shared_agent = self.a.agent_id
        out = self.ada.get(f"/api/agents/{self.shared_agent}").json()
        assert [k["kb_id"] for k in out["spec"]["knowledge_bases"]] == [self.kb_id], out["spec"]
        assert out["spec"]["memory"]["memory_id"] == self.memory_id
        tab = TestClient(self.app, client=PEER)
        tab.cookies = self.ada.cookies
        key = self.ada.post("/api/apikeys", json={"name": f"x-v1-{tag}"})
        assert key.status_code == 201, key.text
        self.b = ChatWorld(self.clients, self.shared_agent, self.producer, self.plane, tab,
                           key.json()["key"])
        # the KB / memory deletes land at their own middle-third steps, drawn from the plan
        rng = random.Random(f"{self.pairing}:{self.plan.seed}:kbmem")
        n = self.plan.length
        self.kb_at = rng.randint(n // 3, (2 * n) // 3)
        self.mem_at = rng.randint(n // 3, (2 * n) // 3)
        self.kb_force = rng.random() < 0.5

    def ids(self) -> dict[str, set[str]]:
        return {"agent_ids": {self.shared_agent}, "canary_ids": set(), "run_ids": set()}

    def agent_live(self) -> bool:
        """A live agent of this walk still pins the store / mounts the KB: the shared agent, or
        the agent `create_after_delete_same_name` re-created from the same spec (hence the
        world's CURRENT agent id, not the original one)."""
        return self.agent_status(self.a.agent_id) != "deleted"

    def kb_delete(self, *, force: bool) -> None:
        url = f"/api/knowledge-bases/{self.kb_id}" + ("?force=true" if force else "")
        what = f"KB delete ({'force' if force else 'plain'}) while the agent "
        what += "mounts it" if self.agent_live() else "is deleted"
        if self.agent_live() and not force:
            body = self.refusal(lambda: self.expect(self.admin.delete(url), 409,
                                                    "kb.has_attached_agents", what=what), what=what)
            assert body["detail"]["agents"], body
            assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 200
            self.note(f"kb delete refused ({self.node_a})")
            return
        if force and self.agent_live() and self.agent_status(self.a.agent_id) == "deploying":
            # agents.md §1 (N17-obs31): a mounting agent mid-deploy refuses the whole force
            body = self.refusal(lambda: self.expect(self.admin.delete(url), 409,
                                                    "agent.deploy_in_progress", what=what),
                                what=what)
            assert self.a.agent_id in {a["id"] for a in body["detail"]["agents"]}, body
            assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 200
            self.note(f"kb force delete refused: mounting agent deploying ({self.node_a})")
            return
        body = self.expect(self.admin.delete(url), 200, what=what)
        assert body["deleted"] is True
        self.kb_gone = True
        self.a.kb_ids = []
        if self.agent_live():
            # API_KB_DELETE (N17-obs31 fixed in cn/r17-h): force strips the pin from the live
            # agent — the shared one, or the same-name agent re-created from the spec — marks
            # it kb_stale and writes no snapshot
            out = self.ada.get(f"/api/agents/{self.a.agent_id}").json()
            assert [k["kb_id"] for k in out["spec"].get("knowledge_bases", [])] == [], out["spec"]
            assert out["kb_stale"] is True and self.kb_id in out["last_strip"]["kb_ids"], out
            assert self.a.agent_id in {a["id"] for a in body["stripped_agents"]}, body
        assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 404
        self.note(f"kb deleted ({'force' if force else 'plain'}, agent {self.node_a})")

    def memory_delete(self) -> None:
        url = f"/api/memory/resources/{self.memory_id}"
        if self.agent_live():
            what = "memory store delete while a live agent pins it"
            body = self.refusal(lambda: self.expect(self.admin.delete(url), 409, "memory.in_use",
                                                    what=what), what=what)
            assert {a["id"] for a in body["detail"]["agents"]} == {self.a.agent_id}, body
            self.note(f"memory delete refused ({self.node_a})")
            return
        body = self.expect(self.admin.delete(url), 200, what="memory store delete after the agent")
        self.memory_gone = True
        self.a.memory_id = None
        self.note("memory store deleted (agent gone)")

    def memory_rail(self) -> None:
        """The chat memory rail of the open session: 200 while the agent is active and pins a
        managed memory (api.md Console Chat API); once the agent is deleted every chat route is
        the agent's 404."""
        if self.b.sid not in self.b.ids:
            return  # no session row yet
        res = self.ada.get(f"{self.b.base}/memory", params={"session_id": self.b.sid})
        status = self.agent_status(self.shared_agent)
        if status == "deleted":
            assert res.status_code == 404 and res.json()["code"] == "agent.not_found", res.text
        else:
            assert res.status_code == 200, (status, res.status_code, res.text[:300])

    def check(self, what: str) -> None:
        from tests.property.ledger_invariants import scan_ledger

        own = [v for v in scan_ledger(SessionLocal) if any(i and i in v for i in self.a.ids)]
        if own and all(any(k in v for k in REGISTERED_DANGLING.values()) for v in own):
            raise RegisteredDangling((what, own))
        super().check(what)
        self.memory_rail()

    def run(self) -> None:
        # the two extra injections ride on the plan's step loop through `inject_redeploy`'s
        # sibling hook below; wire them by wrapping the base loop's per-step check
        self._kb_done = self._mem_done = False
        super().run()

    def inject_redeploy(self) -> bool:
        # piggy-back: before the redeploy injection fires, land the KB / memory deletes that
        # are due (their steps are drawn independently; the plan loop only knows one slot)
        return super().inject_redeploy()

    def step_a(self, edge: Edge) -> None:
        self._kbmem_due()
        super().step_a(edge)

    def step_b(self, edge: Edge) -> None:
        self._kbmem_due()
        super().step_b(edge)

    def _kbmem_due(self) -> None:
        step = len([t for t in self.trace if ":" in t and not t.startswith("inject")])
        if not self._kb_done and step >= self.kb_at:
            self._kb_done = True
            self.kb_delete(force=self.kb_force)
            self.check("kb_delete")
        if not self._mem_done and step >= self.mem_at:
            self._mem_done = True
            self.memory_delete()
            self.check("memory_delete")


# ---------------------------------------------------------------------------
# runtime canary × replay dataset (round 3, r17-g)
# ---------------------------------------------------------------------------


class CanaryDataset(Cross):
    """The canary graph with its replay dataset deleted mid-walk. evaluation-engine.md §22
    names the dataset's referrers (an ACTIVE run, a pipeline) — a `running` canary is not one
    (its rounds keep a `dataset_info` snapshot), so the plain delete answers 200 and every
    later `traffic {dataset_id}` is the dataset kind's 404 (api.md "the kind's existing
    not-found"), writing nothing: the canary stays `running` at its stage with no error. A
    member's `?force=true` is `403 auth.forbidden` (§22). Side b is the injection only."""

    pairing = "canary_x_dataset"
    graph_a = "runtime_canary"
    graph_b = "runtime_canary"

    def __init__(self, clients, seed, ada_id, canary_seams) -> None:
        super().__init__(clients, seed)
        self.ada_id, self.canary_seams = ada_id, canary_seams
        self.dataset_gone = False

    def setup(self) -> None:
        self.a = CanaryWorld(self.clients, self.ada_id, self.canary_seams,
                             _dataset(None, name=f"x-canary-ds-{uuid.uuid4().hex[:6]}"))
        self.b = self.a
        self.node_b = self.node_a

    def ids(self) -> dict[str, set[str]]:
        canaries = {self.a.canary_id} if self.a.canary_id else set()
        return {"agent_ids": {self.a.agent_id}, "canary_ids": canaries, "run_ids": set()}

    def enabled(self, side: str) -> list[Edge]:
        graph = GRAPHS["runtime_canary"]
        return [e for e in graph.out_edges(self.node_a) if e.id not in self.a.XFAIL]

    def inject(self) -> bool:
        ds = f"/api/eval/datasets/{self.a.dataset_id}"
        what = "member force delete of the replay dataset"
        # the dataset is an administrator's (NULL creator): users-and-ownership.md §8's
        # creator-or-admin gate answers 403 `dataset.forbidden` BEFORE §22's member-force
        # `auth.forbidden` — two 403s the contracts never order (N17-obs30, DOC)
        self.refusal(lambda: self.expect(self.ada.delete(ds + "?force=true"), 403,
                                         "dataset.forbidden", what=what), what=what)
        body = self.expect(self.admin.delete(ds), 200, what="replay dataset delete (no referrer)")
        assert body["deleted"] is True
        self.dataset_gone = True
        self.note(f"inject: replay dataset deleted (canary at {self.node_a})")
        return True

    def inject_redeploy(self) -> bool:
        return True  # no agent graph on this pairing

    def step_a(self, edge: Edge) -> None:
        if self.dataset_gone and edge.id in ("traffic", "traffic_s1", "traffic_s2"):
            w = self.a
            before = w.row()
            what = f"{edge.id} with the deleted replay dataset"
            self.refusal(lambda: self.expect(
                w.act("traffic", dataset_id=w.dataset_id), 404, "dataset.not_found", what=what),
                what=what)
            after = w.row()
            assert (after.status, after.stage, after.running_action, after.error) == (
                before.status, before.stage, None, None), (after.status, after.error)
            return  # the node does not move
        super().step_a(edge)
        self.node_b = self.node_a

    def step_b(self, edge: Edge) -> None:
        self.step_a(edge)


# ---------------------------------------------------------------------------
# agent × offboarded creator (round 5, r17-k — users × ownership, api keys × offboarding)
# ---------------------------------------------------------------------------

OFFBOARDED = (401, "auth.required")  # a non-active account's cookie is no session (auth.py)
ADA_CHAT_EDGES = {"first_turn", "next_turn", "console_turn_after_v1", "send_while_live",
                  "send_ended", "stop_open", "stop_after_v1", "stop_ended", "history_ended",
                  "reattach_open", "reattach_unknown_turn", "reattach_v1_turn",
                  "invalid_session_id"}
#: the deploy job's edges: no request of ada's — the job runs inline (workers are unfiltered) and
#: the composite reads the outcome from the ledger / as the administrator
JOB_EDGES = {"deploy_succeeds", "deploy_fails", "redeploy_succeeds", "redeploy_fails_keeps_live",
             "cold_redeploy_succeeds", "cold_redeploy_fails", "row_marked_deleted_mid_job"}
ADA_V1_EDGES = {"v1_turn", "v1_turn_again", "v1_send_ended"}
ADA_AGENT_EDGES = {"redeploy", "redeploy_from_failed", "delete_active", "delete_failed",
                   "chat_active", "chat_deleted", "redeploy_deleted", "delete_deleted",
                   "create_after_delete_same_name", "create_name_reserved",
                   "create_duplicate_while_deploying", "delete_while_deploying",
                   "redeploy_while_deploying", "chat_while_deploying",
                   "delete_while_cold_redeploying", "chat_while_cold_redeploying",
                   "delete_while_redeploying", "chat_while_redeploying"}


class AgentOffboarding(AgentChat):
    """The chat graph on ada's agent while the administrator offboards ada mid-walk: `PATCH
    /api/users/{ada} {status: disabled}` at the delete slot, `DELETE /api/users/{ada}` later
    (the flip-back slot, instead of the visibility flip back), and — when only disabled — a
    re-enable probe. Contract (users-and-ownership.md §3, §7; api.md users rows; auth.py): a
    non-active account's cookie is no session (401 `auth.required`, nothing written); her `/v1`
    key is offboarded in the same request (401 `auth.invalid_api_key`, the key flipped to
    disabled and listed `creator_offboarded: disabled` — "offboarding never deletes a key";
    `delete_user` disables the account's keys and keeps them listed); the keys are never
    re-enabled by the account's return (`POST /api/apikeys/{id}/enable` is 409
    `apikey.creator_offboarded` while offboarded, 200 once active again); her agent and
    sessions keep `created_by = ada` — under `isolated` a dangling (deleted) creator "matches
    none of the three → admin-only", so the other member reads the kind's 404; the agent's
    deploy job keeps running (workers are unfiltered)."""

    pairing = "agent_x_offboarding"

    def __init__(self, clients, seed, hooks, app, producer, plane, ada_id) -> None:
        super().__init__(clients, seed, hooks, app, producer, plane)
        self.ada_id = ada_id
        self.offboarded: str | None = None  # None · "disabled" · "deleted"
        self.reenabled = False

    def setup(self) -> None:
        super().setup()
        self.key_id = next(k["id"] for k in self.ada.get("/api/apikeys").json()["keys"]
                           if k["name"].startswith("x-v1-"))

    def ids(self) -> dict[str, set[str]]:
        return {"agent_ids": {self.shared_agent}, "canary_ids": set(), "run_ids": set()}

    # -- the offboarding injections -------------------------------------------------------------
    def key_state(self) -> dict:
        rows = self.admin.get("/api/apikeys").json()["keys"]
        return next(k for k in rows if k["id"] == self.key_id)

    def inject(self) -> bool:  # the delete slot: disable ada
        res = self.admin.patch(f"/api/users/{self.ada_id}", json={"status": "disabled"})
        assert res.status_code == 200 and res.json()["status"] == "disabled", res.text
        self.offboarded = "disabled"
        # §3: the grant / status writers call disable_offboarded_keys eagerly
        key = self.key_state()
        assert key["enabled"] is False and key["creator_offboarded"] == "disabled", key
        self.note(f"inject: ada disabled (chat at {self.node_b}, agent {self.node_a})")
        return True

    def flip(self, to: str) -> None:
        if to == "shared" and self.offboarded == "disabled" and not self.reenabled:
            # the flip-back slot: re-enable ada — her console returns, her key does NOT (§3:
            # "Keys are only ever disabled here, never re-enabled" by the account's return)
            res = self.admin.patch(f"/api/users/{self.ada_id}", json={"status": "active"})
            assert res.status_code == 200 and res.json()["status"] == "active", res.text
            self.offboarded = None
            self.reenabled = True
            key = self.key_state()
            assert key["enabled"] is False, key
            res = self.ada.post(f"/v1/agents/{self.shared_agent}/invoke",
                                json={"prompt": "after re-enable", "session_id": self.b.sid},
                                headers={"X-Api-Key": self.b.api_key})
            assert (res.status_code, res.json()["code"]) == (401, "auth.invalid_api_key"), res.text
            res = self.admin.post(f"/api/apikeys/{self.key_id}/enable")
            assert res.status_code == 200, res.text
            assert self.key_state()["enabled"] is True
            self.note("inject: ada re-enabled; key re-enabled by the administrator")
        super().flip(to)

    def inject_redeploy(self) -> bool:
        if self.offboarded:
            return True  # ada cannot redeploy while offboarded; the slot is spent
        return super().inject_redeploy()

    # -- the cross rules while offboarded ----------------------------------------------------
    def ada_refused(self, call, what: str) -> None:
        """401 `auth.required` and nothing written (auth.py: a non-active account's cookie is
        no session)."""
        self.refusal(lambda: self.expect(call(), *OFFBOARDED, what=what), what=what)

    def step_a(self, edge: Edge) -> None:
        if self.offboarded and edge.id in JOB_EDGES:
            # users-and-ownership.md §7: workers / deploy stay unfiltered — the job of a
            # disabled creator finishes exactly as before; the agent world's edge checks the
            # read model as ada (401 now), so the composite runs the job and reads the ledger
            w = self.a
            fail = edge.id in ("deploy_fails", "redeploy_fails_keeps_live", "cold_redeploy_fails")
            w.run_job(fail_at="package" if fail else None,
                      mark_deleted_at="provision" if edge.id == "row_marked_deleted_mid_job"
                      else None)
            row, job = w.row(), w.job()
            want = {"deploy_succeeds": ("active", "succeeded"),
                    "redeploy_succeeds": ("active", "succeeded"),
                    "cold_redeploy_succeeds": ("active", "succeeded"),
                    "deploy_fails": ("failed", "failed"),
                    "cold_redeploy_fails": ("failed", "failed"),
                    "redeploy_fails_keeps_live": ("active", "failed"),
                    "row_marked_deleted_mid_job": ("deleted", "cancelled")}[edge.id]
            assert (row.status, job.status) == want, (edge.id, row.status, job.status)
            if edge.id == "redeploy_fails_keeps_live":
                out = self.admin.get(f"/api/agents/{w.agent_id}").json()
                assert out["last_deploy_failed"] is True, out
            self.node_a = edge.dst
            return
        if self.offboarded and edge.id in ADA_AGENT_EDGES:
            w = self.a
            if edge.id.startswith("redeploy"):
                call = lambda: self.ada.post(f"/api/agents/{w.agent_id}/redeploy", json=w.spec())  # noqa: E731
            elif edge.id.startswith("delete"):
                call = lambda: self.ada.delete(f"/api/agents/{w.agent_id}")  # noqa: E731
            elif edge.id.startswith("chat"):
                call = lambda: self.ada.post(f"/api/chat/{w.agent_id}", json={"prompt": "hi"})  # noqa: E731
            else:
                call = lambda: self.ada.post("/api/agents", json=w.spec())  # noqa: E731
            self.ada_refused(call, f"agent {edge.id} by the offboarded creator")
            return
        super().step_a(edge)

    def step_b(self, edge: Edge) -> None:
        if not self.offboarded:
            super().step_b(edge)
            return
        w = self.b
        if edge.id in ADA_V1_EDGES:
            # §3: the key is offboarded — 401 invalid_api_key, the key disabled
            what = f"/v1 {edge.id} with the offboarded creator's key"
            self.refusal(lambda: self.expect(
                self.ada.post(f"/v1/agents/{w.agent_id}/invoke",
                              json={"prompt": "x", "session_id": w.sid},
                              headers={"X-Api-Key": w.api_key}), 401, "auth.invalid_api_key",
                what=what), what=what)
            assert self.key_state()["enabled"] is False
            return
        if edge.id == "turn_completes" and w.gate is not None:
            # api.md: the turn runs server-side to its terminal event whatever happens to the
            # connection — the live turn started before the offboarding completes; the
            # world's own history read is ada's (now 401), so the check is the stream itself
            w.gate.set()
            w.thread.join(timeout=BUDGET_S)
            assert not w.thread.is_alive(), "the live turn never ended after the offboarding"
            assert w.box["status"] == 200 and "event: done" in w.box["text"], w.box
            # the world's reattach differential (§8.3) compares against the POST's frames
            w.post_frames[w.turn] = [(k, sq, d) for k, sq, d in _frames(w.box["text"])
                                     if k != "heartbeat"]
            w.producer.wait_finished(w.agent_id, w.sid)
            w.gate = w.thread = None
            self.node_b = edge.dst
            return
        if edge.id in ADA_CHAT_EDGES:
            base, sid = w.base, w.sid
            if edge.id in ("first_turn", "next_turn", "console_turn_after_v1", "send_while_live",
                           "send_ended", "invalid_session_id"):
                call = lambda: self.ada.post(base, json={"prompt": "p", "session_id": sid})  # noqa: E731
            elif edge.id.startswith("stop"):
                call = lambda: self.ada.post(f"{base}/sessions/{sid}/stop")  # noqa: E731
            elif edge.id == "history_ended":
                call = lambda: self.ada.get(f"{base}/history", params={"session_id": sid})  # noqa: E731
            else:
                call = lambda: self.ada.get(f"{base}/sessions/{sid}/turns/1/stream")  # noqa: E731
            self.ada_refused(call, f"chat {edge.id} by the offboarded creator")
            return
        if edge.id == "turn_completes" and w.gate is not None:
            # api.md: the turn runs server-side to its terminal event whatever happens to the
            # connection — the live turn started before the offboarding completes
            super().step_b(edge)
            return
        super().step_b(edge)  # bob's / the administrator's edges: unchanged (ada stays creator)

    def filter(self, side: str, edges: list[Edge]) -> list[Edge]:
        edges = super().filter(side, edges)
        if self.offboarded and side == "b":
            # a gated turn cannot start for a 401 caller; the live one may still complete
            edges = [e for e in edges if e.id not in ("first_turn", "next_turn",
                                                       "console_turn_after_v1")
                     or self.b.gate is not None]
            edges = [e for e in edges if e.id != "turn_completes" or self.b.gate is not None]
        return edges

    def close(self) -> None:
        super().close()
        if self.offboarded == "disabled" or (self.offboarded is None and self.reenabled):
            self.admin.patch(f"/api/users/{self.ada_id}", json={"status": "active"})

    def run(self) -> None:
        super().run()
        if self.offboarded == "disabled":
            # the walk ended disabled: the user delete — §3 "delete_user disables the account's
            # keys and keeps them listed", §7 a dangling creator id reads admin-only
            res = self.admin.delete(f"/api/users/{self.ada_id}")
            assert res.status_code == 200 and res.json()["ok"] is True, res.text
            self.offboarded = "deleted"
            key = self.key_state()
            assert key["enabled"] is False and key["creator_offboarded"] == "deleted", key
            out = self.admin.get(f"/api/agents/{self.shared_agent}")
            if out.status_code == 200:
                assert out.json()["created_by"] == self.ada_id  # the dangling creator stays
            self.flip("isolated")
            res = self.clients["bob"].get(f"/api/agents/{self.shared_agent}")
            assert (res.status_code, res.json()["code"]) == (404, "agent.not_found"), res.text
            self.flip("shared")
            self.note("post-walk: ada deleted; her agent is admin-only under isolated")
            self.check("user_deleted")


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.fixture
def kbmem_fakes(monkeypatch):
    """The reference world's self-hosted KB / memory backends on the N6 fakes."""
    fakes = {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
             "embedder": FakeEmbedder()}
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    fakes["opensearch"].ensure_index(core.memory_index(""), core.memory_index_body())
    knowledge_backend.set_backend(SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0))
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    set_default_resources(SUBSTRATE)
    return fakes


def _clients(admin, ada, bob) -> tuple[dict[str, TestClient], str]:
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return {"admin": admin, "ada": ada_client, "bob": bob_client}, ada_id


def _run(cross: Cross) -> None:
    try:
        cross.run()
    except AssertionError as exc:
        label = ">".join(cross.trace)
        if cross.current is not None and isinstance(exc, RegisteredSymptom | RegisteredDangling):
            side, edge = cross.current
            for _rid, matches, reason in PINS:
                if matches(cross, side, edge):
                    pytest.xfail(f"{reason} — {cross.plan.id} at {side}:{edge.id}")
        raise AssertionError(
            f"cross-graph walk {cross.plan.id} failed\ntrace: {label}\n"
            f"repro-hash: {repro_hash(cross.pairing + ':' + label)}\n{exc}") from exc
    finally:
        cross.close()


def test_plans_are_deterministic_and_inject_in_the_middle_third():
    for pairing in PAIRINGS:
        for seed in range(20):
            p, q = plan(pairing, seed), plan(pairing, seed)
            assert p == q
            assert p.length // 3 <= p.inject_at <= (2 * p.length) // 3
            assert len(p.sides) == p.length and set(p.sides) <= {"a", "b"}


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_agent_x_canary(admin, ada, bob, agent_hooks, seams, seed):
    clients, ada_id = _clients(admin, ada, bob)
    _run(AgentCanary(clients, seed, agent_hooks, ada_id, seams))


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_agent_x_chat(app, admin, ada, bob, agent_hooks, chat_producer, seed):
    clients, _ada_id = _clients(admin, ada, bob)
    producer, plane = chat_producer
    _run(AgentChat(clients, seed, agent_hooks, app, producer, plane))


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_dataset_x_run(monkeypatch, admin, ada, bob, seed):
    clients, ada_id = _clients(admin, ada, bob)
    _run(DatasetRun(clients, seed, monkeypatch, ada_id))


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_agent_x_run(monkeypatch, admin, ada, bob, agent_hooks, seed):
    clients, ada_id = _clients(admin, ada, bob)
    _run(AgentRun(clients, seed, agent_hooks, monkeypatch, ada_id))


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_agent_x_kbmem(app, admin, ada, bob, kbmem_fakes, agent_hooks, chat_producer, seed):
    clients, _ada_id = _clients(admin, ada, bob)
    producer, plane = chat_producer
    _run(AgentKbMem(clients, seed, agent_hooks, app, producer, plane, kbmem_fakes))


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_agent_x_offboarding(app, admin, ada, bob, agent_hooks, chat_producer, seed):
    clients, ada_id = _clients(admin, ada, bob)
    producer, plane = chat_producer
    _run(AgentOffboarding(clients, seed, agent_hooks, app, producer, plane, ada_id))


@pytest.mark.parametrize("seed", cross_seeds(soak=SOAK))
def test_canary_x_dataset(admin, ada, bob, seams, seed):
    clients, ada_id = _clients(admin, ada, bob)
    _run(CanaryDataset(clients, seed, ada_id, seams))


def _live_update_job(x: AgentCanary) -> None:
    """An update deploy job queued on the shared agent's ledger row (status `deploying`) —
    the N17-obs13 window, which the N17-obs16 route guard no longer opens by itself."""
    from tests.test_canary_deploy_guard import _queue_update_job

    x.a.job_id = _queue_update_job(x.shared_agent)
    x.a.ids.add(x.a.job_id)
    x.node_a = "redeploying"
    assert x.agent_status(x.shared_agent) == "deploying"


def _published(agent_id: str, version: str) -> None:
    """What the real deploy stage leaves on the row: the new live version."""
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).version = version
        db.commit()
    finally:
        db.close()


def test_N17_obs13_canary_complete_while_the_deploy_job_is_live(admin, ada, bob,
                                                                agent_hooks, seams):
    """N17-obs13 (fixed in cn/r17-c) — minimal cross-graph sequence (shrunk from
    agent_x_canary/X0001, 27 steps):

        agent: create > deploy_succeeds · canary: seed_running > setup > traffic > verdict >
        advance > traffic_s1 > verdict_s1 > advance_s1 > traffic_s2 > verdict_s2 (the 1/99 stage)
        · agent: redeploy (202, row `deploying`, update job queued) · canary: complete_final

    Contract: agents.md §1 / §1.2 "A deploy job owns its agent until it is terminal" (409
    `agent.deploy_in_progress`, nothing touched) + architecture.md "Spec snapshots". Before
    the fix the complete was accepted (202): the agent row carried the candidate spec /
    version while `deploying` and `_finish` stamped the canary's spec into the REDEPLOY's
    snapshot. Now: 409, nothing written; once the job finishes the complete is a 202 and
    the redeploy's snapshot holds the spec the job packaged (`test_pipeline`)."""
    clients, ada_id = _clients(admin, ada, bob)
    x = AgentCanary(clients, 0, agent_hooks, ada_id, seams)
    x.setup()
    canary = GRAPHS["runtime_canary"]
    for e in ("seed_running", "setup", "traffic", "verdict", "advance", "traffic_s1",
              "verdict_s1", "advance_s1", "traffic_s2", "verdict_s2"):
        x.step_b(canary.edge(e))
    try:
        # N17-obs16 (cn/r17-d): under the running canary the redeploy itself is refused
        x.cross_redeploy(GRAPHS["agent"].edge("redeploy"))
        assert x.node_a == "active"
        # the obs13 window needs a live job: since the route refuses it, queue the update
        # job on the ledger (a job already live at setup, an out-of-band publish) — the
        # deploy-job guard below is N17-obs13's own belt
        _live_update_job(x)
        x.publish_during_live_job(canary.edge("complete_final"))
        # the job finishing hands the agent back — and publishes V+1 (the hermetic deploy
        # stage leaves the version alone; the real one bumps it): the base moved, so the
        # same complete is now 409 canary.stale_base, nothing written (N17-obs16 belt)
        x.step_a(GRAPHS["agent"].edge("redeploy_succeeds"))
        _published(x.shared_agent, "2")
        x.publish_on_stale_base(canary.edge("complete_final"))
    finally:
        x.close()


def test_N17_obs13_canary_rollback_while_the_deploy_job_is_live(admin, ada, bob,
                                                                agent_hooks, seams):
    """The rollback half of the agents.md §1.2 ruling: a roll-forward mint publishes the
    champion's live version too, so a `ready` canary's rollback is the same 409 while the
    agent's update job is live, and a 202 once the job is terminal."""
    clients, ada_id = _clients(admin, ada, bob)
    x = AgentCanary(clients, 0, agent_hooks, ada_id, seams)
    x.setup()
    canary = GRAPHS["runtime_canary"]
    for e in ("seed_running", "setup"):
        x.step_b(canary.edge(e))
    try:
        x.cross_redeploy(GRAPHS["agent"].edge("redeploy"))  # N17-obs16: 409 agent.in_use
        _live_update_job(x)
        x.publish_during_live_job(canary.edge("rollback"))
        x.step_a(GRAPHS["agent"].edge("redeploy_succeeds"))
        _published(x.shared_agent, "2")
        x.publish_on_stale_base(canary.edge("rollback"))
    finally:
        x.close()


def test_N17_obs18_agent_delete_under_a_queued_run(monkeypatch, admin, ada, bob, agent_hooks):
    """N17-obs18 (fixed in cn/r17-f) — minimal cross-graph sequence (round 2, `agent_x_run`):

        agent: create > deploy_succeeds · run: create (queued) · agent: delete_active → 409
        `agent.in_use {runs}` · run: stop_queued (202) · agent: delete_active → 200

    Before the fix the delete answered 200 and the queued run kept naming the deleted agent
    (the dangling oracle fired); now the run owns the agent until it is terminal
    (agents.md §1.3) and the oracle stays clean on every step."""
    clients, ada_id = _clients(admin, ada, bob)
    x = AgentRun(clients, 0, agent_hooks, monkeypatch, ada_id)
    try:
        x.setup()
        x.step_b(GRAPHS["eval_run"].edge("create"))
        x.current = ("a", GRAPHS["agent"].edge("delete_active"))
        x.cross_delete(GRAPHS["agent"].edge("delete_active"))
        assert x.node_a == "active"
        x.check("delete refused")
        x.step_b(GRAPHS["eval_run"].edge("stop_queued"))
        assert x.active_runs() == []
        x.cross_delete(GRAPHS["agent"].edge("delete_active"))
        assert x.node_a == "deleted"
        x.check("delete after stop")
    finally:
        x.close()


def test_N17_obs31_kb_force_delete_strips_the_mount(app, admin, ada, bob, kbmem_fakes,
                                                   agent_hooks, chat_producer):
    """N17-obs31 (= N16-obs34, fixed in cn/r17-h) — minimal cross-graph sequence (round 3,
    `agent_x_kbmem`):

        agent: create (spec mounts the KB, pins the memory) > deploy_succeeds ·
        admin: DELETE /api/knowledge-bases/{kb}?force=true → 200

    api.md Console Knowledge Bases API: force "strips it from every mounted agent's spec
    first … each stripped agent is marked `kb_stale`". Before the fix the self-hosted
    `delete_kb(force=True)` deleted the row and left the mount (the ledger scan fired); now the
    strip lives in the shared router layer, the mount is gone, the agent reads `kb_stale`, and
    the scan after the step is clean."""
    clients, _ada_id = _clients(admin, ada, bob)
    producer, plane = chat_producer
    x = AgentKbMem(clients, 0, agent_hooks, app, producer, plane, kbmem_fakes)
    try:
        x.setup()
        x.kb_delete(force=True)
        x.check("kb force delete")
    finally:
        x.close()
