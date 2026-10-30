"""CN extra (Family B step 1, user decision 2026-10-16): the Chat session list is a
member's OWN sessions — the ones their account opened (`chat_sessions.created_by` =
their `users.id`), plus, for rows older than the column, the ones whose first actor is
their username — while an administrator sees every session of the agent. The history
of a session a member could not list reads as missing (404 `chat.session_not_found`,
never 403). The open console (gate off) is the operator = an administrator.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.services import users as users_service

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
AGENT_ID = "ag-chat-vis"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _member(app, username: str) -> tuple[str, TestClient]:
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    assert client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@acme-corp.com", "password": password},
    ).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    assert client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 200
    return user_id, client


def _seed(sessions: list[tuple[str, str, str | None, str | None]]) -> None:
    """`(session_id, actor_id, created_by, created_by_name)` rows, each with one message
    (the list shows only sessions with a replayable transcript)."""
    db = SessionLocal()
    try:
        db.add(Agent(id=AGENT_ID, workspace_id=DEFAULT_WORKSPACE_ID, name="vis-agent",
                     method="zip_runtime", status="active", spec={"system_prompt": "x"}))
        for sid, actor, created_by, created_by_name in sessions:
            db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=AGENT_ID,
                               session_id=sid, actor_id=actor, turns=1,
                               created_by=created_by, created_by_name=created_by_name))
            db.add(ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=AGENT_ID,
                               session_id=sid, role="user", text=f"hello from {sid}"))
        db.commit()
    finally:
        db.close()


def _listed(client: TestClient) -> set[str]:
    res = client.get(f"/api/chat/{AGENT_ID}/sessions")
    assert res.status_code == 200, res.text
    return {s["session_id"] for s in res.json()["sessions"]}


def _history(client: TestClient, session_id: str) -> int:
    res = client.get(f"/api/chat/{AGENT_ID}/history", params={"session_id": session_id})
    return res.status_code


def test_members_see_their_own_sessions_and_admins_every_session(app):
    with TestClient(app, client=PEER) as admin:
        assert admin.post("/api/auth/login", json=ADMIN).status_code == 200
        ada_id, ada = _member(app, "ada")
        bob_id, bob = _member(app, "bob")
        _seed([
            ("s-ada", "ada", ada_id, "ada"),
            ("s-bob", "bob", bob_id, "bob"),
            ("s-admin", "operator", None, "operator"),  # the config admin opened it
            ("s-legacy-ada", "ada", None, None),  # pre-column row: matched by its actor
            ("s-legacy-api", "api", None, None),  # pre-column /v1 row: nobody's
        ])

        assert _listed(admin) == {"s-ada", "s-bob", "s-admin", "s-legacy-ada", "s-legacy-api"}
        assert _listed(ada) == {"s-ada", "s-legacy-ada"}
        assert _listed(bob) == {"s-bob"}

        listed = admin.get(f"/api/chat/{AGENT_ID}/sessions").json()["sessions"]
        rows = {s["session_id"]: (s["created_by"], s["created_by_name"]) for s in listed}
        assert rows["s-ada"] == (ada_id, "ada")
        assert rows["s-admin"] == (None, "operator")
        assert rows["s-legacy-api"] == (None, None)

        # history follows the same rule; a hidden session reads as missing, never 403
        assert _history(ada, "s-ada") == 200 and _history(ada, "s-legacy-ada") == 200
        hidden = ada.get(f"/api/chat/{AGENT_ID}/history", params={"session_id": "s-bob"})
        assert hidden.status_code == 404 and hidden.json()["code"] == "chat.session_not_found"
        assert _history(ada, "s-legacy-api") == 404
        assert _history(bob, "s-ada") == 404
        for sid in ("s-ada", "s-bob", "s-admin", "s-legacy-ada", "s-legacy-api"):
            assert _history(admin, sid) == 200, sid


def test_the_open_console_operator_sees_every_session(client):
    _seed([("s-one", "river", "u-x", "someone"), ("s-two", "api", None, None)])
    assert _listed(client) == {"s-one", "s-two"}
    assert _history(client, "s-one") == 200 and _history(client, "s-two") == 200


def test_a_session_with_no_row_yet_is_readable_by_whoever_asks(app):
    """The first turn's row lands at `meta`; until then the history is empty, not hidden."""
    with TestClient(app, client=PEER) as admin:
        assert admin.post("/api/auth/login", json=ADMIN).status_code == 200
        _, ada = _member(app, "ada")
        _seed([])
        res = ada.get(f"/api/chat/{AGENT_ID}/history", params={"session_id": "s-not-yet"})
        assert res.status_code == 200 and res.json()["messages"] == []
