# ruff: noqa: F811  — pytest fixtures are imported by name from the delete-guard suite
"""N17-obs16 (round 17, rev-r17 on cn/r17-c; fix cn/r17-d): `POST /api/agents/{id}/redeploy`
was accepted while a Runtime canary on the agent was `running` — only the DELETE was
refused (agents.md §1.1). The sequence setup → redeploy (job finishes, production = a new
version) → complete then set the agent's spec / version to the canary's candidate built on
the PRE-redeploy base: production silently reverted, every call legal, invisible to the
digest oracle. RULING (a): every re-publish is refused with the delete's
`409 agent.in_use {canaries}` while a canary names the agent — members and administrators
alike, nothing written; terminal canaries never block. The policy bulk re-publish lists
such an agent as skipped instead. Contract: agents.md §1.1, api.md."""
from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services import policy_enforcement
from tests.conftest import ws_ctx
from tests.graph.executor import ledger_digest
from tests.test_agent_delete_canary_guard import (  # noqa: F401 — fixtures by name
    SPEC,
    TERMINAL,
    _canary,
    _status,
    active_agent,
    no_real_deploy,
)

EDITED = {**SPEC, "system_prompt": "hi v2"}


@pytest.mark.parametrize("side", ["champion", "challenger"])
def test_redeploy_is_409_agent_in_use_while_a_canary_runs(client, active_agent, side):
    canary_id = _canary(active_agent, side=side)
    before = ledger_digest()

    res = client.post(f"/api/agents/{active_agent}/redeploy", json=EDITED)

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "agent.in_use"
    assert [c["id"] for c in body["detail"]["canaries"]] == [canary_id]
    assert "re-publishing the agent" in body["message"]
    assert ledger_digest() == before  # no job, no snapshot, the row untouched
    assert _status(Agent, active_agent) == "active"


@pytest.mark.parametrize("status", TERMINAL)
def test_a_settled_canary_never_blocks_the_redeploy(client, active_agent, status):
    _canary(active_agent, status=status)
    res = client.post(f"/api/agents/{active_agent}/redeploy", json=EDITED)
    assert res.status_code == 202, res.text
    assert _status(Agent, active_agent) == "deploying"


def test_the_guard_answers_after_the_deploy_job_guard(client, active_agent):
    """Order as on the delete (agents.md §1.1): `agent.deploy_in_progress` first."""
    _canary(active_agent)
    db = SessionLocal()
    try:
        db.get(Agent, active_agent).status = "deploying"
        db.commit()
    finally:
        db.close()
    res = client.post(f"/api/agents/{active_agent}/redeploy", json=EDITED)
    assert res.status_code == 409 and res.json()["code"] == "agent.deploy_in_progress"


def test_the_policy_bulk_republish_skips_an_agent_under_a_running_canary(client, active_agent):
    _canary(active_agent)
    db = SessionLocal()
    try:
        agent = db.get(Agent, active_agent)
        reason = policy_enforcement._apply_skip_reason(db, agent)
        assert reason and "Runtime canary" in reason
        (entry,) = policy_enforcement.apply(db, ws_ctx(), agent_ids=[active_agent],
                                            operator="test")
    finally:
        db.close()
    assert entry["job_id"] is None and "Runtime canary" in entry["skipped"]
    assert _status(Agent, active_agent) == "active"
