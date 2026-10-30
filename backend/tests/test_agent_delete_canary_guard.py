"""N16-obs111 (round 16, r16-o reference graph; fix cn/r16-q): `DELETE /api/agents/{id}`
answered 200 while a Runtime canary on the agent was `running`, leaving the canary routing
to a `deleted` Agent that no later action but `cleanup` could settle.

RULING: an agent named as champion or challenger by a canary whose status is not terminal
is refused with 409 ``agent.in_use {canaries: [{id, name, status, stage}]}`` — members and
administrators alike, no force — before any AWS call; `completed` / `rolled_back` /
`cleaned` canaries never block; once the canary is settled the delete goes through. The
architect conversation purge keeps its own path (canaries cleaned first, then the agent —
`tests/test_assistant_purge_dependencies.py::
test_clear_stops_the_canary_waits_for_its_endpoints_then_deletes_the_agent`).
Contract: `docs/cn/interfaces/agents.md` §1.1, `users-and-ownership.md` §8.
"""

from __future__ import annotations

import inspect

import pytest

import app.routers.agents as agents_router
from app.assistant import purge
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent
from app.optimization.models import CANARY_BLOCKING_STATUSES, RuntimeCanary
from tests.conftest import settle_deploy_jobs

SPEC = {"name": "canary-guard-agent", "method": "zip_runtime", "system_prompt": "hi"}
TERMINAL = ("completed", "rolled_back", "cleaned")


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)


@pytest.fixture
def active_agent(client) -> str:
    """A deployed agent whose job is settled (so only the canary guard can refuse)."""
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    settle_deploy_jobs(agent_id, status="succeeded")
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.status = "active"
        agent.resource_id = "rt-guard"
        agent.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/rt-guard"
        db.commit()
    finally:
        db.close()
    return agent_id


def _canary(agent_id: str, *, status: str = "running", side: str = "champion",
            other: str = "other-agent") -> str:
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            name=f"CANARY-{status}-{side}", workspace_id=DEFAULT_WORKSPACE_ID,
            champion_agent_id=agent_id if side == "champion" else other,
            champion_agent_name="agent",
            challenger_agent_id=agent_id if side == "challenger" else other,
            challenger_agent_name="agent",
            status=status, stage="traffic",
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _status(model, row_id: str) -> str:
    db = SessionLocal()
    try:
        return db.get(model, row_id).status
    finally:
        db.close()


@pytest.fixture
def teardown_stubs(monkeypatch):
    """The Runtime delete and the residue sweep are recorded, never run (C8)."""
    calls: list[str] = []
    monkeypatch.setattr(agents_router.teardown, "cleanup_residue", lambda *a, **k: [])
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources",
                        lambda a, w: calls.append(a.id))
    return calls


@pytest.mark.parametrize("side", ["champion", "challenger"])
def test_delete_is_409_agent_in_use_while_a_canary_runs_and_touches_nothing(
    client, monkeypatch, active_agent, side,
):
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached with a running canary"))
    canary_id = _canary(active_agent, side=side)

    res = client.delete(f"/api/agents/{active_agent}")

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "agent.in_use"
    assert body["detail"] == {"canaries": [
        {"id": canary_id, "name": f"CANARY-running-{side}", "status": "running",
         "stage": "traffic"}], "runs": []}  # `runs` since N17-obs18 (additive)
    # untouched: the row still reads active, the canary still runs, the name is still taken
    assert client.get(f"/api/agents/{active_agent}").json()["status"] == "active"
    assert _status(RuntimeCanary, canary_id) == "running"
    assert client.post("/api/agents", json=SPEC).json()["code"] == "agent.name_exists"


def test_every_running_canary_is_listed_oldest_first(client, monkeypatch, active_agent):
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached with running canaries"))
    first = _canary(active_agent, side="champion")
    _canary(active_agent, status="cleaned")  # terminal: not listed
    second = _canary(active_agent, side="challenger")

    body = client.delete(f"/api/agents/{active_agent}").json()

    assert body["code"] == "agent.in_use"
    assert [c["id"] for c in body["detail"]["canaries"]] == [first, second]


