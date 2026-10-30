"""N17-obs31 = N16-obs34 (round 16 r16-d, rediscovered by the round-3 cross walk; fix cn/r17-h):
`DELETE /api/knowledge-bases/{kb}?force=true` deleted the KB row but never stripped `kb_id`
from the mounting agents' specs — the promise api.md and the router's own comment made. The
strip now lives in the shared layer (`knowledge_backend.strip_kb_from_agents`, called by the
router before any backend): every mounting agent, hidden ones included, loses the pin in one
transaction and is marked `last_strip` (`kb_stale` on its projection) until its next publish;
a mounting agent mid-deploy refuses the force with `409 agent.deploy_in_progress` and nothing
is written. Contract: api.md Console Knowledge Bases API, knowledge-backend.md, agents.md §1."""
from __future__ import annotations

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.pipeline import (
    STAGE_ORDER,
    StageResult,
    create_deployment,
    execute_deploy_job,
    register_method,
)
from app.models.ledger import Agent, Job, SpecSnapshot
from app.services import knowledge_backend
from tests.conftest import ws_ctx
from tests.graph.executor import ledger_digest
from tests.property.ledger_invariants import scan_ledger
from tests.test_visibility import ada, admin, app  # noqa: F401 — fixtures by name


class _Backend:
    """The storage half only: the router's strip must not depend on the backend."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, bool]] = []

    def delete_kb(self, ws, kb_id, force=False):
        self.deleted.append((kb_id, force))
        return {"kb_id": kb_id, "deleted": True, "cleanup": {}, "cleanup_warnings": [],
                "cleanup_job_id": None}


@pytest.fixture
def backend():
    impl = _Backend()
    knowledge_backend.set_backend(impl)
    return impl


def _agent(name: str, kb_ids: list[str], *, status: str = "active",
           created_by: str | None = None, method: str = "zip_runtime") -> str:
    db = SessionLocal()
    try:
        row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method, status=status,
                    created_by=created_by, created_by_name="someone" if created_by else None,
                    spec={"name": name, "method": method,
                          "knowledge_bases": [{"kb_id": k, "description": f"d-{k}"}
                                              for k in kb_ids]})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _row(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _pins(agent_id: str) -> list[str]:
    return [k["kb_id"] for k in _row(agent_id).spec.get("knowledge_bases", [])]


def test_force_with_no_mounting_agent_is_a_plain_delete(client, backend):
    res = client.delete("/api/knowledge-bases/KBNONE?force=true")
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True and res.json()["stripped_agents"] == []
    assert backend.deleted == [("KBNONE", True)]


def test_force_strips_the_pin_from_every_mounting_agent_and_marks_them(client, backend):
    one = _agent("reader-1", ["KB1", "KB2"])
    two = _agent("reader-2", ["KB1"])
    other = _agent("reader-3", ["KB2"])  # mounts another KB only: untouched
    assert client.get(f"/api/agents/{one}").json()["kb_stale"] is False

    res = client.delete("/api/knowledge-bases/KB1?force=true")

    assert res.status_code == 200, res.text
    assert [a["name"] for a in res.json()["stripped_agents"]] == ["reader-1", "reader-2"]
    assert _pins(one) == ["KB2"] and _pins(two) == [] and _pins(other) == ["KB2"]
    assert _row(one).spec["knowledge_bases"][0]["description"] == "d-KB2"  # the rest is kept
    assert backend.deleted == [("KB1", True)]
    # the "pins knowledge base 'KB1' that no longer exists" line is gone (KB2 has no ledger
    # row in this fake-backend world, so only KB1's lines are the oracle here)
    assert [v for v in scan_ledger(SessionLocal) if "'KB1'" in v] == []
    for aid in (one, two):
        out = client.get(f"/api/agents/{aid}").json()
        assert out["kb_stale"] is True
        assert out["last_strip"]["kb_ids"] == ["KB1"] and out["last_strip"]["at"]
        assert out["last_strip"]["by"]  # the caller
    assert client.get(f"/api/agents/{other}").json()["kb_stale"] is False
    # a strip is not a publish: no snapshot row appeared
    db = SessionLocal()
    try:
        assert db.query(SpecSnapshot).filter(SpecSnapshot.agent_id.in_([one, two])).count() == 0
    finally:
        db.close()


def test_a_second_force_accumulates_the_stripped_ids(client, backend):
    aid = _agent("reader", ["KBA", "KBB"])
    assert client.delete("/api/knowledge-bases/KBA?force=true").status_code == 200
    assert client.delete("/api/knowledge-bases/KBB?force=true").status_code == 200
    assert _pins(aid) == [] and _row(aid).last_strip["kb_ids"] == ["KBA", "KBB"]


def test_force_strips_an_agent_hidden_from_the_caller_too(client, backend, monkeypatch):
    """The ledger must keep no dangling pin whoever looks: an agent another member created
    (hidden under isolation) is stripped as well — the strip is on the ledger, not a view."""
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        ws.settings = {**(ws.settings or {}), "resource_visibility": "isolated"}
        db.commit()
    finally:
        db.close()
    hidden = _agent("bobs-reader", ["KBH"], created_by="u-bob")
    res = client.delete("/api/knowledge-bases/KBH?force=true")
    assert res.status_code == 200, res.text
    assert _pins(hidden) == []
    assert _row(hidden).last_strip["kb_ids"] == ["KBH"]


def test_force_is_409_while_a_mounting_agent_deploys_and_writes_nothing(client, backend):
    """agents.md §1: the job owns the agent — the force is refused as a whole, nothing stripped,
    the KB row untouched (the backend is never called), every agent listed."""
    idle = _agent("idle-reader", ["KBD"])
    busy = _agent("busy-reader", ["KBD"])
    db = SessionLocal()
    try:
        agent = db.get(Agent, busy)
        agent.status = "deploying"
        _dep, job = create_deployment(db, agent, mode="update")
        job_id = job.id
    finally:
        db.close()
    before = ledger_digest()

    res = client.delete("/api/knowledge-bases/KBD?force=true")

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "agent.deploy_in_progress"
    assert body["detail"]["kb_id"] == "KBD"
    assert body["detail"]["agents"] == [{"id": busy, "name": "busy-reader", "job_id": job_id,
                                        "stage": "generate", "job_status": "queued"}]
    assert ledger_digest() == before
    assert _pins(idle) == ["KBD"] and _pins(busy) == ["KBD"]
    assert backend.deleted == []


def test_force_logs_the_strip_on_the_agents_latest_job(client, backend):
    aid = _agent("logged-reader", ["KBL"])
    db = SessionLocal()
    try:
        _dep, job = create_deployment(db, db.get(Agent, aid))
        job.status = "succeeded"
        db.commit()
        job_id = job.id
    finally:
        db.close()
    assert client.delete("/api/knowledge-bases/KBL?force=true").status_code == 200
    db = SessionLocal()
    try:
        events = [json.loads(line) for line in db.get(Job, job_id).log.splitlines() if line]
    finally:
        db.close()
    lines = [json.dumps(e) for e in events if e["stage"] == "ledger"]
    assert any("KBL" in line and "forced delete" in line for line in lines), events


def test_the_strip_is_one_transaction_even_when_a_job_log_append_fails(client, backend,
                                                                      monkeypatch):
    """rev-r17 on f30450e6: `_append_log` commits per call, so a log line inside the per-agent
    loop could leave agent 1 stripped and agent 2 mounted. Now every agent is stripped in ONE
    commit and the informational log lines follow it best-effort: with the second agent's
    append raising, both agents are stripped, the delete still answers 200, the backend ran."""
    from app.deployer import pipeline

    one = _agent("tx-reader-1", ["KBT"])
    two = _agent("tx-reader-2", ["KBT"])
    db = SessionLocal()
    try:
        for aid in (one, two):
            _dep, job = create_deployment(db, db.get(Agent, aid))
            job.status = "succeeded"
        db.commit()
    finally:
        db.close()
    calls: list[str] = []
    real = pipeline._append_log

    def flaky(db_, job_id, stage, message, level="info"):
        calls.append(job_id)
        if len(calls) == 2:
            raise RuntimeError("log store down")
        real(db_, job_id, stage, message, level)

    monkeypatch.setattr(pipeline, "_append_log", flaky)

    res = client.delete("/api/knowledge-bases/KBT?force=true")

    assert res.status_code == 200, res.text
    assert len(calls) == 2
    assert _pins(one) == [] and _pins(two) == []  # both-or-neither: both
    assert _row(one).last_strip and _row(two).last_strip
    assert backend.deleted == [("KBT", True)]


def test_the_next_successful_publish_clears_the_mark(client, backend):
    aid = _agent("republished-reader", ["KBP"], method="fake_strip_ok")
    register_method("fake_strip_ok", {s: lambda ctx, agent: StageResult() for s in STAGE_ORDER})
    assert client.delete("/api/knowledge-bases/KBP?force=true").status_code == 200
    assert _row(aid).last_strip is not None
    db = SessionLocal()
    try:
        agent = db.get(Agent, aid)
        agent.status = "deploying"
        _dep, job = create_deployment(db, agent, mode="update")
        job_id = job.id
    finally:
        db.close()

    execute_deploy_job(job_id)

    row = _row(aid)
    assert row.status == "active" and row.last_strip is None
    assert client.get(f"/api/agents/{aid}").json()["kb_stale"] is False


def test_service_strip_is_a_no_op_without_mounts():
    db = SessionLocal()
    try:
        assert knowledge_backend.strip_kb_from_agents(db, ws_ctx(), "KBZZ") == []
    finally:
        db.close()


# ---- N17-obs4: force is an administrator's override (a member's force would strip OTHER
# members' agents' specs — a cross-member mutation); a member's plain delete keeps the route
# policy. Auth-enabled clients from tests.test_visibility.


def test_a_members_force_is_403_and_strips_nothing(app, admin, ada, backend):  # noqa: F811
    from tests.test_visibility import _active_agent

    ada_id, ada = ada  # the fixture is (user id, logged-in client)
    # ada's own KBs (N17-obs530: another creator's KB is 403 before the force check can speak)
    from app.models.ledger import KnowledgeBase
    from tests.test_visibility import _add
    for kb_id in ("KBM", "KBFREE"):
        _add(KnowledgeBase(kb_id=kb_id, name=kb_id.lower(), status="ACTIVE",
                           created_by=ada_id, created_by_name="ada"))
    other = _active_agent("bobs-reader", created_by="u-bob")
    db = SessionLocal()
    try:
        row = db.get(Agent, other)
        row.spec = {**row.spec, "knowledge_bases": [{"kb_id": "KBM"}]}
        db.commit()
    finally:
        db.close()
    before = ledger_digest()

    res = ada.delete("/api/knowledge-bases/KBM?force=true")

    assert res.status_code == 403, res.text
    assert res.json()["code"] == "auth.forbidden" and res.json()["detail"] == {"kb_id": "KBM"}
    assert ledger_digest() == before and backend.deleted == []
    assert _pins(other) == ["KBM"]
    # unmounted KB, member force: still 403 — the flag itself is the override
    assert ada.delete("/api/knowledge-bases/KBFREE?force=true").status_code == 403
    # the member's plain delete keeps the route policy: 409 while mounted, else the backend
    plain = ada.delete("/api/knowledge-bases/KBM")
    assert plain.status_code == 409 and plain.json()["code"] == "kb.has_attached_agents"
    assert ada.delete("/api/knowledge-bases/KBFREE").status_code == 200
    assert backend.deleted == [("KBFREE", False)]
    # the administrator's force strips as the contract says
    res = admin.delete("/api/knowledge-bases/KBM?force=true")
    assert res.status_code == 200, res.text
    assert _pins(other) == [] and backend.deleted[-1] == ("KBM", True)
