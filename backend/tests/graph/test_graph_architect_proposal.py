# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_assistant (+ purge)
"""E02 — the architect proposal lifecycle graph walked through the real routes (round 16, r16-o).

Graph: ``tests/graph/graphs/architect_proposal.yaml``. Hermetic: the architect's data plane
is the scripted ``FakeArchitectRuntime`` (``tests/test_assistant.harness``), the deploy
starter is recorded (``no_real_deploy``), the CLEAR job's AWS seams are the ``cloud`` fakes of
``tests/test_assistant_purge_dependencies``. The walk runs as the administrator (the
conversation owner); ownership edges live in the r16-e model, not here.
"""

from __future__ import annotations

import copy
import os

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.assistant import AssistantConversation, AssistantMessage
from app.models.ledger import Agent, Job, KnowledgeBase
from tests.graph import neighbourhood
from tests.graph.executor import (
    GRAPHS,
    World,
    assert_tables_explicit,
    ledger_digest,
    params_for,
)
from tests.graph.walks import run_walk, walk_seeds
from tests.model.test_assistant_state_machine import (
    _canon,
    _finish_deploy,
    _rows,
    _validator_view,
)
from tests.test_assistant import (  # noqa: F401 — autouse fixtures + helpers by name
    BASE,
    VALID_PROPOSAL,
    _block,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)
from tests.test_assistant_purge_dependencies import cloud, wait_job  # noqa: F401
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
CONV_URL = f"{BASE}/conversations"


def _assert_matches_validator(revision: dict, raw, *, where: str) -> None:
    """Differential oracle (architecture.md, *Proposal submission*): the stored revision is
    exactly what the pure validator says about the same input — status, bytes, errors."""
    status, display, errors = _validator_view(raw)
    assert revision["status"] == status, (where, revision["status"], status, errors)
    assert _canon(revision["content"]) == _canon(display), where
    assert revision["validation_errors"] == errors, (where, revision["validation_errors"], errors)


def _messages(cid: str) -> int:
    db = SessionLocal()
    try:
        return db.query(AssistantMessage).filter_by(conversation_id=cid).count()
    finally:
        db.close()


def _updated_at(cid: str):
    db = SessionLocal()
    try:
        return db.get(AssistantConversation, cid).updated_at
    finally:
        db.close()


