"""N17-obs12 (reclassified PRODUCT low by the ownership-first ruling, fixed in cn/r17-j):
`POST /api/chat/{id}` answered the agent-status `409 agent.not_active` BEFORE the personal
session's `404 chat.session_not_found`, so another member learned that a deploying agent exists
and is deploying. Now the session's ownership 404 answers first (api.md "Refusal precedence");
the owner's send on a deploying agent is still 409 agent.not_active."""
# ruff: noqa: F811 — fixtures by name from tests.test_visibility
from __future__ import annotations

from app.core.db import SessionLocal
from app.models.ledger import Agent, ChatSession
from tests.graph.executor import ledger_digest
from tests.test_visibility import PEER, _active_agent, ada, admin, app, bob  # noqa: F401


def _seed_session(agent_id: str, created_by: str) -> str:
    sid = "s" * 40
    db = SessionLocal()
    try:
        db.add(ChatSession(workspace_id="default", agent_id=agent_id, session_id=sid,
                           actor_id="ada", turns=1, created_by=created_by, created_by_name="ada"))
        db.commit()
    finally:
        db.close()
    return sid


def _set_status(agent_id: str, status: str) -> None:
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = status
        db.commit()
    finally:
        db.close()


def test_other_members_post_on_a_deploying_agents_session_is_the_sessions_404(app, admin, ada,
                                                                            bob):
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    agent_id = _active_agent("deploying-chat", created_by=ada_id)
    sid = _seed_session(agent_id, ada_id)
    _set_status(agent_id, "deploying")
    before = ledger_digest()

    res = bob_client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": sid})

    assert res.status_code == 404, res.text  # not 409: a non-owner learns no state
    assert res.json()["code"] == "chat.session_not_found"
    assert ledger_digest() == before
    # the owner (and the administrator) reach the status gate as before
    for client in (ada_client, admin):
        res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": sid})
        assert res.status_code == 409 and res.json()["code"] == "agent.not_active", res.text
    assert ledger_digest() == before


def test_a_new_session_id_on_a_deploying_agent_is_still_the_status_409(app, admin, ada):
    ada_id, ada_client = ada
    agent_id = _active_agent("deploying-chat-new", created_by=ada_id)
    _set_status(agent_id, "deploying")
    res = ada_client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": "n" * 40})
    assert res.status_code == 409 and res.json()["code"] == "agent.not_active", res.text
    res = ada_client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert res.status_code == 409 and res.json()["code"] == "agent.not_active", res.text