@pytest.mark.parametrize("status", TERMINAL)
def test_a_settled_canary_never_blocks_the_delete(client, active_agent, teardown_stubs, status):
    _canary(active_agent, status=status)
    res = client.delete(f"/api/agents/{active_agent}")
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True
    assert teardown_stubs == [active_agent]
    assert _status(Agent, active_agent) == "deleted"


def test_the_delete_goes_through_once_the_canary_is_cleaned_up(
    client, active_agent, teardown_stubs,
):
    """The operator's path: 409 while running → `cleanup` (here: the status the action
    leaves) → 200, and the canary row stays as history."""
    canary_id = _canary(active_agent)
    assert client.delete(f"/api/agents/{active_agent}").status_code == 409
    db = SessionLocal()
    try:
        db.get(RuntimeCanary, canary_id).status = "cleaned"
        db.commit()
    finally:
        db.close()

    res = client.delete(f"/api/agents/{active_agent}")

    assert res.status_code == 200, res.text
    assert _status(Agent, active_agent) == "deleted"
    assert _status(RuntimeCanary, canary_id) == "cleaned"
    assert client.delete(f"/api/agents/{active_agent}").status_code == 404


def test_a_canary_naming_the_agent_blocks_whatever_its_workspace_column_says(
    client, monkeypatch, active_agent,
):
    """Matched on the agent id alone (ids are global): a legacy row with a NULL
    `workspace_id` still routes to the agent and still blocks."""
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached with a running canary"))
    db = SessionLocal()
    try:
        db.add(RuntimeCanary(
            name="legacy", workspace_id=None, status="running", stage="traffic",
            champion_agent_id=active_agent, champion_agent_name="agent",
            challenger_agent_id=active_agent, challenger_agent_name="agent",
        ))
        db.commit()
    finally:
        db.close()
    assert client.delete(f"/api/agents/{active_agent}").json()["code"] == "agent.in_use"


def test_the_guard_runs_after_the_deploy_job_guard_and_before_any_aws_call(
    client, monkeypatch,
):
    """A fresh agent is still `deploying` (job queued): the deploy guard answers first —
    the canary guard never masks it, and neither reaches the teardown."""
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached"))
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    _canary(agent_id)
    body = client.delete(f"/api/agents/{agent_id}").json()
    assert body["code"] == "agent.deploy_in_progress"


def test_blocking_statuses_are_exactly_the_non_terminal_ones():
    """The model's status enum is `running | completed | rolled_back | cleaned`; only the
    first still routes traffic or holds versions on the agents."""
    assert CANARY_BLOCKING_STATUSES == frozenset({"running"})
    assert not CANARY_BLOCKING_STATUSES & set(TERMINAL)


def test_the_guard_raises_directly_and_the_purge_does_not_go_through_it():
    """The architect purge removes a conversation's agents AFTER cleaning their canaries
    (`purge._STEPS`: canaries before agents) and marks the row through
    `mark_agent_deleted`, not `teardown_agent_row` — so the 409 cannot block a clear whose
    canary step already ran. Pinned structurally; the behaviour is
    `test_assistant_purge_dependencies::test_clear_stops_the_canary_waits_for_its_endpoints_then_deletes_the_agent`."""
    steps = [name for name, _fn in purge._STEPS]
    assert steps.index("canaries") < steps.index("agents")
    src = inspect.getsource(purge._step_agents)
    assert "teardown_agent_row" not in src and "delete_agent_row" not in src
    assert "mark_agent_deleted" in src
    db = SessionLocal()
    try:
        agent = Agent(name="direct", method="zip_runtime", status="active",
                      spec={"name": "direct", "method": "zip_runtime"})
        db.add(agent)
        db.flush()
        db.add(RuntimeCanary(
            name="c", status="running", stage="setup", workspace_id=agent.workspace_id,
            champion_agent_id=agent.id, champion_agent_name="direct",
            challenger_agent_id=agent.id, challenger_agent_name="direct",
        ))
        db.commit()
        with pytest.raises(AppError) as exc:
            agents_router.refuse_delete_with_running_canary(db, agent)
        assert exc.value.code == "agent.in_use" and exc.value.status_code == 409
    finally:
        db.close()