class ProposalWorld(World):
    graph = "architect_proposal"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}  # N16-obs117 fixed in cn/r17-a: `reject_approved` is covered
    FIXED: dict[str, list[str]] = {"N16-obs117": ["reject_approved"]}  # neighbourhood keeps it

    def __init__(self, clients, harness) -> None:
        super().__init__(clients)
        self.harness = harness
        # the stubbed catalog's knowledge base gets its ledger row, so an approved Agent's KB
        # pin resolves for the shared ledger scan exactly as a bootstrapped KB would (r16-e)
        db = SessionLocal()
        try:
            if db.get(KnowledgeBase, "KB123ABC") is None:
                db.add(KnowledgeBase(kb_id="KB123ABC", workspace_id=DEFAULT_WORKSPACE_ID,
                                     name="hr-policies", status="ACTIVE", data_sources=[]))
                db.commit()
        finally:
            db.close()
        res = self.admin.post(CONV_URL, json={"title": "graph"})
        assert res.status_code == 201, res.text
        self.cid = res.json()["id"]
        self.ids.add(self.cid)
        self.agent_id: str | None = None
        self.job_id: str | None = None
        self.names = 0

    # -- views -----------------------------------------------------------------------
    def detail(self) -> dict:
        res = self.admin.get(f"{CONV_URL}/{self.cid}")
        assert res.status_code == 200, res.text
        return res.json()

    def proposals(self) -> list[dict]:
        return sorted(self.detail()["proposals"], key=lambda p: p["revision"])

    def latest(self) -> dict:
        return self.proposals()[-1]

    def current_content(self) -> dict:
        for p in reversed(self.proposals()):
            if isinstance(p["content"], dict) and "_rejected" not in p["content"]:
                return copy.deepcopy(p["content"])
        return copy.deepcopy(VALID_PROPOSAL)

    def turn_with(self, content) -> list[tuple[str, dict]]:
        self.harness.reply(_block(content))
        return _turn(self.admin, self.cid, "propose")

    def put(self, content: dict):
        return self.admin.put(f"{CONV_URL}/{self.cid}/proposal", json={"content": content})

    def approve_body(self, p: dict) -> dict:
        return {"revision": p["revision"], "content_hash": p["content_hash"]}

    def approve(self, p: dict):
        return self.admin.post(f"{CONV_URL}/{self.cid}/proposal/approve", json=self.approve_body(p))

    def reject(self, revision: int):
        return self.admin.post(f"{CONV_URL}/{self.cid}/proposal/reject",
                               json={"revision": revision})

    def expect_new_revision(self, before: list[dict], status: str) -> dict:
        after = self.proposals()
        assert len(after) == len(before) + 1, [p["revision"] for p in after]
        new = after[-1]
        assert new["revision"] == (before[-1]["revision"] if before else 0) + 1
        assert new["status"] == status, (new["status"], new["validation_errors"])
        if before and before[-1]["status"] in ("draft", "invalid"):
            prev = next(p for p in after if p["revision"] == before[-1]["revision"])
            assert prev["status"] == "superseded", prev["status"]
        return new

    def before(self, edge) -> None:
        if edge.id not in ("reject_stale", "approve_superseded"):
            return
        rows = self.proposals()
        needs_older = edge.id == "reject_stale" and len(rows) < 2
        needs_superseded = edge.id == "approve_superseded" and not any(
            p["status"] == "superseded" for p in rows)
        if needs_older or needs_superseded:
            # the refusal needs an OLDER / a SUPERSEDED revision: stage one valid edit first
            content = self.current_content()
            content["summary"] = f"{content['summary']} (staged edit)"
            assert self.put(content).status_code == 200

    # -- edges: turns ----------------------------------------------------------------
    def edge_propose(self, edge) -> None:
        before = self.proposals()
        events = self.turn_with(VALID_PROPOSAL)
        proposal = next(d for k, d in events if k == "proposal")
        new = self.expect_new_revision(before, "draft")
        assert proposal["revision"] == new["revision"]
        _assert_matches_validator(new, VALID_PROPOSAL, where="propose")

    edge_repropose_after_reject = edge_propose

    def edge_discuss(self, edge) -> None:
        before, messages = self.proposals(), _messages(self.cid)
        self.harness.reply("Tell me more about the HR policies first.")
        events = _turn(self.admin, self.cid, "discuss")
        assert not any(k == "proposal" for k, _d in events)
        assert self.proposals() == before
        assert _messages(self.cid) == messages + 2

    def edge_propose_invalid(self, edge) -> None:
        before = self.proposals()
        content = copy.deepcopy(VALID_PROPOSAL)
        content["tools"] = ["gateway:does-not-exist"]
        self.turn_with(content)
        new = self.expect_new_revision(before, "invalid")
        assert new["validation_errors"]
        _assert_matches_validator(new, content, where="propose_invalid")

    # -- edges: member edits -----------------------------------------------------------
    def edge_edit_valid(self, edge) -> None:
        before = self.proposals()
        content = self.current_content()
        content["summary"] = f"{content['summary']} v{len(before) + 1}"
        self.expect(self.put(content), edge, status=200)
        new = self.expect_new_revision(before, "draft")
        assert new["source"] == "member"
        _assert_matches_validator(new, content, where="edit_valid")

    def edge_edit_invalid(self, edge) -> None:
        before = self.proposals()
        content = self.current_content()
        content["tools"] = "not-a-list"
        self.expect(self.put(content), edge, status=200)
        new = self.expect_new_revision(before, "invalid")
        _assert_matches_validator(new, content, where="edit_invalid")

    def edge_repair_invalid(self, edge) -> None:
        before = self.proposals()
        content = copy.deepcopy(VALID_PROPOSAL)
        content["summary"] = "repaired"
        self.expect(self.put(content), edge, status=200)
        self.expect_new_revision(before, "draft")

    def edge_edit_resources_after_approval(self, edge) -> None:
        content = self.current_content()
        content["tools"] = ["mcp:deepwiki"]  # a resource change: the approved set was two tools
        self.expect(self.put(content), edge)

    # -- edges: approve / reject --------------------------------------------------------
    def edge_approve_invalid(self, edge) -> None:
        latest = self.latest()
        assert latest["status"] == "invalid"
        self.expect(self.approve(latest), edge)

    def edge_approve_rejected(self, edge) -> None:
        latest = self.latest()
        assert latest["status"] == "rejected"
        self.expect(self.approve(latest), edge)

    def edge_approve_wrong_hash(self, edge) -> None:
        latest = self.latest()
        res = self.admin.post(f"{CONV_URL}/{self.cid}/proposal/approve",
                              json={"revision": latest["revision"], "content_hash": "0" * 64})
        self.expect(res, edge)
        assert self.latest()["status"] == "draft"

    def edge_approve_superseded(self, edge) -> None:
        older = [p for p in self.proposals() if p["status"] == "superseded"][-1]
        self.expect(self.approve(older), edge)

    def edge_approve(self, edge) -> None:
        latest = self.latest()
        agents = self.count_agents()
        body = self.expect(self.approve(latest), edge)
        assert body["started"] is True and body["job_id"] and body["agent"]["id"]
        self.agent_id, self.job_id = body["agent"]["id"], body["job_id"]
        self.ids |= {self.agent_id, self.job_id}
        assert self.count_agents() == agents + 1
        assert self.latest()["status"] == "approved"

    def edge_approve_again(self, edge) -> None:
        latest = next(p for p in self.proposals() if p["status"] == "approved")
        agents = self.count_agents()
        body = self.expect(self.approve(latest), edge, status=200)
        assert body["started"] is False and body["job_id"] == self.job_id
        assert body["agent"]["id"] == self.agent_id and self.count_agents() == agents

    def edge_new_draft_after_approval(self, edge) -> None:
        before = self.proposals()
        self.names += 1
        content = copy.deepcopy(VALID_PROPOSAL)
        content["name"] = f"hr-helpdesk-{self.names}"
        events = self.turn_with(content)
        assert any(k == "proposal" for k, _d in events)
        new = self.proposals()[-1]
        assert len(self.proposals()) == len(before) + 1 and new["status"] == "draft"

    def edge_approve_newer_draft(self, edge) -> None:
        latest = self.latest()
        assert latest["status"] == "draft"
        agents = self.count_agents()
        body = self.expect(self.approve(latest), edge)
        assert body["detail"]["agent_id"] == self.agent_id
        assert self.latest()["status"] == "draft" and self.count_agents() == agents

    def edge_reject_approved_stale(self, edge) -> None:
        rows = self.proposals()
        approved = next(p for p in rows if p["status"] == "approved")
        assert approved is not rows[-1]
        self.expect(self.reject(approved["revision"]), edge)



    def edge_reject_draft(self, edge) -> None:
        latest = self.latest()
        body = self.expect(self.reject(latest["revision"]), edge, status=200)
        assert body["proposal"]["status"] == "rejected"

    edge_reject_invalid = edge_reject_draft

    def edge_reject_stale(self, edge) -> None:
        older = self.proposals()[-2]
        self.expect(self.reject(older["revision"]), edge)

    def edge_reject_approved(self, edge) -> None:
        """N16-obs117 (fixed in cn/r17-a): the refused reject of the executed revision names
        the approval and writes NOTHING — the conversation's `updated_at` (the history
        order) and every table's digest are what they were before the call."""
        rows = self.proposals()
        approved = next(p for p in rows if p["status"] == "approved")
        assert approved is rows[-1], "the graph reaches this edge only while approved is newest"
        stamp, digest = _updated_at(self.cid), ledger_digest()
        body = self.expect(self.reject(approved["revision"]), edge)
        assert body["detail"]["revision"] == approved["revision"] and body["detail"]["approval"]
        assert _updated_at(self.cid) == stamp, (stamp, _updated_at(self.cid))
        assert ledger_digest() == digest
        assert self.latest()["status"] == "approved"

    # -- edges: the Agent ----------------------------------------------------------------
    def count_agents(self) -> int:
        db = SessionLocal()
        try:
            return db.query(Agent).filter(Agent.system_key.is_(None)).count()
        finally:
            db.close()

    def edge_deploy_completes(self, edge) -> None:
        self._deploy_completes(edge)

    def _deploy_completes(self, edge) -> None:
        _finish_deploy(self.agent_id, self.job_id)
        db = SessionLocal()
        try:
            assert db.get(Agent, self.agent_id).status == "active"
            assert db.get(Job, self.job_id).status == "succeeded"
        finally:
            db.close()

    # -- edges: CLEAR ---------------------------------------------------------------------
    def edge_clear_while_deploying(self, edge) -> None:
        body = self.expect(self.admin.delete(f"{CONV_URL}/{self.cid}"), edge)
        assert [b["kind"] for b in body["detail"]["blockers"]] == ["job"]
        assert _rows(self.cid)["conversation"] == 1

    edge_clear_while_deploying_newer = edge_clear_while_deploying

    def edge_deploy_completes_newer(self, edge) -> None:
        self._deploy_completes(edge)

    def _clear_inline(self, edge) -> None:
        body = self.expect(self.admin.delete(f"{CONV_URL}/{self.cid}"), edge)
        assert body["deleted"] is True
        self.assert_purged()

    edge_clear_transcript_only = _clear_inline
    edge_clear_draft_only = _clear_inline

    def edge_clear_deployed(self, edge) -> None:
        body = self.expect(self.admin.delete(f"{CONV_URL}/{self.cid}"), edge)
        job = wait_job(body["job_id"])
        assert job.status == "succeeded", (
            job.status, job.error, (job.payload or {}).get("blocker"))
        self.assert_purged()
        db = SessionLocal()
        try:
            assert db.get(Agent, self.agent_id).status == "deleted"
        finally:
            db.close()

    def assert_purged(self) -> None:
        counts = _rows(self.cid)
        assert all(v == 0 for v in counts.values()), counts

    def edge_clear_again(self, edge) -> None:
        self.expect(self.admin.delete(f"{CONV_URL}/{self.cid}"), edge)

    def edge_detail_after_clear(self, edge) -> None:
        self.expect(self.admin.get(f"{CONV_URL}/{self.cid}"), edge)


@pytest.fixture
def make_world(admin, ada, bob, cloud, ready, harness):
    """A fresh world (a fresh conversation) per call — the walk shrinker replays on fresh
    worlds."""
    _ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return lambda: ProposalWorld(
        {"admin": admin, "ada": ada_client, "bob": bob_client}, harness)


@pytest.fixture
def world(make_world) -> ProposalWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(ProposalWorld)


@pytest.mark.parametrize("path", params_for(ProposalWorld))
def test_architect_proposal_path(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(ProposalWorld, include_covered_pairs=True))
def test_soak_architect_proposal_every_edge_pair(world, path):
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_architect_proposal_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, ProposalWorld, GRAPHS["architect_proposal"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(ProposalWorld, GRAPHS))
def test_architect_proposal_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(ProposalWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_architect_proposal_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    world.run(path)
